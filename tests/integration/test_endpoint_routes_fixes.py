"""Endpoint list and detail uptime, and clearing a manual parent (real TimescaleDB).

- The 24 h uptime on the endpoint list and detail counted time inside a monitoring gap as UP: a
  database outage leaves the open period running across its gap. The denominator already left
  the gap out, so uptime read high. The Reports page and the PDF leave it out of both.
- PATCH /endpoints/{id} treated a null manual_parent_id as "not sent", so a parent could be set
  but never removed.
"""
import datetime
import uuid

import pytest

HOST = uuid.UUID("88888888-0000-0000-0000-0000000000a1")
PARENT = uuid.UUID("88888888-0000-0000-0000-0000000000a2")


def _seed_endpoint(conn, endpoint_id, ip, hostname, created_at):
    return conn.execute(
        """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, created_at)
           VALUES ($1, $2, $3, 'ROUTER', 'ACTIVE', $4)""",
        endpoint_id, ip, hostname, created_at,
    )


def test_list_and_detail_uptime_leave_gap_time_out(run_sql, api_client):
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    w0 = now - datetime.timedelta(days=1)
    gap_start, gap_end = w0 + datetime.timedelta(hours=2), w0 + datetime.timedelta(hours=3)
    t_down = w0 + datetime.timedelta(hours=6)
    t_up = t_down + datetime.timedelta(hours=3)

    async def seed(conn):
        await _seed_endpoint(conn, HOST, "10.0.8.1", "branch-rtr", w0 - datetime.timedelta(days=10))
        await conn.executemany(
            """INSERT INTO endpoint_events (endpoint_id, operational_state, detailed_state, success_count,
                   failed_count, health_score, avg_rtt_ms, start_time, end_time, duration_seconds)
               VALUES ($1, $2, $2, 0, 0, 100.0, 1.5, $3, $4, 0)""",
            [
                # Up across the database outage, down for 3 h, then up again.
                (HOST, "UP", w0 - datetime.timedelta(hours=1), t_down),
                (HOST, "DOWN", t_down, t_up),
                (HOST, "UP", t_up, None),
            ],
        )
        await conn.execute(
            """INSERT INTO monitoring_service_events (event_type, start_time, end_time, duration_seconds)
               VALUES ('DATABASE_DISCONNECT', $1, $2, 3600)""",
            gap_start, gap_end,
        )

    run_sql(seed)

    # 24 h window: 1 h unknown (the gap), 3 h down, the other 20 h up. The route's window ends a
    # few seconds after the seeded "now", so allow a little drift.
    expected = 100.0 * 20 / 23
    listed = api_client.get("/api/v1/endpoints/").json()["data"]
    row = next(e for e in listed if e["id"] == str(HOST))
    assert row["uptime_percentage_24h"] == pytest.approx(expected, abs=0.05)

    detail = api_client.get(f"/api/v1/endpoints/{HOST}").json()["data"]
    assert detail["uptime_percentage_24h"] == pytest.approx(expected, abs=0.05)


def test_patch_can_clear_a_manual_parent(run_sql, api_client):
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)

    async def seed(conn):
        await _seed_endpoint(conn, PARENT, "10.0.8.254", "core-sw", created)
        await _seed_endpoint(conn, HOST, "10.0.8.1", "branch-rtr", created)

    run_sql(seed)

    resp = api_client.patch(f"/api/v1/endpoints/{HOST}", json={"manual_parent_id": str(PARENT)})
    assert resp.status_code == 200
    assert api_client.get(f"/api/v1/endpoints/{HOST}").json()["data"]["manual_parent_id"] == str(PARENT)

    # Leaving the field out changes nothing...
    assert api_client.patch(f"/api/v1/endpoints/{HOST}", json={"location": "Lagos"}).status_code == 200
    assert api_client.get(f"/api/v1/endpoints/{HOST}").json()["data"]["manual_parent_id"] == str(PARENT)

    # ...and an explicit null removes the parent.
    assert api_client.patch(f"/api/v1/endpoints/{HOST}", json={"manual_parent_id": None}).status_code == 200
    assert api_client.get(f"/api/v1/endpoints/{HOST}").json()["data"]["manual_parent_id"] is None
