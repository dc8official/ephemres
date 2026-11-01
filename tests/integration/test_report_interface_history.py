"""PDF bandwidth reports longer than a week read the hourly interface rollup (real TimescaleDB).

flow_interface_minute_rollups is kept 7 days. The report read only that table, so a 30-day
report summed the last week and averaged it over 30 days. A report reaching further back now
reads flow_interface_hourly_rollups (kept 365 days), which keeps each hour's busiest minute.

The router saw 36 MB in one minute 20 days ago (its minute rows since dropped by retention,
the hour kept in the hourly rollup) and 12 MB in one minute yesterday.
"""
import asyncio
import datetime
import json
import uuid

from app.services.report_compiler import _interface_totals, compile_bandwidth_report_data, format_bytes

ROUTER_ID = uuid.UUID("88888888-0000-0000-0000-0000000000e1")
MB = 1_000_000


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    old, recent = now - datetime.timedelta(days=20), now - datetime.timedelta(days=1)

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips, flow_interface_aliases)
            VALUES ($1, '10.0.8.1', 'history-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.0.8.1}', $2::jsonb)
            """,
            ROUTER_ID, json.dumps({"1": {"name": "Uplink", "speed_mbps": 1000, "role": "wan"}}),
        )
        await conn.executemany(
            """
            INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx,
                in_bytes, out_bytes, in_packets, out_packets, flow_count)
            VALUES ($1, $2, 1, $3, $4, 1000, 1000, 10)
            """,
            [(old, ROUTER_ID, 36 * MB, 6 * MB), (recent, ROUTER_ID, 12 * MB, 2 * MB)],
        )
        await conn.execute("CALL refresh_continuous_aggregate('flow_interface_hourly_rollups', NULL, NULL)")
        # What the 7-day retention policy does to the minute table.
        await conn.execute(
            "DELETE FROM flow_interface_minute_rollups WHERE bucket < now() - INTERVAL '7 days'"
        )

    run_sql(seed)
    return now


def _cleanup(run_sql):
    async def cleanup(conn):
        await conn.execute("DELETE FROM flow_interface_minute_rollups WHERE exporter_id = $1", ROUTER_ID)
        await conn.execute("CALL refresh_continuous_aggregate('flow_interface_hourly_rollups', NULL, NULL)")

    run_sql(cleanup)


def _with_session(timescale_dsn, fn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def run():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_thirty_day_report_includes_traffic_older_than_a_week(timescale_dsn, run_sql):
    now = _seed(run_sql)
    try:
        start = now - datetime.timedelta(days=30)
        rows = _with_session(timescale_dsn, lambda s: _interface_totals(s, [ROUTER_ID], start, now))
        assert [tuple(r) for r in rows] == [(ROUTER_ID, 1, 48 * MB, 8 * MB, 36 * MB, 6 * MB)]

        data = _with_session(timescale_dsn, lambda s: compile_bandwidth_report_data(s, [ROUTER_ID], start, now))
        assert data["exporters"][0]["formatted_ingress_bytes"] == format_bytes(48 * MB)
        assert data["wan_edge"]["formatted_in_bytes"] == format_bytes(48 * MB)
    finally:
        _cleanup(run_sql)


def test_one_day_report_reads_the_minute_table(timescale_dsn, run_sql):
    now = _seed(run_sql)
    try:
        start = now - datetime.timedelta(days=2)
        rows = _with_session(timescale_dsn, lambda s: _interface_totals(s, [ROUTER_ID], start, now))
        assert [tuple(r) for r in rows] == [(ROUTER_ID, 1, 12 * MB, 2 * MB, 12 * MB, 2 * MB)]
    finally:
        _cleanup(run_sql)
