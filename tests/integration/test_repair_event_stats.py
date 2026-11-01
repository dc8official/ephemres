"""The optional audit-log correction for periods closed before v3.3.5 (real TimescaleDB).

Before v3.3.5 a closed period kept its last cycle's health and RTT. The correction recomputes
them with the engine's own formulas: health from the counts (all history), RTT only where the
period's probe samples are all still there, NULL for a period without a single reply. Periods
that are already right are not written, and a dry run writes nothing.
"""
import asyncio
import datetime
import uuid

import pytest

from monitoring import repair_event_stats

OLD_HOST = uuid.UUID("66666666-0000-0000-0000-0000000000a1")  # history from before v3.3.5, no samples
NEW_HOST = uuid.UUID("66666666-0000-0000-0000-0000000000b1")  # probe samples cover its periods


@pytest.fixture
def sessions(timescale_dsn, monkeypatch):
    """Point the tool's AsyncSessionLocal at the test database (one engine per session)."""
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    url = timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1)

    class Session:
        async def __aenter__(self):
            self.engine = create_async_engine(url, pool_size=1, max_overflow=0)
            self.session = async_sessionmaker(bind=self.engine, class_=AsyncSession)()
            return await self.session.__aenter__()

        async def __aexit__(self, *exc):
            try:
                await self.session.__aexit__(*exc)
            finally:
                await self.engine.dispose()

    monkeypatch.setattr(repair_event_stats, "AsyncSessionLocal", Session)


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    minute = datetime.timedelta(minutes=1)
    old_start = now - datetime.timedelta(days=60)
    new_start = now - datetime.timedelta(minutes=30)
    new_change = new_start + 10 * minute

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
            VALUES ($1, '10.9.3.1', 'old-host', 'Server', 'ACTIVE'), ($2, '10.9.3.2', 'new-host', 'Server', 'ACTIVE')
            """,
            OLD_HOST, NEW_HOST,
        )
        rows = [
            # endpoint, state, success, failed, health, rtt, start, end
            # UP for 25 cycles that ended in an outage: kept the failed last cycle.
            (OLD_HOST, "UP", 96, 4, 0, None, old_start, old_start + 25 * minute),
            # DOWN without a single reply: kept the first good probe of the recovery.
            (OLD_HOST, "DOWN", 0, 12, 100, 12.5, old_start + 25 * minute, old_start + 28 * minute),
            # Already right (as v3.3.5 closes them): left alone.
            (OLD_HOST, "UP", 40, 0, 100, 9.0, old_start + 28 * minute, old_start + 38 * minute),
            # Samples cover this one; it was closed by the DOWN below, whose first cycle is not its own.
            (NEW_HOST, "UP", 36, 4, 0, None, new_start, new_change),
            (NEW_HOST, "DOWN", 0, 8, 0, None, new_change, now - minute),
        ]
        await conn.executemany(
            """
            INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                duration_seconds, monitoring_cycle_count)
            VALUES ($1, $2, $2, $3, $4, $5, $6, false, $7, $8, 0, 1)
            """,
            rows,
        )
        # One sample a minute, stamped 40 s before the cycle's event time.
        samples = [(NEW_HOST, new_start - datetime.timedelta(seconds=40) + k * minute, 4, 4, 10.0 + k) for k in range(9)]
        samples.append((NEW_HOST, new_start - datetime.timedelta(seconds=40) + 9 * minute, 4, 0, None))
        samples.append((NEW_HOST, new_start - datetime.timedelta(seconds=40) + 10 * minute, 4, 0, None))  # confirmed DOWN
        await conn.executemany(
            "INSERT INTO endpoint_probe_samples (endpoint_id, sampled_at, sent, received, rtt_avg_ms) "
            "VALUES ($1, $2, $3, $4, $5)",
            samples,
        )

    run_sql(seed)


async def _periods(conn):
    rows = await conn.fetch(
        "SELECT endpoint_id, health_score, avg_rtt_ms FROM endpoint_events ORDER BY endpoint_id, start_time"
    )
    return [(r["endpoint_id"], float(r["health_score"]), None if r["avg_rtt_ms"] is None else float(r["avg_rtt_ms"])) for r in rows]


def test_dry_run_reports_without_writing(run_sql, sessions):
    _seed(run_sql)
    before = run_sql(_periods)
    assert asyncio.run(repair_event_stats.needed())

    totals = asyncio.run(repair_event_stats.check())
    assert (totals["periods"], totals["health"], totals["rtt"]) == (3, 3, 2)
    assert run_sql(_periods) == before

    report = repair_event_stats._report(totals)
    assert "nothing has been changed" in report and "--apply" in report


def test_apply_corrects_with_the_engine_formulas(run_sql, sessions):
    _seed(run_sql)
    assert asyncio.run(repair_event_stats.apply(progress=lambda line: None)) == 3

    assert run_sql(_periods) == [
        (OLD_HOST, 96.0, None),  # health from the counts; no samples, so the RTT stays as it was
        (OLD_HOST, 0.0, None),  # no reply in the whole period: no RTT
        (OLD_HOST, 100.0, 9.0),  # untouched
        (NEW_HOST, 90.0, 14.0),  # mean of 10..18 ms; the failed cycles have no RTT
        (NEW_HOST, 0.0, None),  # already right
    ]

    # Everything is right now: nothing left to do, and running again writes nothing.
    assert not asyncio.run(repair_event_stats.needed())
    assert asyncio.run(repair_event_stats.check())["periods"] == 0
    assert asyncio.run(repair_event_stats.apply(progress=lambda line: None)) == 0


def test_since_limits_the_correction(run_sql, sessions):
    _seed(run_sql)
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
    assert asyncio.run(repair_event_stats.apply(since, progress=lambda line: None)) == 1
    assert run_sql(_periods)[0] == (OLD_HOST, 0.0, None)


def test_corrects_periods_in_compressed_storage(run_sql, sessions):
    _seed(run_sql)

    async def compress(conn):
        return await conn.fetchval(
            "SELECT COUNT(compress_chunk(c, if_not_compressed => true)) "
            "FROM show_chunks('endpoint_events', older_than => INTERVAL '7 days') c"
        )

    assert run_sql(compress) >= 1
    totals = asyncio.run(repair_event_stats.check())
    assert totals["compressed"] == 2  # the two old periods to correct
    assert "compressed storage" in repair_event_stats._report(totals)

    assert asyncio.run(repair_event_stats.apply(progress=lambda line: None)) == 3
    assert run_sql(_periods)[:2] == [(OLD_HOST, 96.0, None), (OLD_HOST, 0.0, None)]
