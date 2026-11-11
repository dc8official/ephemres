"""Per-account sessions on real PostgreSQL (v3.3.6 build plan, phase 1).

The pure rules are in tests/backend/test_sessions_v336.py. Here: the SQL that applies them.
Time is moved by rewriting created_at / last_activity_at, not by waiting.

* activity (touch_session) restarts the idle timeout; checking a session does not;
* two viewers with 2 and 4 hours end at different times;
* an admin session ends at 12 hours despite activity;
* a changed timeout applies to live sessions at once;
* the per-account limit ends the oldest session as "signed in elsewhere";
* a sign-in sweeps timed-out sessions of every account, with the right reason;
* a session ended through one worker's pool is ended for another (two engines);
* migration 0022 backfills viewers from the old session_timeout setting.
"""
import asyncio
import uuid

import asyncpg
import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services import session_policy
from app.services.session_store import PostgresSessionStore


def _factory(dsn):
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=2)
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def _with_stores(dsn, fn, count=1):
    """Runs `fn(*stores)` with `count` stores, each on its own engine (like separate workers)."""

    async def _inner():
        pairs = [_factory(dsn) for _ in range(count)]
        try:
            return await fn(*[PostgresSessionStore(f) for _, f in pairs])
        finally:
            for engine, _ in pairs:
                await engine.dispose()

    return asyncio.run(_inner())


@pytest.fixture
def make_user(run_sql):
    """Creates an account and returns its id as a string."""

    def _make(role="VIEWER", idle=120, max_sessions=2):
        user_id = uuid.uuid4()

        async def seed(conn):
            role_id = await conn.fetchval("SELECT id FROM roles WHERE role_name = $1", role)
            await conn.execute(
                """INSERT INTO users (id, username, password_hash, role_id, is_active,
                                      must_change_password, session_idle_minutes, max_sessions)
                   VALUES ($1, $2, 'x', $3, true, false, $4, $5)""",
                user_id, f"it-{user_id.hex[:10]}", role_id, idle, max_sessions,
            )

        run_sql(seed)
        return str(user_id)

    return _make


def _shift(run_sql, jti, *, created_ago=None, active_ago=None):
    """Moves a session back in time."""

    async def _do(conn):
        if created_ago is not None:
            await conn.execute(
                "UPDATE user_sessions SET created_at = NOW() - $2::interval WHERE jti = $1", jti, created_ago
            )
        if active_ago is not None:
            await conn.execute(
                "UPDATE user_sessions SET last_activity_at = NOW() - $2::interval WHERE jti = $1", jti, active_ago
            )

    run_sql(_do)


def _ended(run_sql, jti):
    async def _do(conn):
        return await conn.fetchrow("SELECT reason, idle_minutes FROM ended_sessions WHERE jti = $1", jti)

    row = run_sql(_do)
    return (row["reason"], row["idle_minutes"]) if row else None


def _register(store, uid, jti, role="VIEWER", max_sessions=2):
    ttl = int(session_policy.session_lifetime(role).total_seconds())
    return store.register_session(uid, jti, max_sessions=max_sessions, ttl_seconds=ttl)


def test_activity_restarts_the_idle_timeout_and_checks_do_not(timescale_dsn, make_user, run_sql):
    from datetime import timedelta

    uid = make_user(idle=30)
    _with_stores(timescale_dsn, lambda s: _register(s, uid, "a-1"))

    _shift(run_sql, "a-1", active_ago=timedelta(minutes=25))
    # A check (any API call, polling, the event stream) leaves the clock where it was...
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "a-1")) is True
    _shift(run_sql, "a-1", active_ago=timedelta(minutes=25))

    # ...activity restarts it, so 25 more minutes later the session is still alive.
    _with_stores(timescale_dsn, lambda s: s.touch_session(uid, "a-1"))
    _shift(run_sql, "a-1", active_ago=timedelta(minutes=25))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "a-1")) is True

    # Past 30 minutes without activity it ends, and the reason is recorded.
    _shift(run_sql, "a-1", active_ago=timedelta(minutes=31))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "a-1")) is False
    assert _ended(run_sql, "a-1") == ("idle_timeout", 30)
    assert _with_stores(timescale_dsn, lambda s: s.session_end_reason(uid, "a-1")) == ("idle_timeout", 30)


def test_touch_does_not_revive_an_idle_session(timescale_dsn, make_user, run_sql):
    from datetime import timedelta

    uid = make_user(idle=30)
    _with_stores(timescale_dsn, lambda s: _register(s, uid, "z-1"))
    _shift(run_sql, "z-1", active_ago=timedelta(minutes=40))
    _with_stores(timescale_dsn, lambda s: s.touch_session(uid, "z-1"))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "z-1")) is False


def test_viewers_with_2_and_4_hours_end_at_different_times(timescale_dsn, make_user, run_sql):
    from datetime import timedelta

    two = make_user(idle=120)
    four = make_user(idle=240)
    _with_stores(timescale_dsn, lambda s: _register(s, two, "two-1"))
    _with_stores(timescale_dsn, lambda s: _register(s, four, "four-1"))
    for jti in ("two-1", "four-1"):
        _shift(run_sql, jti, active_ago=timedelta(hours=3))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(two, "two-1")) is False
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(four, "four-1")) is True


def test_admin_session_ends_at_12_hours_despite_activity(timescale_dsn, make_user, run_sql):
    from datetime import timedelta

    uid = make_user(role="ADMIN", idle=60)
    _with_stores(timescale_dsn, lambda s: _register(s, uid, "adm-1", role="ADMIN"))
    _shift(run_sql, "adm-1", created_ago=timedelta(hours=11, minutes=59), active_ago=timedelta(seconds=1))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "adm-1")) is True
    _shift(run_sql, "adm-1", created_ago=timedelta(hours=12, minutes=1), active_ago=timedelta(seconds=1))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "adm-1")) is False
    assert _ended(run_sql, "adm-1") == ("absolute_timeout", None)


def test_a_shorter_timeout_applies_to_live_sessions_at_once(timescale_dsn, make_user, run_sql):
    from datetime import timedelta

    uid = make_user(idle=240)
    _with_stores(timescale_dsn, lambda s: _register(s, uid, "live-1"))
    _shift(run_sql, "live-1", active_ago=timedelta(minutes=90))
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "live-1")) is True

    async def shorten(conn):
        await conn.execute("UPDATE users SET session_idle_minutes = 60 WHERE id = $1", uuid.UUID(uid))

    run_sql(shorten)
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(uid, "live-1")) is False


def test_session_limit_ends_the_oldest_as_signed_in_elsewhere(timescale_dsn, make_user, run_sql):
    uid = make_user(max_sessions=2)

    async def scenario(store):
        await _register(store, uid, "lim-1")
        await asyncio.sleep(0.01)  # created_at decides which session is oldest
        await _register(store, uid, "lim-2")
        await asyncio.sleep(0.01)
        await _register(store, uid, "lim-3")
        return [await store.is_session_active(uid, j) for j in ("lim-1", "lim-2", "lim-3")]

    assert _with_stores(timescale_dsn, scenario) == [False, True, True]
    assert _ended(run_sql, "lim-1") == ("signed_in_elsewhere", None)


def test_a_limit_of_one_keeps_only_the_new_session(timescale_dsn, make_user, run_sql):
    uid = make_user(max_sessions=1)

    async def scenario(store):
        await _register(store, uid, "one-1", max_sessions=1)
        await _register(store, uid, "one-2", max_sessions=1)
        return [await store.is_session_active(uid, j) for j in ("one-1", "one-2")]

    assert _with_stores(timescale_dsn, scenario) == [False, True]


def test_sign_in_sweeps_timed_out_sessions_of_every_account(timescale_dsn, make_user, run_sql):
    """An idle session must not count against the limit or be reported as signed in elsewhere."""
    from datetime import timedelta

    other = make_user(idle=30)
    uid = make_user(idle=30, max_sessions=1)
    _with_stores(timescale_dsn, lambda s: _register(s, other, "sw-other"))
    _with_stores(timescale_dsn, lambda s: _register(s, uid, "sw-old", max_sessions=1))
    _shift(run_sql, "sw-other", active_ago=timedelta(hours=1))
    _shift(run_sql, "sw-old", active_ago=timedelta(hours=1))

    _with_stores(timescale_dsn, lambda s: _register(s, uid, "sw-new", max_sessions=1))
    assert _ended(run_sql, "sw-old") == ("idle_timeout", 30)
    assert _ended(run_sql, "sw-other") == ("idle_timeout", 30)


def test_sign_out_on_one_worker_holds_on_another(timescale_dsn, make_user, run_sql):
    """Two engines, two pools: the shape of two uvicorn workers sharing the database."""
    uid = make_user()

    async def scenario(worker_a, worker_b):
        await _register(worker_a, uid, "w-1")
        await _register(worker_a, uid, "w-2")
        before = await worker_b.is_session_active(uid, "w-1")
        await worker_a.invalidate_session(uid, "w-1", reason="signed_out")
        await worker_b.invalidate_all_user_sessions(uid, reason="password_changed")
        after = [await worker_a.is_session_active(uid, "w-1"), await worker_a.is_session_active(uid, "w-2")]
        reasons = [await worker_b.session_end_reason(uid, "w-1"), await worker_a.session_end_reason(uid, "w-2")]
        return before, after, reasons

    before, after, reasons = _with_stores(timescale_dsn, scenario, count=2)
    assert before is True
    assert after == [False, False]
    assert reasons == [("signed_out", None), ("password_changed", None)]


def test_migration_0022_backfills_from_the_old_settings(timescale_dsn):
    """A database at 0021 with session_timeout = 45 and two accounts, upgraded to head."""
    import os
    import urllib.parse

    from app.core import migrator

    admin_dsn = urllib.parse.urlunsplit(urllib.parse.urlsplit(timescale_dsn)._replace(path="/postgres"))
    name = f"ephemres_it_mig_{uuid.uuid4().hex[:8]}"
    db_dsn = urllib.parse.urlunsplit(urllib.parse.urlsplit(timescale_dsn)._replace(path=f"/{name}"))

    async def scenario():
        admin = await asyncpg.connect(admin_dsn)
        await admin.execute(f'CREATE DATABASE "{name}"')
        previous = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = db_dsn
        try:
            await migrator.upgrade_database("0021")
            conn = await asyncpg.connect(db_dsn)
            try:
                await conn.execute(
                    """INSERT INTO app_settings (setting_key, setting_value) VALUES ('session_timeout', '45')
                       ON CONFLICT (setting_key) DO UPDATE SET setting_value = '45'"""
                )
                for username, role in (("old-admin", "ADMIN"), ("old-viewer", "VIEWER")):
                    await conn.execute(
                        """INSERT INTO users (username, password_hash, role_id)
                           SELECT $1, 'x', id FROM roles WHERE role_name = $2""",
                        username, role,
                    )
                await conn.execute(
                    "INSERT INTO users (username, password_hash, role_id) VALUES ('no-role', 'x', NULL)"
                )
            finally:
                await conn.close()

            await migrator.upgrade_database("head")
            conn = await asyncpg.connect(db_dsn)
            try:
                rows = await conn.fetch("SELECT username, session_idle_minutes, max_sessions FROM users")
                return {r["username"]: (r["session_idle_minutes"], r["max_sessions"]) for r in rows}
            finally:
                await conn.close()
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await admin.close()

    users = asyncio.run(scenario())
    # 45 minutes fits down to the viewer choice 30; admins start at 30; the limit comes from config.
    assert users["old-admin"][0] == 30
    assert users["old-viewer"][0] == 30
    assert users["no-role"][0] == 30
    assert {v[1] for k, v in users.items() if k in ("old-admin", "old-viewer", "no-role")} == {
        session_policy.default_max_sessions()
    }


def test_session_list_shows_live_sessions_with_device_and_end_time(timescale_dsn, make_user, run_sql):
    """The Users tab's list: newest first, device recorded, timed-out sessions left out."""
    from datetime import datetime, timedelta, timezone

    uid = make_user(idle=60)

    async def scenario(store):
        await store.register_session(uid, "ls-old", max_sessions=5, ttl_seconds=7 * 86400,
                                     client_ip="10.0.0.5", user_agent="Mozilla/5.0 (iPhone)")
        await asyncio.sleep(0.01)
        await store.register_session(uid, "ls-new", max_sessions=5, ttl_seconds=7 * 86400,
                                     client_ip="10.0.0.9", user_agent="x" * 400)
        await asyncio.sleep(0.01)
        await store.register_session(uid, "ls-idle", max_sessions=5, ttl_seconds=7 * 86400)

    _with_stores(timescale_dsn, scenario)
    _shift(run_sql, "ls-idle", active_ago=timedelta(minutes=61))
    _shift(run_sql, "ls-old", active_ago=timedelta(minutes=20))

    rows = _with_stores(timescale_dsn, lambda s: s.list_sessions(uid))
    assert [r["jti"] for r in rows] == ["ls-new", "ls-old"]
    assert rows[1]["client_ip"] == "10.0.0.5"
    assert len(rows[0]["user_agent"]) == 255  # cut to the column size, not refused
    # Idle for 20 of 60 minutes: about 40 minutes left.
    left = rows[1]["ends_at"] - datetime.now(timezone.utc)
    assert timedelta(minutes=39) < left <= timedelta(minutes=40)
    # The idle one was swept with its reason rather than listed.
    assert _ended(run_sql, "ls-idle") == ("idle_timeout", 60)

    counts = _with_stores(timescale_dsn, lambda s: s.active_session_counts())
    assert counts[uid] == 2


def test_end_by_id_needs_the_right_account(timescale_dsn, make_user, run_sql):
    owner = make_user()
    other = make_user()
    _with_stores(timescale_dsn, lambda s: _register(s, owner, "eb-1"))
    row_id = _with_stores(timescale_dsn, lambda s: s.list_sessions(owner))[0]["id"]

    assert _with_stores(timescale_dsn, lambda s: s.end_session_by_id(other, str(row_id), "ended_by_admin")) is False
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(owner, "eb-1")) is True
    assert _with_stores(timescale_dsn, lambda s: s.end_session_by_id(owner, str(row_id), "ended_by_admin")) is True
    assert _with_stores(timescale_dsn, lambda s: s.is_session_active(owner, "eb-1")) is False
    assert _ended(run_sql, "eb-1") == ("ended_by_admin", None)
    assert _with_stores(timescale_dsn, lambda s: s.end_session_by_id(owner, str(row_id), "ended_by_admin")) is False
