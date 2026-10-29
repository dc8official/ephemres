"""Traffic exported by two routers counts once in an endpoint's views (real TimescaleDB).

app-a's Internet traffic crosses both the edge and the core router, and both export it. The
views used to add the two reports, so app-a showed twice its real traffic. Each conversation
now counts once per minute, as the largest report among the routers that saw it; a
conversation only one router saw (app-a to db-b, through the core router) still counts.
"""
import datetime
import uuid

import pytest

EDGE_ID = uuid.UUID("44444444-0000-0000-0000-0000000000e1")
CORE_ID = uuid.UUID("44444444-0000-0000-0000-0000000000e2")
HOST_A_ID = uuid.UUID("44444444-0000-0000-0000-0000000000a1")
HOST_B_ID = uuid.UUID("44444444-0000-0000-0000-0000000000b1")
HOST_A_IP, HOST_B_IP, INTERNET_IP = "10.0.1.10", "10.0.1.20", "52.94.0.1"
TCP, TOS_EF = 6, 184
MB = 1_000_000


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    m1, m2 = now - datetime.timedelta(minutes=2), now - datetime.timedelta(minutes=3)

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips)
            VALUES ($1, '10.0.0.1', 'edge-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER', '{10.0.0.1}'),
                   ($2, '10.0.0.2', 'core-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER', '{10.0.0.2}'),
                   ($3, $4::inet, 'app-a', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}'),
                   ($5, $6::inet, 'db-b', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}')
            """,
            EDGE_ID, CORE_ID, HOST_A_ID, HOST_A_IP, HOST_B_ID, HOST_B_IP,
        )
        flows = [
            # bucket, exporter, src_ep, dst_ep, src, dst, port, bytes, packets, flows, tos
            (m1, EDGE_ID, HOST_A_ID, None, HOST_A_IP, INTERNET_IP, 443, 6 * MB, 5_000, 10, 0),
            (m1, CORE_ID, HOST_A_ID, None, HOST_A_IP, INTERNET_IP, 443, 5_900_000, 4_900, 10, 0),
            (m1, EDGE_ID, None, HOST_A_ID, INTERNET_IP, HOST_A_IP, 443, 12 * MB, 9_000, 10, 0),
            (m1, CORE_ID, None, HOST_A_ID, INTERNET_IP, HOST_A_IP, 443, 12 * MB, 9_000, 10, 0),
            (m1, CORE_ID, HOST_A_ID, HOST_B_ID, HOST_A_IP, HOST_B_IP, 5432, 3 * MB, 3_000, 5, TOS_EF),
            (m2, EDGE_ID, HOST_A_ID, None, HOST_A_IP, INTERNET_IP, 443, 1 * MB, 1_000, 2, 0),
        ]
        await conn.executemany(
            """
            INSERT INTO flow_minute_rollups (bucket, exporter_id, src_endpoint_id, dst_endpoint_id,
                src_ip, dst_ip, protocol, dst_port, bytes, packets, flow_count, tcp_flags, tos)
            VALUES ($1, $2, $3, $4, $5::inet, $6::inet, 6, $7, $8, $9, $10, 24, $11)
            """,
            flows,
        )

    run_sql(seed)
    return m1, m2


def _data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _get(api_client, path, **params):
    return _data(api_client.get(f"/api/v1/bandwidth/{path}", params={"endpoint_id": str(HOST_A_ID), **params}))


def test_endpoint_series_counts_each_conversation_once(api_client, run_sql):
    m1, m2 = _seed(run_sql)
    points = {datetime.datetime.fromisoformat(p["timestamp"]): p for p in _get(api_client, "traffic-series", window="1h")["points"]}
    assert points[m1]["ingress_bps"] == pytest.approx(12 * MB * 8 / 60)
    assert points[m1]["egress_bps"] == pytest.approx(9 * MB * 8 / 60)  # 6 MB to the Internet + 3 MB to db-b
    assert points[m1]["flows_per_sec"] == pytest.approx(25 / 60, abs=0.01)
    assert points[m2]["egress_bps"] == pytest.approx(1 * MB * 8 / 60)


def test_endpoint_series_on_the_hourly_tier_counts_once(api_client, run_sql):
    _seed(run_sql)
    series = _get(api_client, "traffic-series", window="24h")
    assert series["bucket_seconds"] == 3600
    # The two minutes may fall in different hours; the totals are the same either way.
    assert sum(p["ingress_bps"] for p in series["points"]) == pytest.approx(12 * MB * 8 / 3600)
    assert sum(p["egress_bps"] for p in series["points"]) == pytest.approx(10 * MB * 8 / 3600)


def test_endpoint_conversations_and_top_talkers_count_once(api_client, run_sql):
    _seed(run_sql)
    convs = _get(api_client, "conversations", window="1h")
    assert [(c["src_ip"], c["dst_ip"], c["total_bytes"]) for c in convs["items"]] == [
        (INTERNET_IP, HOST_A_IP, 12 * MB),
        (HOST_A_IP, INTERNET_IP, 7 * MB),
        (HOST_A_IP, HOST_B_IP, 3 * MB),
    ]
    assert convs["items"][1]["total_packets"] == 6_000

    talkers = {t["ip_address"]: t for t in _get(api_client, "top-talkers", window="1h")["top_endpoints"]}
    assert (talkers[HOST_A_IP]["ingress_bytes"], talkers[HOST_A_IP]["egress_bytes"]) == (12 * MB, 10 * MB)
    assert talkers[HOST_A_IP]["flow_count"] == 27


def test_fleet_conversations_count_once(api_client, run_sql):
    _seed(run_sql)
    convs = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": "1h"}))
    assert [c["total_bytes"] for c in convs["items"]] == [12 * MB, 7 * MB, 3 * MB]


def test_endpoint_applications_and_qos_count_once(api_client, run_sql):
    _seed(run_sql)
    data = _get(api_client, "applications", window="1h")
    apps = {a["port"]: a["total_bytes"] for a in data["applications"]}
    assert apps == {443: 19 * MB, 5432: 3 * MB}
    assert data["total_bytes"] == 22 * MB
    qos = {q["dscp"]: q["total_bytes"] for q in data["qos_classes"]}
    assert qos == {0: 19 * MB, 46: 3 * MB}
    zones = {z["zone"]: z["total_bytes"] for z in data["traffic_zones"]}
    assert zones["North-South Ingress"] == 12 * MB
    assert zones["North-South Egress"] == 7 * MB
    assert zones["East-West (Internal)"] == 3 * MB


def test_one_router_view_still_shows_its_own_reports(api_client, run_sql):
    _seed(run_sql)
    core = _get(api_client, "conversations", window="1h", exporter_id=str(CORE_ID))
    assert [c["total_bytes"] for c in core["items"]] == [12 * MB, 5_900_000, 3 * MB]
