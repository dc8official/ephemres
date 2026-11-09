"""Recovering from a lost database, against real TimescaleDB.

An endpoint that went down and recovered during a database outage used to leave no trace, and
event listeners never came back after their connection died.
"""
import asyncio
from collections import deque
import datetime
import urllib.parse
import uuid
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

from monitoring.cycle_store import CycleStore, ProbeCycle
from monitoring.gap_handler import record_lost_cycles_gap
from monitoring.ping import PingResult
from monitoring.state_machine import StateMachine

HOST = uuid.UUID("88888888-0000-0000-0000-0000000000d1")
GOOD = PingResult(success_count=5, failed_count=0, avg_rtt_ms=10.0)
BAD = PingResult(success_count=0, failed_count=5, avg_rtt_ms=None)
MINUTE = datetime.timedelta(minutes=1)


def _factory(dsn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def host(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
               VALUES ($1, '10.9.5.1', 'outage-host', 'Server', 'ACTIVE', $2)""",
            HOST, now - datetime.timedelta(hours=2),
        )

    run_sql(seed)
    return now


def _replay(dsn, results, began):
    """The engine's path: a first cycle, then a backlog of `results`, one minute apart."""
    async def run():
        engine, factory = _factory(dsn)
        machine = StateMachine(confirmation_threshold=3)
        machine.announce_transition = AsyncMock()
        store = CycleStore(machine, asyncio.Semaphore(5), session_factory=factory)
        try:
            with patch("monitoring.probe_samples.AsyncSessionLocal", factory):
                first = [ProbeCycle(began, began + datetime.timedelta(seconds=32), GOOD)]
                state = await store.write(HOST, None, deque(first))
                backlog = deque(
                    ProbeCycle(began + (i + 1) * MINUTE, began + (i + 1) * MINUTE + datetime.timedelta(seconds=32), r)
                    for i, r in enumerate(results)
                )
                final = await store.write(HOST, state, backlog)
            return final, machine.announce_transition.await_count, len(backlog)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _events(run_sql):
    async def read(conn):
        rows = await conn.fetch(
            "SELECT detailed_state, start_time, end_time FROM endpoint_events WHERE endpoint_id=$1 ORDER BY start_time", HOST
        )
        samples = await conn.fetchval("SELECT count(*) FROM endpoint_probe_samples WHERE endpoint_id=$1", HOST)
        return rows, samples

    return run_sql(read)


def test_outage_during_a_database_outage_is_in_history_with_its_real_times(timescale_dsn, host, run_sql):
    began = host - 20 * MINUTE
    final, announced, left = _replay(timescale_dsn, [GOOD] * 2 + [BAD] * 5 + [GOOD] * 5, began)

    rows, samples = _events(run_sql)
    assert [r["detailed_state"] for r in rows if r["detailed_state"] in ("UP", "DOWN")] == ["UP", "DOWN", "UP"]
    down = next(r for r in rows if r["detailed_state"] == "DOWN")
    # Confirmed on the third bad cycle (minutes 3, 4, 5 after the first), ended on the third good one.
    assert down["start_time"] == began + 5 * MINUTE + datetime.timedelta(seconds=32)
    assert down["end_time"] == began + 10 * MINUTE + datetime.timedelta(seconds=32)
    assert rows[-1]["end_time"] is None
    assert samples == 13
    # It was over by the time the database came back: recorded, not alerted.
    assert announced == 0
    assert left == 0
    assert final.confirmed_detailed_state == "UP"


def test_endpoint_still_down_when_the_database_returns_alerts_once(timescale_dsn, host, run_sql):
    began = host - 20 * MINUTE
    final, announced, _ = _replay(timescale_dsn, [GOOD] + [BAD] * 6, began)
    assert final.confirmed_detailed_state == "DOWN"
    assert announced == 1


def test_lost_cycles_gap_is_accepted_by_the_table(timescale_dsn, run_sql, host):
    start, end = host - 300 * MINUTE, host - 60 * MINUTE

    async def run():
        engine, factory = _factory(timescale_dsn)
        try:
            async with factory() as db:
                await record_lost_cycles_gap(db, start, end)
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())

    async def read(conn):
        return await conn.fetchrow(
            "SELECT event_type, end_time, duration_seconds FROM monitoring_service_events WHERE start_time=$1", start
        )

    row = run_sql(read)
    assert row["event_type"] == "DATABASE_DISCONNECT" and row["end_time"] == end and row["duration_seconds"] == 14400


def test_listener_comes_back_after_postgresql_kills_its_connection(timescale_dsn, monkeypatch, run_sql):
    from app.config import settings
    from app.services import event_broker

    parts = urllib.parse.urlsplit(timescale_dsn)
    for attr, value in (("host", parts.hostname), ("port", parts.port), ("user", parts.username),
                        ("password", parts.password or ""), ("name", parts.path.lstrip("/"))):
        monkeypatch.setattr(settings.database, attr, value)
    monkeypatch.setattr(event_broker, "LISTENER_RECONNECT_SECONDS", 0.1)

    async def listeners(conn):
        return await conn.fetch("SELECT pid FROM pg_stat_activity WHERE query = 'LISTEN \"outage_test\"'")

    async def scenario():
        admin = await asyncpg.connect(timescale_dsn)
        gen = event_broker.PostgresEventBroker(session_factory=None).subscribe("outage_test")
        pending = asyncio.ensure_future(gen.__anext__())
        try:
            for _ in range(50):
                old = await listeners(admin)
                if old:
                    break
                await asyncio.sleep(0.1)
            assert old, "subscriber never started listening"
            await admin.execute("SELECT pg_terminate_backend($1)", old[0]["pid"])
            for _ in range(50):
                new = await listeners(admin)
                if new and new[0]["pid"] != old[0]["pid"]:
                    break
                await asyncio.sleep(0.1)
            else:
                pytest.fail("the listener did not reconnect after its connection was killed")
            await admin.execute("SELECT pg_notify('outage_test', '{\"after\": \"restart\"}')")
            assert await asyncio.wait_for(pending, 5) == {"after": "restart"}
        finally:
            await gen.aclose()
            await admin.close()

    asyncio.run(scenario())
