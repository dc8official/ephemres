"""A closed endpoint_events row carries the health and RTT of its whole period (real TimescaleDB).

While an event is open, health_score and avg_rtt_ms hold the latest cycle (the dashboard's
current values). They used to stay that way on close, so an UP period that ended in an outage
showed 0 % health and no RTT in the audit log after hours of good probes.

The open event started 10 minutes ago: 8 good cycles (RTT 10..17 ms) and 2 lost ones, and its
latest cycle failed (health 0, no RTT). The endpoint is probed in a fixed slot each minute; the
opening cycle's probes timed out for 40 s, so its sample is 40 s older than the event. A sample
from the previous period (500 ms) never counts. The newest sample (999 ms) is the cycle that
confirmed a state change, so it counts only when the engine closes the event without one.
"""
import asyncio
import datetime
import uuid

from monitoring.probe_samples import closed_event_stats_sql

HOST = uuid.UUID("33333333-0000-0000-0000-0000000000c1")


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(minutes=10)
    slot = start - datetime.timedelta(seconds=40)  # opening cycle's sample, 40 s before the event

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
            VALUES ($1, '10.9.1.1', 'period-host', 'Server', 'ACTIVE')
            """,
            HOST,
        )
        await conn.execute(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                duration_seconds, monitoring_cycle_count)
            VALUES ($1, 'UP', 'UP', 8, 2, 0, NULL, false, $2, NULL, 0, 10)
            """,
            HOST, start,
        )
        minute = datetime.timedelta(minutes=1)
        rows = [(HOST, slot + k * minute, 1, 1, float(10 + k)) for k in range(8)]
        rows += [(HOST, slot + k * minute, 1, 0, None) for k in (8, 9)]
        rows.append((HOST, slot - minute, 1, 1, 500.0))  # previous period's last cycle
        rows.append((HOST, slot + 10 * minute, 1, 1, 999.0))  # newest cycle, 20 s ago
        await conn.executemany(
            "INSERT INTO endpoint_probe_samples (endpoint_id, sampled_at, sent, received, rtt_avg_ms) "
            "VALUES ($1, $2, $3, $4, $5)",
            rows,
        )

    run_sql(seed)
    return now


def _with_session(timescale_dsn, fn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def run():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                await fn(session)
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _closed_row(conn):
    return await conn.fetchrow(
        "SELECT end_time, health_score, avg_rtt_ms FROM endpoint_events WHERE endpoint_id = $1", HOST
    )


def test_state_change_close_uses_the_period_without_the_confirming_cycle(timescale_dsn, run_sql):
    now = _seed(run_sql)
    from app.core.orm import text

    async def close(session):
        # The statement StateMachine.process runs when a new state is confirmed.
        await session.execute(
            text(
                "UPDATE endpoint_events SET end_time = :execution_time, "
                + closed_event_stats_sql(":execution_time", closed_by_cycle=True)
                + " WHERE endpoint_id = CAST(:endpoint_id AS uuid) AND end_time IS NULL"
            ),
            {"execution_time": now, "endpoint_id": str(HOST)},
        )

    _with_session(timescale_dsn, close)
    row = run_sql(_closed_row)
    assert float(row["health_score"]) == 80.0  # 8 of 10 probes answered
    assert float(row["avg_rtt_ms"]) == 13.5  # mean of 10..17 ms


def test_engine_start_closes_open_events_with_every_sample_of_the_period(timescale_dsn, run_sql):
    _seed(run_sql)
    from monitoring.gap_handler import resolve_startup_state

    _with_session(timescale_dsn, resolve_startup_state)
    row = run_sql(_closed_row)
    assert row["end_time"] is not None
    assert float(row["health_score"]) == 80.0
    # No cycle confirmed a new state, so the newest sample is this period's: (10+..+17+999)/9.
    assert float(row["avg_rtt_ms"]) == round((sum(range(10, 18)) + 999) / 9, 2)
