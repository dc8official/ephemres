"""WAN-role interfaces and edge totals against real TimescaleDB SQL (v3.4 Phase 4).

Two routers each see the same traffic once on their WAN link. The per-exporter totals add
both; the WAN totals must count each WAN interface once and ignore LAN interfaces.
"""
import datetime
import json
import uuid

import pytest

EDGE_ID = uuid.UUID("22222222-0000-0000-0000-0000000000b1")
BRANCH_ID = uuid.UUID("22222222-0000-0000-0000-0000000000b2")
GONE_ID = uuid.UUID("22222222-0000-0000-0000-0000000000b3")

LEGACY_ALIASES = {"1": {"name": "ISP-A", "speed_mbps": 100}, "2": "LAN"}


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    m1, m2 = now - datetime.timedelta(minutes=2), now - datetime.timedelta(minutes=4)
    stale = now - datetime.timedelta(minutes=9)  # outside the 5-minute overview window

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips,
                                   flow_interface_aliases, deleted_at)
            VALUES ($1, '10.0.0.1', 'edge-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.0.0.1}', $2::jsonb, NULL),
                   ($3, '10.9.0.1', 'branch-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.9.0.1}', $4::jsonb, NULL),
                   ($5, '10.8.0.1', 'retired-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.8.0.1}', $6::jsonb, now())
            """,
            EDGE_ID, json.dumps(LEGACY_ALIASES),
            BRANCH_ID, json.dumps({"5": {"name": "ISP-B", "speed_mbps": 50, "role": "lan"},
                                   "6": {"name": "LAN", "speed_mbps": 1000, "role": "lan"}}),
            GONE_ID, json.dumps({"1": {"name": "OLD-WAN", "speed_mbps": 10, "role": "wan"}}),
        )
        rows = [
            # bucket, exporter, ifindex, in_bytes, out_bytes, in_packets, out_packets, flows
            (m1, EDGE_ID, 1, 6_000_000, 1_500_000, 5_000, 1_000, 10),
            (m2, EDGE_ID, 1, 3_000_000, 1_500_000, 2_500, 1_000, 10),
            (stale, EDGE_ID, 1, 99_000_000, 99_000_000, 1, 1, 1),
            (m1, EDGE_ID, 2, 3_000_000, 9_000_000, 1_000, 6_000, 20),
            (m1, BRANCH_ID, 5, 1_500_000, 3_000_000, 1_000, 2_000, 5),
            (m1, BRANCH_ID, 6, 3_000_000, 1_500_000, 2_000, 1_000, 5),
            (m1, GONE_ID, 1, 7_000_000, 7_000_000, 7, 7, 7),
        ]
        await conn.executemany(
            """
            INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx,
                in_bytes, out_bytes, in_packets, out_packets, flow_count)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            rows,
        )

    run_sql(seed)
    return m1, m2


def _data(resp):
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    return body["data"]


def _mark_wan(api_client):
    """Set roles through the admin API: edge #1 and branch #5 are WAN, the rest internal."""
    resp = api_client.put(
        f"/api/v1/bandwidth/endpoints/{EDGE_ID}/interfaces",
        json={
            "1": {"name": "ISP-A", "speed_mbps": 100, "role": "wan"},
            "2": {"name": "LAN", "speed_mbps": 1000},
        },
    )
    _data(resp)
    resp = api_client.put(
        f"/api/v1/bandwidth/endpoints/{BRANCH_ID}/interfaces",
        json={
            "5": {"name": "ISP-B", "speed_mbps": 50, "role": "wan"},
            "6": {"name": "LAN", "speed_mbps": 1000, "role": "lan"},
        },
    )
    _data(resp)


def test_no_wan_interfaces_means_no_edge_totals(api_client, run_sql):
    _seed(run_sql)
    ov = _data(api_client.get("/api/v1/bandwidth/overview"))
    assert ov["wan_interfaces_count"] == 0
    assert ov["wan_links"] == []
    assert (ov["wan_in_bps"], ov["wan_out_bps"]) == (0, 0)

    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "1h", "wan_only": "true"}))
    assert series["wan_only"] is False  # falls back to the normal fleet series


def test_overview_counts_only_wan_interfaces(api_client, run_sql):
    _seed(run_sql)
    _mark_wan(api_client)
    ov = _data(api_client.get("/api/v1/bandwidth/overview"))

    assert ov["wan_interfaces_count"] == 2  # the deleted router's WAN link is ignored
    # In: edge #1 (6 MB + 3 MB) + branch #5 (1.5 MB); the stale bucket and LAN ports are excluded.
    assert ov["wan_in_bps"] == pytest.approx(10_500_000 * 8 / 300)
    assert ov["wan_out_bps"] == pytest.approx(6_000_000 * 8 / 300)
    assert ov["wan_in_pps"] == pytest.approx(8_500 / 300, abs=0.01)
    assert ov["wan_out_pps"] == pytest.approx(4_000 / 300, abs=0.01)

    links = ov["wan_links"]
    assert [(link["exporter_hostname"], link["interface_idx"]) for link in links] == [
        ("edge-router", 1),
        ("branch-router", 5),
    ]
    edge, branch = links
    assert edge["name"] == "ISP-A"
    assert edge["share_percentage"] == pytest.approx(12_000_000 * 100 / 16_500_000, abs=0.01)
    assert branch["share_percentage"] == pytest.approx(4_500_000 * 100 / 16_500_000, abs=0.01)
    # 9 MB in over 300 s = 240 kbps on a 100 Mbps link.
    assert edge["utilization_percentage"] == pytest.approx(0.24, abs=0.01)
    assert edge["speed_mbps"] == 100


def test_wan_links_without_speed_have_no_utilization(api_client, run_sql):
    _seed(run_sql)
    run_sql(lambda conn: conn.execute(
        "UPDATE endpoints SET flow_interface_aliases = $1::jsonb WHERE id = $2",
        json.dumps({"1": {"name": "ISP-A", "role": "wan"}}), EDGE_ID,
    ))
    ov = _data(api_client.get("/api/v1/bandwidth/overview"))
    (link,) = ov["wan_links"]
    assert link["speed_mbps"] is None
    assert link["utilization_percentage"] is None


def test_overview_wan_scoped_to_one_exporter(api_client, run_sql):
    _seed(run_sql)
    _mark_wan(api_client)
    ov = _data(api_client.get("/api/v1/bandwidth/overview", params={"exporter_id": str(BRANCH_ID)}))
    assert ov["wan_interfaces_count"] == 1
    assert ov["wan_in_bps"] == pytest.approx(1_500_000 * 8 / 300)
    assert ov["wan_links"][0]["share_percentage"] == 100.0


def test_wan_series_sums_wan_interfaces_per_minute(api_client, run_sql):
    m1, m2 = _seed(run_sql)
    _mark_wan(api_client)
    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "1h", "wan_only": "true"}))
    assert series["wan_only"] is True
    assert series["bucket_seconds"] == 60
    by_time = {p["timestamp"]: p for p in series["points"]}
    p1 = by_time[m1.isoformat()]
    assert p1["ingress_bps"] == pytest.approx(7_500_000 * 8 / 60, abs=0.01)
    assert p1["egress_bps"] == pytest.approx(4_500_000 * 8 / 60, abs=0.01)
    assert by_time[m2.isoformat()]["ingress_bps"] == pytest.approx(3_000_000 * 8 / 60, abs=0.01)

    # A single interface ignores wan_only.
    one = _data(api_client.get(
        "/api/v1/bandwidth/traffic-series",
        params={"window": "1h", "wan_only": "true", "exporter_id": str(EDGE_ID), "interface_idx": 2},
    ))
    assert one["wan_only"] is False
    assert one["points"][0]["ingress_bps"] == pytest.approx(3_000_000 * 8 / 60, abs=0.01)


def test_role_round_trips_through_the_apis(api_client, run_sql):
    _seed(run_sql)
    _mark_wan(api_client)
    interfaces = _data(api_client.get(
        "/api/v1/bandwidth/interfaces", params={"exporter_id": str(EDGE_ID), "window": "1h"}
    ))["interfaces"]
    assert {i["interface_idx"]: i["role"] for i in interfaces} == {1: "wan", 2: "lan"}

    detail = _data(api_client.get(f"/api/v1/endpoints/{EDGE_ID}"))
    assert detail["flow_interface_aliases"]["1"]["role"] == "wan"
    assert detail["flow_interface_aliases"]["2"]["role"] == "lan"


def test_unknown_role_is_rejected(api_client, run_sql):
    _seed(run_sql)
    resp = api_client.put(
        f"/api/v1/bandwidth/endpoints/{EDGE_ID}/interfaces",
        json={"1": {"name": "ISP-A", "speed_mbps": 100, "role": "core"}},
    )
    assert resp.status_code in (400, 422)


def test_unset_speed_round_trips_and_keeps_the_exporter_list_working(api_client, run_sql):
    _seed(run_sql)
    # The editors send no speed for "Not set"; it must stay unset instead of becoming 1 Gbps.
    _data(api_client.put(
        f"/api/v1/bandwidth/endpoints/{EDGE_ID}/interfaces",
        json={"1": {"name": "ISP-A", "role": "wan"}, "2": {"name": "LAN", "speed_mbps": 1000}},
    ))
    exporters = _data(api_client.get("/api/v1/bandwidth/exporters"))["exporters"]
    edge = next(e for e in exporters if e["id"] == str(EDGE_ID))
    assert edge["interface_aliases"]["1"] == {"name": "ISP-A", "speed_mbps": None, "role": "wan"}
    assert edge["interface_aliases"]["2"] == {"name": "LAN", "speed_mbps": 1000, "role": "lan"}

    detail = _data(api_client.get(f"/api/v1/endpoints/{EDGE_ID}"))
    assert detail["flow_interface_aliases"]["1"]["speed_mbps"] is None

    interfaces = _data(api_client.get(
        "/api/v1/bandwidth/interfaces", params={"exporter_id": str(EDGE_ID), "window": "1h"}
    ))["interfaces"]
    by_idx = {i["interface_idx"]: i for i in interfaces}
    assert by_idx[1]["speed_configured"] is False
    assert by_idx[1]["utilization_percentage"] is None
