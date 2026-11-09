"""Availability reports when two endpoints' events start at the same instant (real TimescaleDB).

endpoint_events is keyed (id, start_time), but the model declared only start_time as its key.
The ORM identity map then treated two endpoints' events with the same start as one object:
the second row overwrote the first and both rows returned it. Live data is stamped to the
microsecond, but restored, imported or replayed data shares instants, and the report showed
0 % uptime for one device and the other's outage under its name.
"""
import asyncio
import datetime
import uuid

from app.services.report_compiler import compile_availability_report_data

CORE = uuid.UUID("99999999-0000-0000-0000-0000000000a1")
EDGE = uuid.UUID("99999999-0000-0000-0000-0000000000a2")


def test_events_starting_at_the_same_instant_stay_separate(timescale_dsn, run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    w0 = now - datetime.timedelta(days=1)
    t_down = w0 + datetime.timedelta(hours=6)
    t_up = t_down + datetime.timedelta(minutes=10)

    async def seed(conn):
        for eid, host, ip in ((CORE, "core-sw", "10.0.1.1"), (EDGE, "edge-fw", "10.0.0.3")):
            await conn.execute(
                """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
                   VALUES ($1, $2, $3, 'SWITCH', 'ACTIVE', $4)""",
                eid, ip, host, w0 - datetime.timedelta(days=10),
            )
        rows = []
        for eid in (CORE, EDGE):
            rows += [
                (eid, "UP", w0 - datetime.timedelta(hours=1), t_down, 1000, 0, 100.0, 1.5),
                (eid, "DOWN", t_down, t_up, 0, 30, 0.0, None),
                (eid, "UP", t_up, None, 2000, 0, 100.0, 1.5),
            ]
        await conn.executemany(
            """INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                   failed_count, health_score, avg_rtt_ms, start_time, end_time, duration_seconds)
               VALUES ($1, $2, $2, $5, $6, $7, $8, $3, $4, 0)""",
            rows,
        )

    run_sql(seed)

    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def compile_report():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                return await compile_availability_report_data(session, [CORE, EDGE], w0, now)
        finally:
            await engine.dispose()

    data = asyncio.run(compile_report())
    rows = {r["hostname"]: r for r in data["endpoints"]}
    for host in ("core-sw", "edge-fw"):
        assert rows[host]["incident_count"] == 1, host
        assert rows[host]["downtime_seconds"] == 600, host
        assert rows[host]["uptime_percentage"] > 99.0, host
        assert rows[host]["avg_rtt_ms"] == 1.5, host
    ledger = data["incidents"]
    assert sorted(i["hostname"] for i in ledger) == ["core-sw", "edge-fw"]
    assert len({i["incident_id"] for i in ledger}) == 2
