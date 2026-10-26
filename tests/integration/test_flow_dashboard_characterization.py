"""Characterization of the flow dashboard endpoints against real TimescaleDB SQL.

The dataset is small enough to compute by hand; the expected values below are those
hand calculations. Assertions marked CHARACTERIZATION pin current behaviour that the v3.4 plan
(docs/architecture/v3.4-dashboard-data-and-design-plan.md) intends to change.
"""
import datetime
import json
import uuid

import pytest

ROUTER_ID = uuid.UUID("11111111-0000-0000-0000-0000000000a1")
HOST_A_ID = uuid.UUID("11111111-0000-0000-0000-0000000000a2")
HOST_B_ID = uuid.UUID("11111111-0000-0000-0000-0000000000a3")

HOST_A_IP = "10.0.1.10"
HOST_B_IP = "10.0.1.20"
INTERNET_IP = "52.94.0.1"
SCANNER_IP = "185.220.101.4"
TRANSIT_SRC_IP = "8.8.8.8"
TRANSIT_DST_IP = "1.1.1.1"

TCP, UDP = 6, 17
TOS_EF = 184  # DSCP 46

ROUTER_ALIASES = {
    "1": {"name": "WAN", "speed_mbps": 100},
    "2": "LAN",  # legacy string alias -> 1 Gbps default speed
    "3": {"name": "DMZ", "speed_mbps": 10000},  # configured, no traffic
}


def _minute_buckets():
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    # Both buckets fall inside the 5-minute overview window and the 1h window.
    return now - datetime.timedelta(minutes=2), now - datetime.timedelta(minutes=3)


def _seed(run_sql):
    m1, m2 = _minute_buckets()

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips,
                                   flow_interface_aliases)
            VALUES ($1, '10.0.0.1', 'edge-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.0.0.1}', $2::jsonb),
                   ($3, $4::inet, 'app-a', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}', '{}'),
                   ($5, $6::inet, 'db-b', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}', '{}')
            """,
            ROUTER_ID, json.dumps(ROUTER_ALIASES), HOST_A_ID, HOST_A_IP, HOST_B_ID, HOST_B_IP,
        )
        flows = [
            # bucket, src_ep, dst_ep, src, dst, proto, port, bytes, packets, flows, flags, tos
            (m1, HOST_A_ID, None, HOST_A_IP, INTERNET_IP, TCP, 443, 6_000_000, 5_000, 10, 0x1A, 0),
            (m1, None, HOST_A_ID, INTERNET_IP, HOST_A_IP, TCP, 443, 12_000_000, 9_000, 10, 0x18, 0),
            (m1, HOST_A_ID, HOST_B_ID, HOST_A_IP, HOST_B_IP, TCP, 5432, 3_000_000, 3_000, 5, 0x18, TOS_EF),
            (m1, None, HOST_B_ID, SCANNER_IP, HOST_B_IP, TCP, 22, 3_000, 50, 50, 0x02, 0),
            (m2, None, None, TRANSIT_SRC_IP, TRANSIT_DST_IP, UDP, 53, 1_200_000, 1_000, 20, 0, 0),
        ]
        await conn.executemany(
            """
            INSERT INTO flow_minute_rollups (bucket, exporter_id, src_endpoint_id, dst_endpoint_id,
                src_ip, dst_ip, protocol, dst_port, bytes, packets, flow_count, tcp_flags, tos)
            VALUES ($1, $2, $3, $4, $5::inet, $6::inet, $7, $8, $9, $10, $11, $12, $13)
            """,
            [(f[0], ROUTER_ID, *f[1:]) for f in flows],
        )
        await conn.executemany(
            """
            INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx,
                in_bytes, out_bytes, in_packets, out_packets, flow_count)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            [
                (m1, ROUTER_ID, 1, 9_000_000, 3_000_000, 7_000, 2_000, 30),
                (m1, ROUTER_ID, 2, 3_000_000, 9_000_000, 2_000, 7_000, 25),
            ],
        )

    run_sql(seed)
    return m1, m2


def _data(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    return body["data"]


def test_overview_counts_last_five_minutes(api_client, run_sql):
    _seed(run_sql)
    ov = _data(api_client.get("/api/v1/bandwidth/overview"))

    # Ingress = rows whose destination is a known endpoint, egress = rows whose source is.
    # CHARACTERIZATION: the A->B row (3 MB) counts in both, so ingress + egress exceed the total.
    assert ov["total_ingress_bps"] == pytest.approx(15_003_000 * 8 / 300)
    assert ov["total_egress_bps"] == pytest.approx(9_000_000 * 8 / 300)
    assert ov["total_ingress_pps"] == pytest.approx(12_050 / 300, abs=0.01)
    assert ov["total_egress_pps"] == pytest.approx(8_000 / 300, abs=0.01)
    assert ov["avg_packet_size"] == 22_203_000 // 18_050
    assert ov["total_flows_count"] == 95
    assert ov["active_exporters_count"] == 1
    assert ov["anomaly_flows_count"] == 50  # SYN-only scanner row


def test_fleet_series_splits_unattributed_transit_evenly(api_client, run_sql):
    m1, m2 = _seed(run_sql)
    points = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "1h"}))["points"]
    by_ts = {datetime.datetime.fromisoformat(p["timestamp"]): p for p in points}
    assert set(by_ts) == {m1, m2}

    p1 = by_ts[m1]
    assert p1["ingress_bps"] == pytest.approx(15_003_000 * 8 / 60)
    assert p1["egress_bps"] == pytest.approx(9_000_000 * 8 / 60)
    assert p1["flows_per_sec"] == pytest.approx(75 / 60, abs=0.01)

    # CHARACTERIZATION: a bucket holding only transit traffic (no known endpoint on either
    # side) is split 50/50 into ingress and egress. Phase 1 reports it as transit instead.
    p2 = by_ts[m2]
    assert p2["ingress_bps"] == pytest.approx(600_000 * 8 / 60)
    assert p2["egress_bps"] == pytest.approx(600_000 * 8 / 60)


def test_hourly_window_lags_behind_fresh_data(api_client, run_sql):
    _seed(run_sql)
    # CHARACTERIZATION: flow_hourly_rollups is a materialized-only continuous aggregate, so
    # minutes that have not been refreshed yet are invisible to the 24h window. Phase 2
    # (migration 0014) enables real-time aggregation and this list stops being empty.
    points = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "24h"}))["points"]
    assert points == []


def test_exporter_series_reads_interface_rollups(api_client, run_sql):
    m1, _ = _seed(run_sql)
    params = {"window": "1h", "exporter_id": str(ROUTER_ID)}
    points = _data(api_client.get("/api/v1/bandwidth/traffic-series", params=params))["points"]
    assert len(points) == 1
    assert datetime.datetime.fromisoformat(points[0]["timestamp"]) == m1
    assert points[0]["ingress_bps"] == pytest.approx(12_000_000 * 8 / 60)
    assert points[0]["egress_bps"] == pytest.approx(12_000_000 * 8 / 60)

    params["interface_idx"] = 1
    wan = _data(api_client.get("/api/v1/bandwidth/traffic-series", params=params))["points"]
    assert wan[0]["ingress_bps"] == pytest.approx(9_000_000 * 8 / 60)
    assert wan[0]["egress_bps"] == pytest.approx(3_000_000 * 8 / 60)


def test_interfaces_average_over_the_whole_window(api_client, run_sql):
    _seed(run_sql)
    params = {"exporter_id": str(ROUTER_ID), "window": "1h"}
    rows = {i["interface_idx"]: i for i in _data(api_client.get("/api/v1/bandwidth/interfaces", params=params))["interfaces"]}
    assert set(rows) == {1, 2, 3}

    wan = rows[1]
    assert wan["name"] == "WAN"
    assert wan["speed_mbps"] == 100
    assert wan["in_bps"] == pytest.approx(9_000_000 * 8 / 3600, abs=0.01)
    assert wan["out_bps"] == pytest.approx(3_000_000 * 8 / 3600, abs=0.01)
    assert wan["utilization_percentage"] == pytest.approx(0.02)
    assert wan["is_active"] is True

    # CHARACTERIZATION: a string alias carries no speed, so utilization assumes 1 Gbps.
    lan = rows[2]
    assert lan["name"] == "LAN"
    assert lan["speed_mbps"] == 1000

    dmz = rows[3]
    assert (dmz["name"], dmz["speed_mbps"], dmz["in_bytes"], dmz["is_active"]) == ("DMZ", 10000, 0, False)


def test_top_talkers_and_conversations(api_client, run_sql):
    _seed(run_sql)
    data = _data(api_client.get("/api/v1/bandwidth/top-talkers", params={"window": "1h"}))

    talkers = data["top_endpoints"]
    assert [t["ip_address"] for t in talkers[:3]] == [HOST_A_IP, INTERNET_IP, HOST_B_IP]
    host_a = talkers[0]
    assert host_a["hostname"] == "app-a"
    assert (host_a["ingress_bytes"], host_a["egress_bytes"]) == (12_000_000, 9_000_000)
    assert host_a["flow_count"] == 25
    assert host_a["avg_packet_size"] == 21_000_000 // 17_000
    assert talkers[2]["hostname"] == "db-b"
    assert {t["ip_address"] for t in talkers[3:]} == {TRANSIT_SRC_IP, TRANSIT_DST_IP, SCANNER_IP}

    convs = data["top_conversations"]
    assert [c["total_bytes"] for c in convs] == [12_000_000, 6_000_000, 3_000_000, 1_200_000, 3_000]
    db_conv = convs[2]
    assert (db_conv["src_hostname"], db_conv["dst_hostname"], db_conv["dst_port"]) == ("app-a", "db-b", 5432)
    assert (db_conv["dscp"], db_conv["dscp_label"]) == (46, "EF (VoIP)")
    scan = convs[4]
    assert (scan["protocol"], scan["anomaly_hint"]) == ("TCP", "SYN_ONLY")
    assert convs[0]["anomaly_hint"] is None


def test_applications_qos_and_zones(api_client, run_sql):
    _seed(run_sql)
    data = _data(api_client.get("/api/v1/bandwidth/applications", params={"window": "1h"}))

    apps = {(a["protocol_name"], a["port"]): a for a in data["applications"]}
    assert apps[("TCP", 443)]["total_bytes"] == 18_000_000
    assert apps[("TCP", 443)]["percentage"] == pytest.approx(18_000_000 / 22_203_000 * 100, abs=0.01)
    assert set(apps) == {("TCP", 443), ("TCP", 5432), ("UDP", 53), ("TCP", 22)}

    qos = {q["dscp"]: q for q in data["qos_classes"]}
    assert qos[46]["total_bytes"] == 3_000_000
    assert qos[46]["percentage"] == pytest.approx(3_000_000 / 22_203_000 * 100, abs=0.01)
    assert qos[0]["total_bytes"] == 19_203_000
    assert data["qos_source"] == "flow_minute_rollups"

    zones = {z["zone"]: z["total_bytes"] for z in data["traffic_zones"]}
    assert zones == {
        "East-West (Internal)": 3_000_000,
        "North-South Egress": 6_000_000,
        "North-South Ingress": 12_003_000,
        "External / Transit": 1_200_000,
    }
