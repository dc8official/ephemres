from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from app.core.orm import delete, select, text

from app.database import AsyncSessionLocal
from app.models.user_session import UserSession

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
    ) -> None:
        pass

    @abstractmethod
    async def is_session_active(self, user_id: str, jti: Optional[str]) -> bool:
        pass

    @abstractmethod
    async def invalidate_session(self, user_id: str, jti: Optional[str]) -> None:
        pass

    @abstractmethod
    async def invalidate_all_user_sessions(self, user_id: str, except_jti: Optional[str] = None) -> None:
        pass

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


class PostgresSessionStore(SessionStore):
    """
    PostgreSQL-native session driver using user_sessions table
    with indexed TTL auto-cleanup and FIFO eviction.
    """

    def __init__(self, session_factory=AsyncSessionLocal) -> None:
        self.session_factory = session_factory

    async def register_session(
        self,
        user_id: str,
        jti: str,
        max_sessions: int = 2,
        ttl_seconds: int = 86400,
    ) -> None:
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)

        async with self.session_factory() as db:
            try:
                # 1. Clean up globally expired sessions for this user
                await db.execute(
                    delete(UserSession).where(
                        UserSession.user_id == u_uuid,
                        UserSession.expires_at <= now,
                    )
                )

                # 2. Insert new session
                new_session = UserSession(
                    user_id=u_uuid,
                    jti=jti,
                    created_at=now,
                    expires_at=expires_at,
                )
                db.add(new_session)
                await db.flush()

                # 3. FIFO session eviction if count > max_sessions
                stmt = (
                    select(UserSession.id)
                    .where(
                        UserSession.user_id == u_uuid,
                        UserSession.expires_at > now,
                    )
                    .order_by(UserSession.created_at.desc())
                )
                res = await db.execute(stmt)
                active_ids = []
                if hasattr(res, "scalars") and hasattr(res.scalars(), "all"):
                    scalars_res = res.scalars().all()
                    if hasattr(scalars_res, "__iter__"):
                        active_ids = list(scalars_res)
                elif hasattr(res, "fetchall"):
                    rows = res.fetchall()
                    if hasattr(rows, "__iter__"):
                        active_ids = [
                            row[0] if isinstance(row, (tuple, list)) else getattr(row, "id", row)
                            for row in rows
                        ]

                if len(active_ids) > max_sessions:
                    excess_ids = active_ids[max_sessions:]
                    await db.execute(
                        delete(UserSession).where(UserSession.id.in_(excess_ids))
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
            stmt = select(UserSession.id).where(
                UserSession.user_id == u_uuid,
                UserSession.jti == jti,
                UserSession.expires_at > now,
            ).limit(1)
            res = await db.execute(stmt)
            row = res.scalar_one_or_none() if hasattr(res, "scalar_one_or_none") else (res.fetchone()[0] if res.fetchone() else None)
            return row is not None

    async def invalidate_session(self, user_id: str, jti: Optional[str]) -> None:
        if not jti:
            return
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        async with self.session_factory() as db:
            try:
                await db.execute(
                    delete(UserSession).where(
                        UserSession.user_id == u_uuid,
                        UserSession.jti == jti,
                    )
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error("PostgresSessionStore.invalidate_session error: %s", e)

    async def invalidate_all_user_sessions(self, user_id: str, except_jti: Optional[str] = None) -> None:
        try:
            u_uuid = UUID(str(user_id))
        except (ValueError, TypeError):
            return

        async with self.session_factory() as db:
            try:
                stmt = delete(UserSession).where(UserSession.user_id == u_uuid)
                if except_jti:
                    stmt = stmt.where(UserSession.jti != except_jti)
                await db.execute(stmt)
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
