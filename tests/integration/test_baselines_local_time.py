"""ICMP baselines by the server's local hour of the week (decided 2026-11-15, build plan 11b).

The baselines were grouped by UTC hour: on a server in a summer-time zone, "Monday 09:00" moved by
an hour twice a year. The data stays in UTC hour buckets (node_historical_baselines_cagg); the
cache now labels each bucket in the server's zone (timezone_utils.server_zone, from TZ here).
"""
import asyncio
import datetime
import uuid

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

UTC = datetime.timezone.utc
EP = uuid.UUID("55555555-0000-0000-0000-0000000000b1")


def _a_monday_late_evening_utc():
    """A Monday 23:10 UTC between 8 and 14 days ago: inside the aggregate's 28 days."""
    day = datetime.datetime.now(UTC).replace(hour=23, minute=10, second=0, microsecond=0) - datetime.timedelta(days=8)
    while day.weekday() != 0:
        day -= datetime.timedelta(days=1)
    return day


def _seed(run_sql, at):
    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
               VALUES ($1, '10.70.0.1', 'lagos-ap', 'Switch', 'ACTIVE', now() - interval '60 days')""", EP,
        )
        await conn.execute(
            """INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                   failed_count, health_score, avg_rtt_ms, is_split_event, start_time, end_time,
                   duration_seconds, monitoring_cycle_count)
               VALUES ($1, 'UP', 'UP', 240, 0, 100, 40, false, $2, $3, 600, 10)""",
            EP, at, at + datetime.timedelta(minutes=10),
        )
        # Materialise the hours just written (the policy would, later).
        await conn.execute("CALL refresh_continuous_aggregate('node_historical_baselines_cagg', NULL, NULL)")

    run_sql(seed)


def _slots(dsn, zone, monkeypatch):
    from app.services.baseline_service import BaselineCache

    monkeypatch.setenv("TZ", zone)

    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                cache = BaselineCache()
                await cache.refresh_from_db(session)
                arr = cache._cache.get(EP) or []
                return {i: v[0] for i, v in enumerate(arr) if v is not None}, cache
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_the_hour_of_the_week_is_the_servers_local_hour(timescale_dsn, run_sql, monkeypatch):
    at = _a_monday_late_evening_utc()
    _seed(run_sql, at)

    # Monday 23:10 UTC is Monday 23:00 in UTC, Tuesday 00:00 in Lagos (UTC+1, no summer time),
    # and Tuesday 10:00 in Sydney in November (UTC+11, summer time).
    utc, _ = _slots(timescale_dsn, "UTC", monkeypatch)
    lagos, cache = _slots(timescale_dsn, "Africa/Lagos", monkeypatch)
    sydney, _ = _slots(timescale_dsn, "Australia/Sydney", monkeypatch)
    sydney_hour = (at.astimezone(datetime.timezone(datetime.timedelta(hours=11))) if at.month in (11, 12, 1, 2)
                   else at).hour
    assert utc == {0 * 24 + 23: 40.0}
    assert lagos == {1 * 24 + 0: 40.0}
    if at.month in (11, 12, 1, 2):
        assert sydney == {1 * 24 + sydney_hour: 40.0}

    # The lookup takes the same local hour: an aware time is converted to the server's zone.
    monkeypatch.setenv("TZ", "Africa/Lagos")
    assert cache.get_baseline(EP, at)[0] == 40.0
    assert cache.get_baseline(EP, at - datetime.timedelta(hours=1)) is None


def test_without_the_aggregate_the_events_are_grouped_the_same_way(timescale_dsn, run_sql, monkeypatch):
    from app.services import baseline_service

    at = _a_monday_late_evening_utc()
    _seed(run_sql, at)
    monkeypatch.setenv("TZ", "Africa/Lagos")

    async def go():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                res = await session.execute(*baseline_service._local_hours_query(False))
                return [(r.day_of_week, r.hour_of_day, r.historical_mean) for r in res.fetchall()]
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == [(2, 0, 40.0)]  # ISO Tuesday, 00:00 in Lagos
