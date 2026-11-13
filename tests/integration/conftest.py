"""Fixtures for the `timescale` integration tests (see README.md in this directory)."""
import asyncio
import os
import secrets
import urllib.parse
from pathlib import Path

import pytest

asyncpg = pytest.importorskip("asyncpg")

DSN_ENV = "EPHEMRES_TEST_TIMESCALE_DSN"
_HERE = Path(__file__).resolve().parent

# Tables the characterization tests write to; truncated between tests.
_SEEDED_TABLES = (
    "endpoint_probe_samples",
    "flow_minute_rollups",
    "flow_interface_minute_rollups",
    # SNMP counters have no foreign key to endpoints, so CASCADE doesn't reach them.
    "snmp_interface_minute_rollups",
    "endpoint_events",
    "monitoring_service_events",
    "endpoints",
    "snmp_credentials",
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(_HERE):
            item.add_marker(pytest.mark.timescale)


def _with_database(dsn: str, name: str) -> str:
    parts = urllib.parse.urlsplit(dsn)
    return urllib.parse.urlunsplit(parts._replace(path=f"/{name}"))


async def _create_and_migrate(admin_dsn: str, db_name: str) -> str:
    admin = await asyncpg.connect(admin_dsn, timeout=30.0)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()

    db_dsn = _with_database(admin_dsn, db_name)
    from app.core import migrator

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = db_dsn
    try:
        await migrator.upgrade_database("head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    return db_dsn


async def reset_interface_traffic(conn) -> None:
    """Empty the interface minute tables and endpoints of a private test database. DELETE, not
    TRUNCATE, for the minute tables: the hourly aggregates log deletes (and inserts) as changes
    to pick up at their next refresh, but never see a TRUNCATE, so stale hours would survive it."""
    await conn.execute("DELETE FROM flow_interface_minute_rollups")
    await conn.execute("DELETE FROM snmp_interface_minute_rollups")
    await conn.execute("TRUNCATE endpoints CASCADE")


async def settle_interface_hourly(conn) -> None:
    """Make the hourly interface aggregates of a private test database match its minute tables.

    The aggregates' refresh policy starts on its own soon after the migration and moves the
    watermark; rows seeded below it afterwards are hidden from the real-time part until the next
    refresh, so a test reading past days passed or failed with the scheduler's timing. One refresh
    over everything after seeding settles it. The policy's own run refuses a concurrent refresh,
    hence the retries."""
    for view in ("flow_interface_hourly_rollups", "snmp_interface_hourly_rollups"):
        for attempt in range(30):
            try:
                await conn.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
                break
            except asyncpg.exceptions.LockNotAvailableError:
                if attempt == 29:
                    raise
                await asyncio.sleep(1)


async def _drop(admin_dsn: str, db_name: str) -> None:
    admin = await asyncpg.connect(admin_dsn, timeout=30.0)
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="session")
def timescale_dsn():
    """asyncpg DSN of a freshly migrated, session-private database."""
    admin_dsn = os.environ.get(DSN_ENV)
    if not admin_dsn:
        pytest.skip(f"{DSN_ENV} is not set (see tests/integration/README.md)")
    db_name = f"ephemres_it_{secrets.token_hex(4)}"
    db_dsn = asyncio.run(_create_and_migrate(admin_dsn, db_name))
    try:
        yield db_dsn
    finally:
        asyncio.run(_drop(admin_dsn, db_name))


async def _truncate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn, timeout=30.0)
    try:
        await conn.execute(f"TRUNCATE {', '.join(_SEEDED_TABLES)} CASCADE")
    finally:
        await conn.close()


@pytest.fixture
def run_sql(timescale_dsn):
    """Run `fn(conn)` against the test database on a short-lived asyncpg connection."""

    def _run(fn):
        async def _inner():
            conn = await asyncpg.connect(timescale_dsn, timeout=30.0)
            try:
                return await fn(conn)
            finally:
                await conn.close()

        return asyncio.run(_inner())

    asyncio.run(_truncate(timescale_dsn))
    return _run


@pytest.fixture
def api_client(timescale_dsn, monkeypatch):
    """TestClient for the bandwidth, endpoints and reports routers backed by the test database.

    The in-process TestClient runs every request in a new event loop, so each request gets its
    own engine instead of sharing a pool bound to a dead loop. FastStore is disabled.
    """
    from app.core.asgi import FastAPI, TestClient
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
    from app.database import get_db
    from app.routers import bandwidth, endpoints, reports, snmp
    from app.routers.auth import get_current_user, require_admin

    engine_url = timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    async def override_get_db():
        engine = create_async_engine(engine_url, pool_size=2, max_overflow=0)
        session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with session_factory() as session:
                yield session
        finally:
            await engine.dispose()

    async def override_user():
        return {"username": "it-admin", "role": "ADMIN"}

    async def no_flow_store():
        return None

    monkeypatch.setattr(bandwidth, "get_flow_store", no_flow_store)
    monkeypatch.setattr(bandwidth, "get_flow_redis", no_flow_store)

    app = FastAPI()
    app.include_router(bandwidth.router, prefix="/api/v1")
    app.include_router(endpoints.router, prefix="/api/v1")
    app.include_router(reports.router, prefix="/api/v1")
    app.include_router(snmp.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[require_admin] = override_user

    with TestClient(app) as client:
        yield client
