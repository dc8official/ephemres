"""Sign-in lockouts and the installer-default admin password (real PostgreSQL).

Lockouts used to live in each API worker's memory: every worker counted on its own, a restart
forgot them, and a table capped at 2000 keys evicted the oldest, so a few thousand sign-ins
with junk usernames released a locked account. They now live in auth_login_failures (0020),
written with one upsert per failure.

Installers up to v3.3.5 seeded admin/admin; upgrades replace it with a random temporary
password when nobody ever changed it.
"""
import asyncio
import os
import stat

import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services.session_store import PostgresSessionStore

IP = "203.0.113.7"


def _with_store(timescale_dsn, fn):
    """Runs `fn(store, session_factory)` with a PostgresSessionStore on the test database."""

    async def _inner():
        engine = create_async_engine(
            timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=5, max_overflow=20
        )
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            return await fn(PostgresSessionStore(factory), factory)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


@pytest.fixture
def clean_failures(run_sql):
    async def wipe(conn):
        await conn.execute("TRUNCATE auth_login_failures")

    run_sql(wipe)
    return run_sql


def test_lockout_is_shared_and_survives_a_junk_username_flood(timescale_dsn, clean_failures):
    async def scenario(store, _factory):
        for _ in range(3):
            await store.record_failed_attempt(IP, "admin", max_attempts=3)
        assert await store.is_account_locked(IP, "admin")
        # Only this address is locked for this username.
        assert not await store.is_account_locked("198.51.100.1", "admin")
        assert not await store.is_account_locked(IP, "operator")

        # The flood that released the old in-memory lockout: more distinct keys than its cap.
        for i in range(2100):
            await store.record_failed_attempt(IP, f"junk{i}", max_attempts=3)
        assert await store.is_account_locked(IP, "admin")

        # Usernames are matched case-insensitively, like the old in-memory key.
        assert await store.is_account_locked(IP, " Admin ")

        await store.clear_failed_attempts(IP, "admin")
        assert not await store.is_account_locked(IP, "admin")

    _with_store(timescale_dsn, scenario)


def test_concurrent_failures_from_several_workers_are_all_counted(timescale_dsn, clean_failures):
    async def scenario(store, _factory):
        await asyncio.gather(*(store.record_failed_attempt(IP, "admin", max_attempts=100) for _ in range(20)))
        return await _count(store, "admin")

    assert _with_store(timescale_dsn, scenario) == 20


async def _count(store, username):
    from app.core.orm import text

    async with store.session_factory() as db:
        res = await db.execute(
            text("SELECT fail_count FROM auth_login_failures WHERE lock_key = :k"), {"k": f"{IP}:{username}"}
        )
        row = res.fetchone()
        return row[0] if row else None


def test_expired_lock_and_quiet_window_restart_the_count(timescale_dsn, clean_failures):
    from app.core.orm import text

    async def scenario(store, factory):
        for _ in range(3):
            await store.record_failed_attempt(IP, "admin", max_attempts=3)
        async with factory() as db:
            await db.execute(text(
                "UPDATE auth_login_failures SET locked_until = NOW() - INTERVAL '1 second' "
                "WHERE lock_key = :k"), {"k": f"{IP}:admin"})
            await db.commit()
        assert not await store.is_account_locked(IP, "admin")
        await store.record_failed_attempt(IP, "admin", max_attempts=3)
        assert await _count(store, "admin") == 1
        assert not await store.is_account_locked(IP, "admin")

        await store.record_failed_attempt(IP, "admin", max_attempts=3)
        async with factory() as db:
            await db.execute(text(
                "UPDATE auth_login_failures SET last_failure_at = NOW() - INTERVAL '31 minutes' "
                "WHERE lock_key = :k"), {"k": f"{IP}:admin"})
            await db.commit()
        await store.record_failed_attempt(IP, "admin", max_attempts=3)
        assert await _count(store, "admin") == 1

    _with_store(timescale_dsn, scenario)


def test_rows_idle_for_a_day_are_removed(timescale_dsn, clean_failures):
    from app.core.orm import text

    async def scenario(store, factory):
        await store.record_failed_attempt(IP, "old", max_attempts=5)
        async with factory() as db:
            await db.execute(text(
                "UPDATE auth_login_failures SET last_failure_at = NOW() - INTERVAL '2 days'"))
            await db.commit()
        await store.record_failed_attempt(IP, "new", max_attempts=5)
        return await _count(store, "old"), await _count(store, "new")

    assert _with_store(timescale_dsn, scenario) == (None, 1)


@pytest.fixture
def admin_account(run_sql):
    async def reset(conn):
        await conn.execute("DELETE FROM user_sessions")
        await conn.execute("DELETE FROM users WHERE username = 'admin'")

    run_sql(reset)

    def create(password):
        from app.services.auth_service import hash_password

        async def insert(conn):
            role_id = await conn.fetchval("SELECT id FROM roles WHERE role_name = 'ADMIN'")
            user_id = await conn.fetchval(
                """INSERT INTO users (username, password_hash, role_id, is_active, must_change_password)
                   VALUES ('admin', $1, $2, TRUE, FALSE) RETURNING id""",
                hash_password(password), role_id,
            )
            await conn.execute(
                "INSERT INTO user_sessions (user_id, jti, expires_at) VALUES ($1, 'old-jti', NOW() + INTERVAL '1 hour')",
                user_id,
            )

        run_sql(insert)

    def state():
        async def read(conn):
            row = await conn.fetchrow(
                "SELECT password_hash, must_change_password FROM users WHERE username = 'admin'")
            sessions = await conn.fetchval("SELECT count(*) FROM user_sessions")
            return row["password_hash"], row["must_change_password"], sessions

        return run_sql(read)

    return create, state


@pytest.mark.parametrize("installed_with", ["admin", "Install-Time-Default"])
def test_upgrade_rotates_an_unchanged_default_admin_password(
    timescale_dsn, admin_account, monkeypatch, tmp_path, installed_with
):
    import app.seed_admin as seed_admin
    from app.services.auth_service import verify_password

    create, state = admin_account
    create(installed_with)
    report = tmp_path / "admin_password_reset.txt"

    async def scenario(_store, factory):
        monkeypatch.setattr(seed_admin, "AsyncSessionLocal", factory)
        return await seed_admin.rotate_default_admin_password(
            ["admin", "Install-Time-Default"], report_path=str(report)
        )

    assert _with_store(timescale_dsn, scenario) is True
    password_hash, must_change, sessions = state()
    username, temp_password = report.read_text().strip().split("\t")
    assert username == "admin"
    assert verify_password(temp_password, password_hash)
    assert not verify_password(installed_with, password_hash)
    assert must_change is True
    assert sessions == 0
    assert stat.S_IMODE(os.stat(report).st_mode) == 0o600


def test_upgrade_keeps_a_password_the_operator_chose(timescale_dsn, admin_account, monkeypatch, tmp_path):
    import app.seed_admin as seed_admin
    from app.services.auth_service import verify_password

    create, state = admin_account
    create("Operator-Chosen-1")
    report = tmp_path / "admin_password_reset.txt"

    async def scenario(_store, factory):
        monkeypatch.setattr(seed_admin, "AsyncSessionLocal", factory)
        return await seed_admin.rotate_default_admin_password(["admin", ""], report_path=str(report))

    assert _with_store(timescale_dsn, scenario) is False
    password_hash, must_change, sessions = state()
    assert verify_password("Operator-Chosen-1", password_hash)
    assert must_change is False
    assert sessions == 1
    assert not report.exists()
