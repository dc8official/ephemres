"""Characterization of the flow dashboard endpoints against real TimescaleDB SQL.

The dataset is small enough to compute by hand; the expected values below are those
hand calculations. Assertions marked CHARACTERIZATION pin current behaviour that the v3.4 plan
(docs/architecture/v3.4-dashboard-data-and-design-plan.md) intends to change; assertions marked
CHANGED IN v3.4 PHASE 1 record behaviour that phase deliberately changed.
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
    # CHANGED IN v3.4 PHASE 1: transit (no monitored endpoint on either side) is its own figure.
    assert ov["total_transit_bps"] == pytest.approx(1_200_000 * 8 / 300)
    assert ov["total_transit_pps"] == pytest.approx(1_000 / 300, abs=0.01)
    assert ov["window_seconds"] == 300


def test_overview_scoped_to_one_exporter(api_client, run_sql):
    _seed(run_sql)
    mine = _data(api_client.get("/api/v1/bandwidth/overview", params={"exporter_id": str(ROUTER_ID)}))
    assert mine["exporter_id"] == str(ROUTER_ID)
    assert mine["total_flows_count"] == 95

    other = _data(api_client.get("/api/v1/bandwidth/overview", params={"exporter_id": str(HOST_A_ID)}))
    assert (other["total_flows_count"], other["total_ingress_bps"], other["active_exporters_count"]) == (0, 0, 0)


def test_fleet_series_reports_transit_separately(api_client, run_sql):
    m1, m2 = _seed(run_sql)
    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "1h"}))
    assert (series["bucket_seconds"], series["directional"]) == (60, True)
    points = series["points"]
    by_ts = {datetime.datetime.fromisoformat(p["timestamp"]): p for p in points}
    assert set(by_ts) == {m1, m2}

    p1 = by_ts[m1]
    assert p1["ingress_bps"] == pytest.approx(15_003_000 * 8 / 60)
    assert p1["egress_bps"] == pytest.approx(9_000_000 * 8 / 60)
    assert p1["flows_per_sec"] == pytest.approx(75 / 60, abs=0.01)
    assert p1["transit_bps"] == 0

    # CHANGED IN v3.4 PHASE 1: a bucket holding only transit traffic used to be split 50/50 into
    # ingress and egress; it is now reported as transit.
    p2 = by_ts[m2]
    assert (p2["ingress_bps"], p2["egress_bps"]) == (0, 0)
    assert p2["transit_bps"] == pytest.approx(1_200_000 * 8 / 60)
    assert p2["transit_pps"] == pytest.approx(1_000 / 60, abs=0.01)


def test_hourly_window_includes_fresh_data(api_client, run_sql):
    _seed(run_sql)
    # CHANGED IN v3.4 PHASE 2 (migration 0014): flow_hourly_rollups uses real-time aggregation,
    # so minutes the refresh policy has not materialized yet still reach the 24h window.
    # Before, this list was empty until the next hourly refresh.
    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "24h"}))
    assert (series["bucket_seconds"], series["directional"]) == (3600, True)
    points = series["points"]
    assert points, "fresh minute data should be visible in the hourly tier"

    def total(key):
        return sum(p[key] for p in points) * 3600 / 8

    # bps is rounded to 2 decimals per point, so allow a few bytes of drift.
    assert total("ingress_bps") == pytest.approx(15_003_000, abs=10)
    assert total("egress_bps") == pytest.approx(9_000_000, abs=10)
    assert total("transit_bps") == pytest.approx(1_200_000, abs=10)


def test_hourly_and_daily_aggregates_are_real_time(run_sql):
    rows = run_sql(lambda conn: conn.fetch(
        "SELECT view_name, materialized_only FROM timescaledb_information.continuous_aggregates "
        "WHERE view_name IN ('flow_hourly_rollups', 'flow_daily_rollups') ORDER BY view_name"
    ))
    assert [(r["view_name"], r["materialized_only"]) for r in rows] == [
        ("flow_daily_rollups", False),
        ("flow_hourly_rollups", False),
    ]


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


def _interfaces(api_client, window):
    params = {"exporter_id": str(ROUTER_ID), "window": window}
    data = _data(api_client.get("/api/v1/bandwidth/interfaces", params=params))
    return data, {i["interface_idx"]: i for i in data["interfaces"]}


def test_interfaces_rates_cover_the_time_with_data(api_client, run_sql):
    from app.services.flow_metrics import percentile_with_idle

    _seed(run_sql)
    data, rows = _interfaces(api_client, "1h")
    assert set(rows) == {1, 2, 3}
    assert (data["source"], data["p95_resolution_seconds"]) == ("flow_interface_minute_rollups", 60)

    # CHANGED IN v3.4 PHASE 2: rates average over the covered time (from the exporter's first
    # data in the window, ~2-3 minutes here) instead of the full window length.
    covered = data["covered_seconds"]
    assert 60 <= covered <= 4 * 60
    wan = rows[1]
    assert wan["name"] == "WAN"
    assert wan["speed_mbps"] == 100
    assert wan["in_bps"] == pytest.approx(9_000_000 * 8 / covered, rel=0.02)
    assert wan["out_bps"] == pytest.approx(3_000_000 * 8 / covered, rel=0.02)
    # Busiest minute and 95th percentile minute (idle minutes count as 0).
    assert wan["in_peak_bps"] == pytest.approx(9_000_000 * 8 / 60)
    slots = -(-covered // 60)
    assert wan["in_p95_bps"] == pytest.approx(percentile_with_idle([9_000_000], slots) * 8 / 60, rel=0.02)
    assert wan["peak_utilization_percentage"] == pytest.approx(1.2)
    assert wan["speed_configured"] is True
    assert wan["is_active"] is True

    # A string alias carries no speed: speed_mbps reports the 1 Gbps default, but no utilization.
    lan = rows[2]
    assert lan["name"] == "LAN"
    assert (lan["speed_mbps"], lan["speed_configured"], lan["utilization_percentage"]) == (1000, False, None)
    assert lan["p95_utilization_percentage"] is None

    dmz = rows[3]
    assert (dmz["name"], dmz["speed_mbps"], dmz["in_bytes"], dmz["is_active"]) == ("DMZ", 10000, 0, False)


def test_interfaces_30d_window_reads_hourly_rollup(api_client, run_sql):
    _seed(run_sql)
    data, rows = _interfaces(api_client, "30d")
    # CHANGED IN v3.4 PHASE 2: 30 days used to read the 7-day minute table and divide a week of
    # bytes by thirty days. The hourly rollup (real-time) covers it and keeps minute peaks.
    assert (data["source"], data["p95_resolution_seconds"]) == ("flow_interface_hourly_rollups", 3600)
    wan = rows[1]
    assert wan["in_bytes"] == 9_000_000
    assert wan["in_peak_bps"] == pytest.approx(9_000_000 * 8 / 60)
    assert wan["in_bps"] == pytest.approx(9_000_000 * 8 / data["covered_seconds"], rel=0.02)


def test_exporter_series_long_window_uses_hourly_rollup(api_client, run_sql):
    _seed(run_sql)
    params = {"window": "7d", "exporter_id": str(ROUTER_ID)}
    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params=params))
    assert series["bucket_seconds"] == 3600
    total_in = sum(p["ingress_bps"] for p in series["points"]) * 3600 / 8
    assert total_in == pytest.approx(12_000_000, abs=10)


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


def test_endpoint_detail_returns_flow_exporter_fields(api_client, run_sql):
    # CHANGED IN v3.4 PHASE 1: GET /endpoints/{id} used to drop these, so the detail page never
    # showed the passive-exporter banner or the configured exporter IPs.
    _seed(run_sql)
    ep = _data(api_client.get(f"/api/v1/endpoints/{ROUTER_ID}"))
    assert ep["device_role"] == "FLOW_EXPORTER"
    assert ep["monitoring_enabled"] is False
    assert ep["flow_exporter_ips"] == ["10.0.0.1"]
    assert ep["flow_interface_aliases"]["1"] == {"name": "WAN", "speed_mbps": 100}

    host = _data(api_client.get(f"/api/v1/endpoints/{HOST_A_ID}"))
    assert (host["device_role"], host["flow_exporter_ips"]) == ("ACTIVE_HOST", [])


def test_exporter_health_from_minute_rollups(api_client, run_sql):
    m1, _ = _seed(run_sql)
    data = _data(api_client.get("/api/v1/bandwidth/exporters"))
    router = next(e for e in data["exporters"] if e["id"] == str(ROUTER_ID))
    # CHANGED IN v3.4 PHASE 2: last_flow_time was always None.
    assert datetime.datetime.fromisoformat(router["last_flow_time"]) == m1
    assert router["flows_per_sec"] == pytest.approx(95 / 300, abs=0.01)
