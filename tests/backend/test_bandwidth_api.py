import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.core.asgi import FastAPI
from app.core.asgi import TestClient

from app.routers.bandwidth import router as bandwidth_router
from app.routers.settings import router as settings_router
from app.database import get_db
from app.routers.auth import get_current_user, require_admin

test_app = FastAPI()
test_app.include_router(bandwidth_router, prefix="/api/v1")
test_app.include_router(settings_router, prefix="/api/v1")


@pytest.fixture
def mock_admin():
    admin = MagicMock()
    admin.id = uuid4()
    admin.username = "admin"
    admin.role = "admin"
    admin.is_active = True
    return admin


@pytest.fixture
def client(mock_admin):
    mock_session = AsyncMock()

    async def override_get_db():
        yield mock_session

    async def override_get_current_user():
        return mock_admin

    async def override_require_admin():
        return mock_admin

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_get_current_user
    test_app.dependency_overrides[require_admin] = override_require_admin

    with TestClient(test_app) as test_client:
        yield test_client, mock_session

    test_app.dependency_overrides.clear()


def test_bandwidth_overview(client):
    test_client, mock_session = client

    mock_res = MagicMock()
    # (total, ingress, egress, transit bytes; total, ingress, egress, transit packets; flows; exporters)
    mock_res.one_or_none.return_value = (50_000_000, 30_000_000, 15_000_000, 5_000_000, 40_000, 24_000, 12_000, 4_000, 12000, 3)
    mock_session.execute.return_value = mock_res

    resp = test_client.get("/api/v1/bandwidth/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    ov = data["data"]
    assert ov["active_exporters_count"] == 3
    assert ov["total_flows_count"] == 12000
    assert ov["total_ingress_bps"] == 30_000_000 * 8 / 300
    assert ov["total_egress_bps"] == 15_000_000 * 8 / 300
    assert ov["total_transit_bps"] == round(5_000_000 * 8 / 300, 2)
    assert ov["total_transit_pps"] == round(4_000 / 300, 2)
    assert ov["avg_packet_size"] == 50_000_000 // 40_000
    assert ov["window_seconds"] == 300


def test_bandwidth_traffic_series(client):
    test_client, mock_session = client

    now_dt = datetime.now(timezone.utc)
    # (bucket, flow_count, ingress bytes/packets, egress bytes/packets, transit bytes/packets)
    mock_row1 = (now_dt, 4, 10000, 10, 5000, 5, 0, 0)
    mock_result = MagicMock()
    mock_result.all.return_value = [mock_row1]

    mock_session.execute.return_value = mock_result

    resp = test_client.get("/api/v1/bandwidth/traffic-series?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["points"]) == 1
    point = data["data"]["points"][0]
    assert point["ingress_bps"] == round(10000 * 8 / 60, 2)
    assert point["egress_bps"] == round(5000 * 8 / 60, 2)
    assert point["transit_bps"] == 0
    assert data["data"]["bucket_seconds"] == 60
    assert data["data"]["directional"] is True


def test_bandwidth_top_talkers(client):
    test_client, mock_session = client

    ep_id = uuid4()
    mock_src_row = ("192.168.1.50", ep_id, 1000000, 800)
    mock_conv_row = ("192.168.1.50", "192.168.1.100", 6, 5432, 2000000, 1200)

    m1 = MagicMock()
    m1.all.return_value = [mock_src_row]
    m_in = MagicMock()
    m_in.all.return_value = []
    # Hostname resolution query for ep_id
    m_host = MagicMock()
    m_host.all.return_value = [(ep_id, "app-server-1")]
    # Conversations query
    m2 = MagicMock()
    m2.all.return_value = [mock_conv_row]

    mock_session.execute.side_effect = [m1, m_in, m_host, m2]

    resp = test_client.get("/api/v1/bandwidth/top-talkers?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["top_endpoints"]) == 1
    assert data["data"]["top_endpoints"][0]["ip_address"] == "192.168.1.50"
    assert data["data"]["top_endpoints"][0]["hostname"] == "app-server-1"
    assert len(data["data"]["top_conversations"]) == 1
    assert data["data"]["top_conversations"][0]["dst_port"] == 5432
    assert data["data"]["top_conversations"][0]["src_hostname"] == "app-server-1"
    assert data["data"]["top_conversations"][0]["dst_hostname"] is None


def test_bandwidth_applications(client):
    test_client, mock_session = client

    mock_app_row = (6, 443, 85000000)
    mock_res = MagicMock()
    mock_res.all.return_value = [mock_app_row]
    mock_session.execute.return_value = mock_res

    resp = test_client.get("/api/v1/bandwidth/applications?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["applications"]) == 1
    app_item = data["data"]["applications"][0]
    assert app_item["port"] == 443
    assert app_item["protocol_name"] == "TCP"
    assert app_item["service_label"] == "HTTPS"


@pytest.mark.parametrize("daily_has_tos", [True, False])
def test_bandwidth_applications_30d_window_daily_rollup(client, daily_has_tos):
    """
    Verifies that requesting the 30-day window routes cleanly to FlowDailyRollup
    without raising AttributeError on missing src_endpoint_id / src_ip attributes,
    and that QoS falls back to flow_minute_rollups when the daily continuous
    aggregate has no `tos` column (TimescaleDB installs).
    """
    from app.routers import bandwidth

    test_client, mock_session = client
    bandwidth._TABLE_COLUMN_CACHE.clear()

    mock_app_row = (6, 443, 85000000)
    mock_qos_row = (184, 85000000, 75000, 120)  # EF (VoIP)
    mock_res_apps = MagicMock()
    mock_res_apps.all.return_value = [mock_app_row]
    mock_res_qos = MagicMock()
    mock_res_qos.all.return_value = [mock_qos_row]

    mock_res_column = MagicMock()
    mock_res_column.scalar.return_value = 1 if daily_has_tos else None

    # FlowDailyRollup skips the zone query; executes app query, `tos` column check, then qos query
    mock_session.execute.side_effect = [mock_res_apps, mock_res_column, mock_res_qos]

    resp = test_client.get("/api/v1/bandwidth/applications?window=30d")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["applications"]) == 1
    assert len(data["data"]["qos_classes"]) == 1
    assert data["data"]["qos_classes"][0]["dscp_label"] == "EF (VoIP)"
    # Traffic zones are safely zeroed out without runtime attribute errors
    assert len(data["data"]["traffic_zones"]) == 4
    for z in data["data"]["traffic_zones"]:
        assert z["total_bytes"] == 0
        assert z["percentage"] == 0.0

    qos_sql = str(mock_session.execute.call_args_list[2].args[0])
    expected_table = "flow_daily_rollups" if daily_has_tos else "flow_minute_rollups"
    assert expected_table in qos_sql
    assert data["data"]["qos_source"] == expected_table
    bandwidth._TABLE_COLUMN_CACHE.clear()


def test_bandwidth_applications_1y_qos_fallback_reports_clamped_window(client):
    """A 1y window falling back to flow_minute_rollups only has 90 days of ToS data; say so."""
    import datetime

    from app.routers import bandwidth

    test_client, mock_session = client
    bandwidth._TABLE_COLUMN_CACHE.clear()

    mock_res_apps = MagicMock()
    mock_res_apps.all.return_value = [(6, 443, 1000)]
    mock_res_column = MagicMock()
    mock_res_column.scalar.return_value = None
    mock_res_qos = MagicMock()
    mock_res_qos.all.return_value = [(0, 1000, 10, 1)]
    mock_session.execute.side_effect = [mock_res_apps, mock_res_column, mock_res_qos]

    resp = test_client.get("/api/v1/bandwidth/applications?window=1y")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["qos_source"] == "flow_minute_rollups"
    qos_start = datetime.datetime.fromisoformat(data["qos_window_start"].replace("Z", "+00:00"))
    age = datetime.datetime.now(datetime.timezone.utc) - qos_start
    assert datetime.timedelta(days=89) < age <= datetime.timedelta(days=90, minutes=1)
    column_check_sql = str(mock_session.execute.call_args_list[1].args[0])
    assert "current_schema()" in column_check_sql
    bandwidth._TABLE_COLUMN_CACHE.clear()



def test_bandwidth_map_exporter(client):
    test_client, mock_session = client

    target_id = uuid4()
    mock_endpoint = MagicMock()
    mock_endpoint.id = target_id
    mock_endpoint.hostname = "edge-core-01"
    mock_endpoint.ip_address = "192.168.1.1"
    mock_endpoint.flow_exporter_ips = ["10.10.10.1"]

    mock_scalar = MagicMock()
    mock_scalar.scalar_one_or_none.return_value = mock_endpoint
    mock_session.execute.return_value = mock_scalar

    payload = {"endpoint_id": str(target_id)}
    resp = test_client.post("/api/v1/bandwidth/exporters/172.16.200.1/map", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "Successfully mapped exporter IP" in data["data"]["message"]
    assert "172.16.200.1" in mock_endpoint.flow_exporter_ips
    assert mock_session.commit.awaited


@patch("app.core.faststore.Redis")
def test_flow_preflight_success(mock_redis_cls, client):
    test_client, _ = client

    mock_redis_instance = AsyncMock()
    mock_redis_instance.ping = AsyncMock(return_value=True)
    mock_redis_instance.info = AsyncMock(return_value={"redis_version": "7.0.12"})
    mock_redis_instance.xadd = AsyncMock(return_value="1700000000000-0")
    mock_redis_instance.xdel = AsyncMock(return_value=1)
    mock_redis_instance.aclose = AsyncMock()
    mock_redis_cls.return_value = mock_redis_instance

    resp = test_client.post("/api/v1/settings/flow/preflight")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    res = data["data"]
    assert res["redis_connected"] is True
    assert res["redis_version"] == "7.0.12"
    assert res["redis_version_supported"] is True
    assert res["stream_write_success"] is True
    assert res["ready"] is True


@patch("app.routers.bandwidth.get_flow_redis")
def test_list_flow_exporters_and_unmatched(mock_get_flow_redis, client):
    test_client, mock_session = client

    mock_redis = AsyncMock()
    mock_redis.zremrangebyscore = AsyncMock()
    mock_redis.zrevrangebyscore = AsyncMock(return_value=[("10.10.10.0", 1700000000.0)])
    mock_redis.smembers = AsyncMock(return_value=[])
    mock_redis.close = AsyncMock()
    mock_get_flow_redis.return_value = mock_redis

    ep_id = uuid4()
    mock_endpoint = MagicMock()
    mock_endpoint.id = ep_id
    mock_endpoint.hostname = "core-router-01"
    mock_endpoint.ip_address = "172.16.1.1"
    mock_endpoint.flow_exporter_ips = ["172.16.1.254"]
    mock_endpoint.flow_interface_aliases = {1: "GigabitEthernet0/0/0"}
    mock_endpoint.device_role = "FLOW_EXPORTER"

    mock_scalar = MagicMock()
    mock_scalar.scalars.return_value.all.return_value = [mock_endpoint]
    mock_session.execute.return_value = mock_scalar

    resp = test_client.get("/api/v1/bandwidth/exporters")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    exporters = data["data"]["exporters"]
    unmatched = data["data"]["unmatched"]

    assert len(exporters) == 1
    assert exporters[0]["id"] == str(ep_id)
    assert exporters[0]["hostname"] == "core-router-01"
    assert exporters[0]["primary_ip"] == "172.16.1.1"
    assert exporters[0]["device_role"] == "FLOW_EXPORTER"

    assert len(unmatched) == 1
    assert unmatched[0]["ip_address"] == "10.10.10.0"

    # Verify that the endpoint statement (the first query) compiled cleanly with the
    # recent_exporters subqueries; the exporter health query runs after it.
    call_stmt = mock_session.execute.call_args_list[0][0][0]
    compiled_sql = call_stmt.compile()
    assert "IN (SELECT DISTINCT" in compiled_sql


def test_bandwidth_traffic_zones_with_cidr_masks(client):
    """Verify _is_internal correctly strips CIDR subnet masks (/32, /24, /128) when evaluating zone mesh."""
    test_client, mock_session = client

    m_apps = MagicMock()
    m_apps.all.return_value = []

    m_qos = MagicMock()
    m_qos.all.return_value = []

    # Row: (src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, total_bytes, flow_count)
    mock_rows = [
        ("192.168.1.10/32", "10.0.0.5/24", None, None, 100000, 10),      # East-West (both private with CIDR)
        ("192.168.1.10/32", "8.8.8.8/32", None, None, 500000, 50),       # North-South Egress (src private, dst public)
        ("8.8.8.8/32", "192.168.1.10/32", None, None, 800000, 80),       # North-South Ingress (src public, dst private)
        ("1.1.1.1/32", "8.8.8.8/32", None, None, 20000, 2),              # External / Transit (both public)
    ]
    m_zone = MagicMock()
    m_zone.all.return_value = mock_rows

    mock_session.execute.side_effect = [m_apps, m_qos, m_zone]

    resp = test_client.get("/api/v1/bandwidth/applications?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    zones = {z["zone"]: z for z in data["data"]["traffic_zones"]}
    assert zones["East-West (Internal)"]["total_bytes"] == 100000
    assert zones["North-South Egress"]["total_bytes"] == 500000
    assert zones["North-South Ingress"]["total_bytes"] == 800000
    assert zones["External / Transit"]["total_bytes"] == 20000


@patch("app.routers.bandwidth.get_flow_store")
def test_bandwidth_overview_unmatched_zset_and_fallback(mock_get_store, client):
    """Verify overview queries flow:unmatched:zset first, falling back to flow:unmatched:set."""
    test_client, mock_session = client

    mock_res = MagicMock()
    mock_res.one_or_none.return_value = (50000000, 0, 0, 50000000, 40000, 0, 0, 40000, 12000, 3)
    mock_session.execute.return_value = mock_res

    # 1. Primary: zcard succeeds with non-zero
    mock_store_zset = AsyncMock()
    mock_store_zset.zcard = AsyncMock(return_value=7)
    mock_get_store.return_value = mock_store_zset

    resp = test_client.get("/api/v1/bandwidth/overview")
    assert resp.status_code == 200
    assert resp.json()["data"]["unmatched_exporters_count"] == 7
    mock_store_zset.zcard.assert_awaited_with("flow:unmatched:zset")

    # 2. Fallback: zcard returns 0, fallback to scard
    mock_store_fallback = AsyncMock()
    mock_store_fallback.zcard = AsyncMock(return_value=0)
    mock_store_fallback.scard = AsyncMock(return_value=4)
    mock_get_store.return_value = mock_store_fallback

    resp2 = test_client.get("/api/v1/bandwidth/overview")
    assert resp2.status_code == 200
    assert resp2.json()["data"]["unmatched_exporters_count"] == 4
    mock_store_fallback.scard.assert_awaited_with("flow:unmatched:set")


def test_bandwidth_map_exporter_with_cidr_suffix(client):
    """Verify map_flow_exporter successfully accepts an exporter IP containing a CIDR netmask (/32)."""
    test_client, mock_session = client

    target_id = uuid4()
    mock_endpoint = MagicMock()
    mock_endpoint.id = target_id
    mock_endpoint.hostname = "edge-core-01"
    mock_endpoint.ip_address = "192.168.1.1"
    mock_endpoint.flow_exporter_ips = ["10.10.10.1"]

    mock_scalar = MagicMock()
    mock_scalar.scalar_one_or_none.return_value = mock_endpoint
    mock_session.execute.return_value = mock_scalar

    payload = {"endpoint_id": str(target_id)}
    # Pass 172.16.200.1/32
    resp = test_client.post("/api/v1/bandwidth/exporters/172.16.200.1%2F32/map", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert "Successfully mapped exporter IP" in data["data"]["message"]
    assert "172.16.200.1" in mock_endpoint.flow_exporter_ips
    assert mock_session.commit.awaited



