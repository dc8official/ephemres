""""In state since" survives the 24-hour split and engine restarts (real TimescaleDB).

The engine closes a period every 24 hours and on every restart, and opens a new row for the
same state. last_seen (shown as "In state since") read the latest row's start, so an endpoint
up for weeks showed "UP for 3 hours" after a restart or a split. The new row now carries the
start of the unbroken state; a real state change still starts it again.
"""
import asyncio
import datetime
import uuid

from monitoring.ping import PingResult, classify_ping_result
from monitoring.state_machine import StateMachine

HOST = uuid.UUID("55555555-0000-0000-0000-0000000000c1")
GOOD = PingResult(success_count=4, failed_count=0, avg_rtt_ms=10.0)
LOST = PingResult(success_count=0, failed_count=4, avg_rtt_ms=None)
UP_OP, UP_DETAIL = classify_ping_result(GOOD)


def _seed(run_sql, open_since: datetime.timedelta, state_since=None):
    now = datetime.datetime.now(datetime.timezone.utc)

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
            VALUES ($1, '10.9.2.1', 'since-host', 'Server', 'ACTIVE')
            """,
            HOST,
        )
        await conn.execute(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, state_since,
                end_time, duration_seconds, monitoring_cycle_count)
            VALUES ($1, $2, $3, 4, 0, 100, 10, false, $4, $5, NULL, 0, 1)
            """,
            HOST, UP_OP, UP_DETAIL, now - open_since, state_since,
        )

    run_sql(seed)
    return now


def _with_session(timescale_dsn, fn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def run():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                result = await fn(session)
                await session.commit()
                return result
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _cycles(timescale_dsn, *results, threshold=1):
    async def run(session):
        machine = StateMachine(confirmation_threshold=threshold)
        state = await machine.initialize_endpoint(HOST, session)
        for result in results:
            state = await machine.process_cycle(state, result, session)

    _with_session(timescale_dsn, run)


def _last_seen(api_client):
    resp = api_client.get(f"/api/v1/endpoints/{HOST}")
    assert resp.status_code == 200, resp.text
    return datetime.datetime.fromisoformat(resp.json()["data"]["last_seen"])


async def _events(conn):
    return await conn.fetch(
        "SELECT start_time, state_since, end_time, is_split_event FROM endpoint_events "
        "WHERE endpoint_id = $1 ORDER BY start_time",
        HOST,
    )


def test_daily_split_keeps_the_state_start(timescale_dsn, run_sql, api_client):
    began = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    _seed(run_sql, datetime.timedelta(hours=25), state_since=began)
    _cycles(timescale_dsn, GOOD)

    closed, split = run_sql(_events)
    assert closed["end_time"] is not None and split["is_split_event"]
    assert split["state_since"] == began
    assert abs(_last_seen(api_client) - began) < datetime.timedelta(seconds=1)


def test_engine_restart_keeps_the_state_start(timescale_dsn, run_sql, api_client):
    now = _seed(run_sql, datetime.timedelta(hours=2))
    from monitoring.gap_handler import resolve_startup_state

    _with_session(timescale_dsn, resolve_startup_state)
    _cycles(timescale_dsn, GOOD)

    closed, reopened = run_sql(_events)
    assert closed["end_time"] is not None and reopened["end_time"] is None
    assert reopened["state_since"] == closed["start_time"]
    assert abs(_last_seen(api_client) - (now - datetime.timedelta(hours=2))) < datetime.timedelta(seconds=1)


def test_state_change_starts_the_state_again(timescale_dsn, run_sql, api_client):
    _seed(run_sql, datetime.timedelta(hours=2))
    _cycles(timescale_dsn, LOST, LOST)  # the first starts the pending count

    _, down = run_sql(_events)
    assert down["state_since"] is None
    assert _last_seen(api_client) == down["start_time"]
