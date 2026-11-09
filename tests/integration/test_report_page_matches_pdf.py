"""The Reports page's figures agree with the PDF availability report (real TimescaleDB).

Two things went wrong on the page side:
- GET /reports/incidents gave each outage 60 s per endpoint_events row, as if rows were one
  minute each. A row is a whole period, so a 3-hour outage stored as one row read as 60 s.
- The fleet summary and GET /reports/uptime counted time inside a monitoring gap as up or down.
  A database outage leaves the open period running across its gap (DATABASE_DISCONNECT), and
  that hour was credited as uptime, while the PDF counts it as neither, as the uptime
  denominator already did.
"""
import asyncio
import datetime
import uuid

from app.services.report_compiler import compile_availability_report_data

HOST = uuid.UUID("77777777-0000-0000-0000-0000000000a1")


def _iso(dt: datetime.datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def test_page_figures_match_the_pdf(timescale_dsn, run_sql, api_client):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    w0 = now - datetime.timedelta(days=1)
    gap_start, gap_end = w0 + datetime.timedelta(hours=2), w0 + datetime.timedelta(hours=3)
    t_down = w0 + datetime.timedelta(hours=6)
    t_up = t_down + datetime.timedelta(hours=3)

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
               VALUES ($1, '10.0.7.1', 'branch-rtr', 'ROUTER', 'ACTIVE', $2)""",
            HOST, w0 - datetime.timedelta(days=10),
        )
        await conn.executemany(
            """INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                   failed_count, health_score, avg_rtt_ms, start_time, end_time, duration_seconds)
               VALUES ($1, $2, $2, $5, $6, $7, $8, $3, $4, 0)""",
            [
                # Up across the database outage, down for 3 h in one row, then up again.
                (HOST, "UP", w0 - datetime.timedelta(hours=1), t_down, 1000, 0, 100.0, 1.5),
                (HOST, "DOWN", t_down, t_up, 0, 900, 0.0, None),
                (HOST, "UP", t_up, None, 2000, 0, 100.0, 1.5),
            ],
        )
        await conn.execute(
            """INSERT INTO monitoring_service_events (event_type, start_time, end_time, duration_seconds)
               VALUES ('DATABASE_DISCONNECT', $1, $2, 3600)""",
            gap_start, gap_end,
        )

    run_sql(seed)

    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def compile_pdf_data():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                return await compile_availability_report_data(session, [HOST], w0, now)
        finally:
            await engine.dispose()

    pdf_row = asyncio.run(compile_pdf_data())["endpoints"][0]
    # 24 h window: 3 h down, 1 h unknown (the gap), the other 20 h up.
    assert pdf_row["downtime_seconds"] == 3 * 3600
    expected_pct = round(100.0 * 20 / 23, 2)
    assert pdf_row["uptime_percentage"] == expected_pct

    window = {"start_date": _iso(w0), "end_date": _iso(now)}

    fleet = api_client.get("/api/v1/reports/fleet-summary", params=window).json()["data"]
    row = next(e for e in fleet["endpoints"] if e["id"] == str(HOST))
    assert row["uptime_seconds"] == 20 * 3600
    assert row["downtime_seconds"] == 3 * 3600
    assert row["uptime_percentage"] == expected_pct
    assert row["incident_count"] == 1

    uptime = api_client.get(f"/api/v1/reports/uptime/{HOST}", params=window).json()["data"]
    assert uptime["uptime_seconds"] == 20 * 3600
    assert uptime["downtime_seconds"] == 3 * 3600
    assert uptime["unknown_seconds"] == 3600
    assert uptime["uptime_percentage"] == expected_pct

    incidents = api_client.get(f"/api/v1/reports/incidents/{HOST}", params=window).json()["data"]
    assert len(incidents) == 1
    assert incidents[0]["duration_seconds"] == 3 * 3600


def test_fleet_summary_without_gaps(run_sql, api_client):
    """No monitoring gap in the window: the gap arrays sent to the query are empty."""
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    w0 = now - datetime.timedelta(days=1)
    t_down = w0 + datetime.timedelta(hours=6)

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
               VALUES ($1, '10.0.7.1', 'branch-rtr', 'ROUTER', 'ACTIVE', $2)""",
            HOST, w0 - datetime.timedelta(days=10),
        )
        await conn.executemany(
            """INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                   failed_count, health_score, avg_rtt_ms, start_time, end_time, duration_seconds)
               VALUES ($1, $2, $2, 0, 0, 100.0, NULL, $3, $4, 0)""",
            [
                (HOST, "UP", w0 - datetime.timedelta(hours=1), t_down),
                (HOST, "DOWN", t_down, None),
            ],
        )

    run_sql(seed)
    params = {"start_date": _iso(w0), "end_date": _iso(now)}
    row = api_client.get("/api/v1/reports/fleet-summary", params=params).json()["data"]["endpoints"][0]
    assert row["uptime_seconds"] == 6 * 3600
    assert row["downtime_seconds"] == 18 * 3600
    assert row["incident_count"] == 1

    incident = api_client.get(f"/api/v1/reports/incidents/{HOST}", params=params).json()["data"][0]
    # Still down: no end, and the duration runs to now.
    assert incident["incident_end"] is None
    assert incident["duration_seconds"] >= 18 * 3600
