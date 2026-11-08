"""Time the monitoring engine was not running does not count against availability (real TimescaleDB).

monitoring_service_events was meant to hold those gaps, and availability leaves them out, but
nothing recorded them: on start the engine closed the open periods at the start time, crediting
the downtime to whatever state the endpoint was in, and the minutes before the first new cycle
counted against it. Now the start closes each period at its last cycle and records the gap from
there until the first cycles after the start.

Here the engine stopped an hour ago, after an endpoint had been UP for an hour.
"""
import asyncio
import datetime
import uuid

import pytest

from monitoring.gap_handler import close_startup_gap, resolve_startup_state
from monitoring.ping import PingResult, classify_ping_result
from monitoring.state_machine import StateMachine

HOST = uuid.UUID("77777777-0000-0000-0000-0000000000c1")
STALE = uuid.UUID("77777777-0000-0000-0000-0000000000c2")  # paused days ago, period left open
GOOD = PingResult(success_count=4, failed_count=0, avg_rtt_ms=10.0)
UP_OP, UP_DETAIL = classify_ping_result(GOOD)
HOUR = datetime.timedelta(hours=1)


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    began = now - 2 * HOUR

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
            VALUES ($1, '10.9.4.1', 'gap-host', 'Server', 'ACTIVE', $3),
                   ($2, '10.9.4.2', 'paused-host', 'Server', 'ACTIVE', $3)
            """,
            HOST, STALE, began,
        )
        await conn.executemany(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                duration_seconds, monitoring_cycle_count)
            VALUES ($1, $2, $3, 240, 0, 100, 10, false, $4, NULL, $5, 60)
            """,
            [
                (HOST, UP_OP, UP_DETAIL, began, 3600),  # last cycle an hour ago
                (STALE, UP_OP, UP_DETAIL, now - 3 * 24 * HOUR, 600),  # last cycle 3 days ago
            ],
        )

    run_sql(seed)
    return now, began


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


def _first_cycle(timescale_dsn):
    async def run(session):
        machine = StateMachine(confirmation_threshold=3)
        state = await machine.initialize_endpoint(HOST, session)
        await machine.process_cycle(state, GOOD, session)

    _with_session(timescale_dsn, run)


async def _events(conn):
    return await conn.fetch(
        "SELECT start_time, end_time, state_since FROM endpoint_events WHERE endpoint_id = $1 ORDER BY start_time",
        HOST,
    )


async def _gaps(conn):
    return await conn.fetch("SELECT event_type, start_time, end_time FROM monitoring_service_events")


def test_start_records_the_downtime_and_closes_periods_at_their_last_cycle(timescale_dsn, run_sql):
    now, began = _seed(run_sql)
    started_at = datetime.datetime.now(datetime.timezone.utc)

    gap_id = _with_session(timescale_dsn, resolve_startup_state)
    assert gap_id is not None
    (closed,) = run_sql(_events)
    assert closed["end_time"] == began + HOUR  # not the start time: the last hour was not probed
    (gap,) = run_sql(_gaps)
    # The paused endpoint's 3-day-old cycle does not start the gap.
    assert (gap["event_type"], gap["start_time"], gap["end_time"]) == ("SERVICE_RESTART", began + HOUR, None)

    _first_cycle(timescale_dsn)
    _with_session(timescale_dsn, lambda session: close_startup_gap(session, gap_id, started_at))

    closed, reopened = run_sql(_events)
    (gap,) = run_sql(_gaps)
    assert gap["end_time"] == reopened["start_time"]
    # The state went on across the engine downtime, so "In state since" keeps its start.
    assert reopened["state_since"] == began


def test_downtime_does_not_count_against_availability(timescale_dsn, run_sql, api_client):
    _seed(run_sql)
    started_at = datetime.datetime.now(datetime.timezone.utc)
    gap_id = _with_session(timescale_dsn, resolve_startup_state)
    _first_cycle(timescale_dsn)
    _with_session(timescale_dsn, lambda session: close_startup_gap(session, gap_id, started_at))

    resp = api_client.get(f"/api/v1/endpoints/{HOST}")
    assert resp.status_code == 200, resp.text
    # Up for the hour it was monitored; the hour the engine was down is left out.
    assert resp.json()["data"]["uptime_percentage_24h"] == pytest.approx(100.0, abs=0.05)


def test_start_without_open_periods_records_no_gap(timescale_dsn, run_sql):
    assert _with_session(timescale_dsn, resolve_startup_state) is None
    assert run_sql(_gaps) == []


def test_gap_left_open_by_an_interrupted_start_ends_where_monitoring_resumed(timescale_dsn, run_sql):
    now, began = _seed(run_sql)
    _with_session(timescale_dsn, resolve_startup_state)
    _first_cycle(timescale_dsn)  # the engine ran, then stopped before close_startup_gap

    _with_session(timescale_dsn, resolve_startup_state)
    first, second = sorted(run_sql(_gaps), key=lambda g: g["start_time"])
    _, reopened = run_sql(_events)[:2]
    assert first["start_time"] == began + HOUR
    assert first["end_time"] == reopened["start_time"]
    # The new gap starts at the short run's last cycle and stays open until the next one settles.
    assert second["start_time"] == reopened["start_time"] and second["end_time"] is None


def test_a_start_that_ran_no_cycle_does_not_end_the_gap(timescale_dsn, run_sql):
    """A period opened by the old run just after the gap's start used to end it: 5 minutes of real
    downtime were recorded as 2 s, and since nothing was left open the next start recorded no gap."""
    now, began = _seed(run_sql)
    stopped = began + HOUR

    async def period_opened_two_seconds_later(conn):
        await conn.execute(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                duration_seconds, monitoring_cycle_count)
            VALUES ($1, $2, $3, 4, 0, 100, 10, false, $4, NULL, 10, 1)
            """,
            STALE, UP_OP, UP_DETAIL, stopped + datetime.timedelta(seconds=2),
        )

    run_sql(period_opened_two_seconds_later)
    gap_id = _with_session(timescale_dsn, resolve_startup_state)  # this start never ran a cycle

    started_at = datetime.datetime.now(datetime.timezone.utc)
    assert _with_session(timescale_dsn, resolve_startup_state) == gap_id
    (gap,) = run_sql(_gaps)
    assert (gap["start_time"], gap["end_time"]) == (stopped, None)

    _first_cycle(timescale_dsn)
    _with_session(timescale_dsn, lambda session: close_startup_gap(session, gap_id, started_at))
    (gap,) = run_sql(_gaps)
    assert gap["start_time"] == stopped
    assert gap["end_time"] == run_sql(_events)[-1]["start_time"]


def test_a_gap_that_failed_to_close_ends_at_the_first_cycles_of_its_run(timescale_dsn, run_sql):
    """Not at a state change a day later, which would leave that day out of availability."""
    now, began = _seed(run_sql)
    _with_session(timescale_dsn, resolve_startup_state)
    run_started = now - 24 * HOUR + 5 * HOUR
    first_cycle = run_started + datetime.timedelta(seconds=40)

    async def ran_for_a_day(conn):
        await conn.execute(
            "UPDATE monitoring_service_events SET created_at = $1, start_time = $2",
            run_started, run_started - HOUR,
        )
        await conn.executemany(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                duration_seconds, monitoring_cycle_count)
            VALUES ($1, $2, $3, 4, 0, 100, 10, false, $4, $5, $6, 1)
            """,
            [
                (HOST, UP_OP, UP_DETAIL, first_cycle, first_cycle + HOUR, 3600),
                (HOST, "DOWN", "DOWN", now - datetime.timedelta(seconds=10), None, 5),
            ],
        )

    run_sql(ran_for_a_day)
    _with_session(timescale_dsn, resolve_startup_state)
    first, second = sorted(run_sql(_gaps), key=lambda g: g["start_time"])
    assert first["end_time"] == first_cycle
    assert second["start_time"] == now - datetime.timedelta(seconds=5) and second["end_time"] is None
