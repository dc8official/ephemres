"""Hours of WAN links with their busiest minute, and the usual band read from them (real TimescaleDB).

v3.3.6 build plan 7.5 step 3. interface_traffic.link_hours reads the hourly interface
aggregates with the SNMP-else-flow rule; each hour keeps its busiest minute (migrations 0016 and
0027), which the per-link charts draw above the average. traffic_usual.usual_for_links builds the
usual band from those hours with one query for every link.

In a private database: test_report_interface_history refreshes flow_interface_hourly_rollups in
the shared one, and rows below the watermark it leaves are hidden from the real-time part. The
private database's own refresh policy can do the same, so each test settles the aggregates after
seeding (conftest.settle_interface_hourly).
"""
import asyncio
import datetime
import os
import uuid

import pytest

R1 = uuid.UUID("55555555-0000-0000-0000-0000000000f1")
MB = 1_000_000
UTC = datetime.timezone.utc


@pytest.fixture(scope="module")
def private_dsn():
    from tests.integration.conftest import DSN_ENV, _create_and_migrate, _drop

    admin_dsn = os.environ.get(DSN_ENV)
    if not admin_dsn:
        pytest.skip(f"{DSN_ENV} not set")
    name = f"ephemres_it_{uuid.uuid4().hex[:8]}"
    dsn = asyncio.run(_create_and_migrate(admin_dsn, name))
    try:
        yield dsn
    finally:
        asyncio.run(_drop(admin_dsn, name))


def _execute(dsn, fn):
    import asyncpg

    from tests.integration.conftest import reset_interface_traffic, settle_interface_hourly

    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            await reset_interface_traffic(conn)
            await conn.execute(
                "INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status) "
                "VALUES ($1, '10.0.0.254', 'r1', 'Router', 'ACTIVE')",
                R1,
            )
            result = await fn(conn)
            await settle_interface_hourly(conn)
            return result
        finally:
            await conn.close()

    return asyncio.run(go())


def _with_db(dsn, fn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def inner():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(inner())


FLOW_SQL = """INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                  in_packets, out_packets, flow_count) VALUES ($1, $2, $3, $4, $5, 10, 10, 3)"""
SNMP_SQL = """INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                  in_packets, out_packets) VALUES ($1, $2, $3, $4, $5, 10, 10)"""


def test_hours_carry_their_busiest_minute_from_snmp_or_flows(private_dsn):
    from app.services.interface_traffic import link_hours

    hour = datetime.datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - datetime.timedelta(hours=3)
    earlier = hour - datetime.timedelta(hours=1)
    minute = lambda h, m: h + datetime.timedelta(minutes=m)

    async def seed(conn):
        # Link 1: flows every minute of both hours; SNMP counters only in the later hour, with a
        # 9 MB burst in minute 5. Link 2: flows only, a 4 MB burst in minute 7.
        await conn.executemany(FLOW_SQL, [(minute(h, m), R1, 1, 1 * MB, MB // 2) for h in (earlier, hour) for m in range(10)])
        await conn.executemany(SNMP_SQL, [(minute(hour, m), R1, 1, 9 * MB if m == 5 else 1_200_000, 600_000) for m in range(10)])
        await conn.executemany(FLOW_SQL, [(minute(hour, m), R1, 2, 4 * MB if m == 7 else 1 * MB, 0) for m in range(10)])

    _execute(private_dsn, seed)
    hours = _with_db(private_dsn, lambda db: link_hours(db, [(R1, 1), (R1, 2)], earlier, hour + datetime.timedelta(hours=1)))

    one = hours[(R1, 1)]
    assert [(h.bucket, h.source) for h in one] == [(earlier, "flow"), (hour, "snmp")]
    # The hour with counters uses them: total and busiest minute from SNMP, not flows.
    assert (one[1].in_bytes, one[1].peak_in_minute_bytes) == (9 * 1_200_000 + 9 * MB, 9 * MB)
    assert (one[1].out_bytes, one[1].peak_out_minute_bytes) == (10 * 600_000, 600_000)
    assert (one[0].in_bytes, one[0].peak_in_minute_bytes) == (10 * MB, 1 * MB)

    two = hours[(R1, 2)]
    assert [(h.source, h.in_bytes, h.peak_in_minute_bytes) for h in two] == [("flow", 13 * MB, 4 * MB)]


def test_no_links_no_query(private_dsn):
    from app.services.interface_traffic import link_hours

    now = datetime.datetime.now(UTC)
    assert _with_db(private_dsn, lambda db: link_hours(db, [], now - datetime.timedelta(hours=1), now)) == {}


def test_usual_band_from_the_database(private_dsn):
    """Three recent weekdays with traffic at 10:00 UTC: their 10:00 cell has a band (min to max of
    3 samples), read with one query for two links; a link with no rows has none."""
    from app.services.traffic_usual import WEEKDAY_WEEKEND, Band, usual_for_links

    now = datetime.datetime.now(UTC)
    days, d = [], now.date()
    while len(days) < 3:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            days.append(d)
    rates_mbps = (8, 12, 10)

    async def seed(conn):
        rows = []
        for day, rate in zip(days, rates_mbps):
            ten = datetime.datetime(day.year, day.month, day.day, 10, tzinfo=UTC)
            # A whole hour at `rate` Mb/s: rate * 60 / 8 MB a minute.
            rows += [(ten + datetime.timedelta(minutes=m), R1, 1, rate * MB * 60 // 8, 0) for m in range(60)]
        await conn.executemany(FLOW_SQL, rows)

    _execute(private_dsn, seed)
    usual = _with_db(private_dsn, lambda db: usual_for_links(db, [(R1, 1), (R1, 2)], now=now, tz=UTC))

    link = usual[(R1, 1)]
    assert (link.days, link.grouping) == (3, WEEKDAY_WEEKEND)
    next_weekday_ten = datetime.datetime(days[0].year, days[0].month, days[0].day, 10, 30, tzinfo=UTC)
    assert link.band(next_weekday_ten) == Band(8 * MB, 12 * MB, 3)
    assert usual[(R1, 2)].grouping is None
