"""Adding an endpoint whose IP was deleted restores the old row (real TimescaleDB).

The add form never sends flow_exporter_ips or flow_interface_aliases, and the restore
UPDATE wrote their None as NULL into NOT NULL columns, so re-adding any deleted endpoint
from the UI failed with a 500.
"""
import uuid

OLD = uuid.UUID("55555555-0000-0000-0000-0000000000d1")


def test_readding_a_deleted_endpoint_restores_it(run_sql, api_client):
    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, deleted_at)
            VALUES ($1, '10.9.3.1', 'gone-host', 'Server', 'DELETED', now())
            """,
            OLD,
        )

    run_sql(seed)
    resp = api_client.post(
        "/api/v1/endpoints/",
        json={"ip_address": "10.9.3.1", "hostname": "back-host", "device_type": "SERVER"},
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["data"]["id"] == str(OLD)

    async def read(conn):
        return await conn.fetchrow(
            "SELECT hostname, endpoint_status, flow_exporter_ips, flow_interface_aliases FROM endpoints WHERE id = $1",
            OLD,
        )

    row = run_sql(read)
    assert row["endpoint_status"] == "ACTIVE" and row["hostname"] == "back-host"
    assert row["flow_exporter_ips"] == [] and row["flow_interface_aliases"] in ({}, "{}")
