"""The shared record of signed-in sessions and failed sign-ins, kept in PostgreSQL.

Every API worker reads and writes the same tables (user_sessions, auth_login_failures), so a
sign-out, a password change or a lockout on one worker holds on all of them and survives a
restart. PostgresSessionStore is the only implementation (services/driver_manager.py).

From v3.3.6 a session also ends after the account's idle timeout, counted from the last real
interaction (touch_session), and an admin session ends 12 hours after sign-in
(services/session_policy.py). Whenever a session ends, a row in ended_sessions records why, so
the next request from that browser can be told (session_end_reason).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.core.orm import delete, select, text

from app.database import AsyncSessionLocal
from app.models.user_session import UserSession
from app.services import session_policy

logger = logging.getLogger(__name__)


class SessionStore(ABC):
    """Abstract interface for user session management and concurrency control."""

    @abstractmethod
    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        pass

    @abstractmethod
    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        pass

    @abstractmethod
    async def invalidate_session(
        self, user_id: str, jti: Optional[str], reason: str = session_policy.SIGNED_OUT
    ) -> None:
        pass

    @abstractmethod
    async def invalidate_all_user_sessions(
        self, user_id: str, except_jti: Optional[str] = None, reason: str = session_policy.SIGNED_OUT
    ) -> None:
        pass

    # Not abstract: the two below only add detail (activity, the reason for a 401), and test
    # doubles that leave them out keep the behaviour of a store without idle timeouts.
    async def touch_session(self, user_id: str, jti: Optional[str]) -> None:
        """Records real user interaction on a session, restarting its idle timeout."""
        return None

    async def session_end_reason(self, user_id: str, jti: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
        """(reason, idle minutes) for a session that has ended, or None if unknown."""
        return None

    # The Users tab's session list (admin only). Defaults: a store that can't list shows none.
    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """The account's live sessions, newest first (see PostgresSessionStore.list_sessions)."""
        return []

    async def active_session_counts(self) -> Dict[str, int]:
        """Live sessions per account id, for the Users table."""
        return {}

    async def end_session_by_id(self, user_id: str, session_id: str, reason: str) -> bool:
        """Ends one session of the account, picked by its row id; False if there is none."""
        return False

    @abstractmethod
    async def record_failed_attempt(
        self, client_ip: str, username: str, max_attempts: Optional[int] = None, lockout_seconds: int = 900
    ) -> None:
        pass

    @abstractmethod
    async def is_account_locked(self, client_ip: str, username: str) -> bool:
        pass

    @abstractmethod
    async def clear_failed_attempts(self, client_ip: str, username: str) -> None:
        pass


# Deletes sessions of one account and records why; callers append the jti condition and the tail.
_END_SESSIONS_SQL = """
    WITH gone AS (
        DELETE FROM user_sessions
        WHERE user_id = CAST(:uid AS uuid)"""
_END_SESSIONS_TAIL = """
        RETURNING jti, user_id
    )
    INSERT INTO ended_sessions (jti, user_id, reason, ended_at)
    SELECT jti, user_id, CAST(:reason AS varchar), CAST(:now AS timestamptz)
    FROM gone
    ON CONFLICT (jti) DO NOTHING
"""


def _timed_out_reason(row, now: datetime) -> Optional[str]:
    """Why the session in `row` (created_at, last_activity_at, expires_at, idle minutes, role)
    has timed out at `now`, or None while it is still active. Mirrors _SWEEP_ENDED_SQL: the
    earlier of the idle end and the absolute end decides the reason."""
    created_at, last_activity_at, expires_at, idle_minutes, role_name = (
        row[0], row[1], row[2], row[3], row[4],
    )
    idle_end = last_activity_at + timedelta(minutes=int(idle_minutes))
    hard_end = expires_at
    if str(role_name or "").upper() == session_policy.ADMIN:
        hard_end = min(hard_end, created_at + session_policy.ADMIN_ABSOLUTE_LIFETIME)
    if min(idle_end, hard_end) > now:
        return None
    return session_policy.IDLE_TIMEOUT if idle_end <= hard_end else session_policy.ABSOLUTE_TIMEOUT


class PostgresSessionStore(SessionStore):
    """
    PostgreSQL-native session driver using the user_sessions table, with idle and absolute
    timeouts, FIFO eviction past the account's session limit, and ended_sessions recording why
    each session ended.
    """

    # Ends every session whose idle timeout or absolute end has passed and records why. Run on
    # each sign-in for all accounts (the tables hold a few rows per user), which also clears
    # the rows of browsers that were simply closed. Whichever end came first is the reason.
    _SWEEP_ENDED_SQL = f"""
        WITH candidates AS (
            SELECT s.id, s.jti, s.user_id, u.session_idle_minutes AS idle_minutes,
                   s.last_activity_at + make_interval(mins => u.session_idle_minutes) AS idle_end,
                   CASE WHEN r.role_name = '{session_policy.ADMIN}'
                        THEN LEAST(s.expires_at, s.created_at + make_interval(
                                 secs => {int(session_policy.ADMIN_ABSOLUTE_LIFETIME.total_seconds())}))
                        ELSE s.expires_at
                   END AS hard_end
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN roles r ON r.id = u.role_id
        ),
        dead AS (
            DELETE FROM user_sessions s USING candidates c
            WHERE s.id = c.id AND LEAST(c.idle_end, c.hard_end) <= CAST(:now AS timestamptz)
            RETURNING c.jti, c.user_id, c.idle_minutes, c.idle_end, c.hard_end
        )
        INSERT INTO ended_sessions (jti, user_id, reason, idle_minutes, ended_at)
        SELECT jti, user_id,
               CASE WHEN idle_end <= hard_end THEN '{session_policy.IDLE_TIMEOUT}'
                    ELSE '{session_policy.ABSOLUTE_TIMEOUT}' END,
               CASE WHEN idle_end <= hard_end THEN idle_minutes END,
               CAST(:now AS timestamptz)
        FROM dead
        ON CONFLICT (jti) DO NOTHING
    """

    # Keeps the account's newest `max` sessions; the new one always survives, even when an
    # older row has the same created_at. The rest are recorded as signed in elsewhere.
    _EVICT_SQL = f"""
        WITH ranked AS (
            SELECT id, row_number() OVER (
                       ORDER BY (jti = CAST(:new_jti AS varchar)) DESC, created_at DESC, id DESC
                   ) AS rn
            FROM user_sessions
            WHERE user_id = CAST(:uid AS uuid)
        ),
        gone AS (
            DELETE FROM user_sessions s USING ranked r
            WHERE s.id = r.id AND r.rn > CAST(:max AS integer)
            RETURNING s.jti, s.user_id
        )
        INSERT INTO ended_sessions (jti, user_id, reason, ended_at)
        SELECT jti, user_id, '{session_policy.SIGNED_IN_ELSEWHERE}', CAST(:now AS timestamptz)
        FROM gone
        ON CONFLICT (jti) DO NOTHING
    """

    def __init__(self, session_factory=AsyncSessionLocal) -> None:
        self.session_factory = session_factory

    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
        client_ip: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        """Adds a session. ttl_seconds is its absolute end (session_policy.session_lifetime).
        client_ip and user_agent are kept for the admin's session list only."""
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        async with self.session_factory() as db:
            try:
                # 1. End every session that has timed out, so it neither counts against the
                #    session limit nor gets reported as "signed in elsewhere" below.
                await db.execute(text(self._SWEEP_ENDED_SQL), {"now": now})
                await db.execute(
                    text("DELETE FROM ended_sessions WHERE ended_at < CAST(:before AS timestamptz)"),
                    {"before": now - session_policy.ENDED_SESSION_RETENTION},
                )

                # 2. Insert the new session; signing in counts as interaction.
                db.add(
                    UserSession(
                        user_id=u_uuid,
                        jti=jti,
                        created_at=now,
                        last_activity_at=now,
                        expires_at=expires_at,
                        client_ip=(client_ip or None) and str(client_ip)[:45],
                        user_agent=(user_agent or None) and str(user_agent)[:255],
                    )
                )
                await db.flush()

                # 3. Past the account's limit, end the oldest sessions.
                await db.execute(
                    text(self._EVICT_SQL),
                    {"uid": u_uuid, "new_jti": jti, "max": int(max_sessions), "now": now},
                )

                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.register_session error: %s", e)

    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        if not jti:
            return False
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return False

        now = datetime.now(timezone.utc)
        # An error propagates: "can't check" is not "not active" (auth answers 503, not 401).
        async with self.session_factory() as db:
            # The account's timeout and role are read with the session on every check, so a
            # changed timeout applies to live sessions at once (decided behaviour 7).
            res = await db.execute(
                text("""
                    SELECT s.created_at, s.last_activity_at, s.expires_at,
                           u.session_idle_minutes, r.role_name
                    FROM user_sessions s
                    JOIN users u ON u.id = s.user_id
                    LEFT JOIN roles r ON r.id = u.role_id
                    WHERE s.user_id = CAST(:uid AS uuid) AND s.jti = CAST(:jti AS varchar)
                    LIMIT 1
                """),
                {"uid": u_uuid, "jti": str(jti)},
            )
            row = res.fetchone()
            if row is None:
                return False

            reason = _timed_out_reason(row, now)
            if reason is None:
                return True

            # Timed out: end it here so the reason is on record for the 401 that follows. A
            # failure to record only loses the explanation, never lets the session through.
            try:
                await db.execute(
                    text("""
                        WITH gone AS (
                            DELETE FROM user_sessions
                            WHERE user_id = CAST(:uid AS uuid) AND jti = CAST(:jti AS varchar)
                            RETURNING jti, user_id
                        )
                        INSERT INTO ended_sessions (jti, user_id, reason, idle_minutes, ended_at)
                        SELECT jti, user_id, CAST(:reason AS varchar),
                               CAST(:idle AS integer), CAST(:now AS timestamptz)
                        FROM gone
                        ON CONFLICT (jti) DO NOTHING
                    """),
                    {
                        "uid": u_uuid,
                        "jti": str(jti),
                        "reason": reason,
                        "idle": int(row[3]) if reason == session_policy.IDLE_TIMEOUT else None,
                        "now": now,
                    },
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore: could not record a timed-out session: %s", e)
            return False

    async def touch_session(self, user_id: str, jti: Optional[str]) -> None:
        """Restarts the idle timeout. At most one write per 30 seconds per session: the browser
        sends at most one a minute, and this keeps a misbehaving client from writing on every
        call. A session already past its idle timeout is not revived (the caller's auth check
        ran a moment earlier, so this closes the gap between the two)."""
        if not jti:
            return
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return
        now = datetime.now(timezone.utc)
        async with self.session_factory() as db:
            await db.execute(
                text("""
                    UPDATE user_sessions s SET last_activity_at = CAST(:now AS timestamptz)
                    FROM users u
                    WHERE u.id = s.user_id
                      AND s.user_id = CAST(:uid AS uuid) AND s.jti = CAST(:jti AS varchar)
                      AND s.last_activity_at < CAST(:throttle AS timestamptz)
                      AND s.last_activity_at + make_interval(mins => u.session_idle_minutes)
                          > CAST(:now AS timestamptz)
                """),
                {"uid": u_uuid, "jti": str(jti), "now": now, "throttle": now - timedelta(seconds=30)},
            )
            await db.commit()

    async def _sweep(self, db) -> None:
        """Ends timed-out sessions (all accounts) so lists and counts show only live ones."""
        await db.execute(text(self._SWEEP_ENDED_SQL), {"now": datetime.now(timezone.utc)})

    async def list_sessions(self, user_id: str) -> List[Dict[str, Any]]:
        """Live sessions of one account, newest first. Each carries `jti` for the caller to
        compare with its own session; routers must not pass it on (it names the token).
        `ends_at` is when the session ends if nobody touches it: the earlier of the idle end
        and the absolute end. A read with a write first: the sweep, so a session that has
        already timed out is never listed as live."""
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return []
        async with self.session_factory() as db:
            await self._sweep(db)
            await db.commit()
            res = await db.execute(
                text(f"""
                    SELECT s.id, s.jti, s.created_at, s.last_activity_at, s.client_ip, s.user_agent,
                           LEAST(
                               s.last_activity_at + make_interval(mins => u.session_idle_minutes),
                               CASE WHEN r.role_name = '{session_policy.ADMIN}'
                                    THEN LEAST(s.expires_at, s.created_at + make_interval(
                                        secs => {int(session_policy.ADMIN_ABSOLUTE_LIFETIME.total_seconds())}))
                                    ELSE s.expires_at
                               END
                           ) AS ends_at
                    FROM user_sessions s
                    JOIN users u ON u.id = s.user_id
                    LEFT JOIN roles r ON r.id = u.role_id
                    WHERE s.user_id = CAST(:uid AS uuid)
                    ORDER BY s.created_at DESC
                """),
                {"uid": u_uuid},
            )
            return [dict(row) for row in res.mappings().all()]

    async def active_session_counts(self) -> Dict[str, int]:
        async with self.session_factory() as db:
            await self._sweep(db)
            await db.commit()
            res = await db.execute(
                text("SELECT user_id, COUNT(*) AS n FROM user_sessions GROUP BY user_id")
            )
            return {str(row[0]): int(row[1]) for row in res.fetchall()}

    async def end_session_by_id(self, user_id: str, session_id: str, reason: str) -> bool:
        try:
            u_uuid = UUID(str(user_id))
            s_uuid = UUID(str(session_id))
        except (ValueError, TypeError):
            return False
        async with self.session_factory() as db:
            try:
                # Both ids must match, so an admin can't end another account's session through
                # a mismatched URL. RETURNING tells whether anything was ended.
                res = await db.execute(
                    text(_END_SESSIONS_SQL + " AND id = CAST(:sid AS uuid)" + _END_SESSIONS_TAIL
                         + " RETURNING jti"),
                    {"uid": u_uuid, "sid": s_uuid, "reason": reason, "now": datetime.now(timezone.utc)},
                )
                ended = res.fetchone() is not None
                await db.commit()
                return ended
            except Exception:
                await db.rollback()
                raise

    async def session_end_reason(self, user_id: str, jti: Optional[str]) -> Optional[Tuple[str, Optional[int]]]:
        if not jti:
            return None
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return None
        async with self.session_factory() as db:
            res = await db.execute(
                text("""
                    SELECT reason, idle_minutes FROM ended_sessions
                    WHERE user_id = CAST(:uid AS uuid) AND jti = CAST(:jti AS varchar)
                """),
                {"uid": u_uuid, "jti": str(jti)},
            )
            row = res.fetchone()
            if row is None:
                return None
            return str(row[0]), (int(row[1]) if row[1] is not None else None)

    async def invalidate_session(
        self, user_id: str, jti: Optional[str], reason: str = session_policy.SIGNED_OUT
    ) -> None:
        if not jti:
            return
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        async with self.session_factory() as db:
            try:
                await db.execute(
                    text(_END_SESSIONS_SQL + " AND jti = CAST(:jti AS varchar)" + _END_SESSIONS_TAIL),
                    {"uid": u_uuid, "jti": str(jti), "reason": reason, "now": datetime.now(timezone.utc)},
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.invalidate_session error: %s", e)

    async def invalidate_all_user_sessions(
        self, user_id: str, except_jti: Optional[str] = None, reason: str = session_policy.SIGNED_OUT
    ) -> None:
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        params = {"uid": u_uuid, "reason": reason, "now": datetime.now(timezone.utc)}
        where = ""
        if except_jti:
            where = " AND jti <> CAST(:except_jti AS varchar)"
            params["except_jti"] = str(except_jti)
        async with self.session_factory() as db:
            try:
                await db.execute(text(_END_SESSIONS_SQL + where + _END_SESSIONS_TAIL), params)
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.invalidate_all_user_sessions error: %s", e)

    # Failed sign-ins live in auth_login_failures (migration 0020), shared by every worker and
    # kept across restarts. A counter restarts after FAILURE_WINDOW without failures, or once
    # its lockout has run out.
    FAILURE_WINDOW = timedelta(minutes=30)
    IDLE_ROW_RETENTION = timedelta(days=1)

    async def record_failed_attempt(
        self, client_ip: str, username: str, max_attempts: Optional[int] = None, lockout_seconds: int = 900
    ) -> None:
        if max_attempts is None:
            from app.services.runtime_settings import get_runtime_settings

            max_attempts = get_runtime_settings().lockout_threshold
        from app.services.auth_service import _get_lockout_key

        now = datetime.now(timezone.utc)
        params = {
            "k": _get_lockout_key(client_ip, username),
            "now": now,
            "window_start": now - self.FAILURE_WINDOW,
            "lock_until": now + timedelta(seconds=lockout_seconds),
            "max": int(max_attempts),
            "idle_before": now - self.IDLE_ROW_RETENTION,
        }
        # One statement, so two workers counting the same key never lose a failure.
        new_count = """CASE WHEN f.last_failure_at < CAST(:window_start AS timestamptz)
                              OR (f.locked_until IS NOT NULL AND f.locked_until <= CAST(:now AS timestamptz))
                            THEN 1 ELSE f.fail_count + 1 END"""
        upsert = text(f"""
            INSERT INTO auth_login_failures AS f (lock_key, fail_count, last_failure_at, locked_until)
            VALUES (:k, 1, CAST(:now AS timestamptz),
                    CASE WHEN 1 >= CAST(:max AS integer) THEN CAST(:lock_until AS timestamptz) END)
            ON CONFLICT (lock_key) DO UPDATE SET
                fail_count = {new_count},
                last_failure_at = CAST(:now AS timestamptz),
                locked_until = CASE
                    WHEN f.locked_until > CAST(:now AS timestamptz) THEN f.locked_until
                    WHEN {new_count} >= CAST(:max AS integer) THEN CAST(:lock_until AS timestamptz)
                    ELSE NULL
                END
        """)
        async with self.session_factory() as db:
            try:
                await db.execute(upsert, params)
                await db.execute(
                    text("DELETE FROM auth_login_failures WHERE last_failure_at < CAST(:idle_before AS timestamptz)"),
                    {"idle_before": params["idle_before"]},
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.record_failed_attempt error: %s", e)
                raise

    async def is_account_locked(self, client_ip: str, username: str) -> bool:
        from app.services.auth_service import _get_lockout_key

        async with self.session_factory() as db:
            res = await db.execute(
                text("""
                    SELECT 1 FROM auth_login_failures
                    WHERE lock_key = :k AND locked_until > CAST(:now AS timestamptz)
                    LIMIT 1
                """),
                {"k": _get_lockout_key(client_ip, username), "now": datetime.now(timezone.utc)},
            )
            return res.fetchone() is not None

    async def clear_failed_attempts(self, client_ip: str, username: str) -> None:
        from app.services.auth_service import _get_lockout_key

        async with self.session_factory() as db:
            try:
                await db.execute(
                    text("DELETE FROM auth_login_failures WHERE lock_key = :k"),
                    {"k": _get_lockout_key(client_ip, username)},
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.clear_failed_attempts error: %s", e)
