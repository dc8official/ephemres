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
    # (total_bytes, total_flows, active_exporters)
    mock_res.one_or_none.return_value = (50000000, 12000, 3)
    mock_session.execute.return_value = mock_res

    resp = test_client.get("/api/v1/bandwidth/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["data"]["active_exporters_count"] == 3
    assert data["data"]["total_flows_count"] == 12000
    assert data["data"]["total_ingress_bps"] > 0
    assert data["data"]["total_egress_bps"] > 0


def test_bandwidth_traffic_series(client):
    test_client, mock_session = client

    now_dt = datetime.now(timezone.utc)
    mock_row1 = (now_dt, 10000)
    mock_result = MagicMock()
    mock_result.all.return_value = [mock_row1]

    mock_session.execute.return_value = mock_result

    resp = test_client.get("/api/v1/bandwidth/traffic-series?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["points"]) == 1
    assert data["data"]["points"][0]["ingress_bps"] > 0
    assert data["data"]["points"][0]["egress_bps"] > 0


def test_bandwidth_top_talkers(client):
    test_client, mock_session = client

    ep_id = uuid4()
    mock_src_row = ("192.168.1.50", ep_id, 1000000, 800)
    mock_conv_row = ("192.168.1.50", "192.168.1.100", 6, 5432, 2000000, 1200)

    m1 = MagicMock()
    m1.all.return_value = [mock_src_row]
    # Hostname resolution query for ep_id
    m_host = MagicMock()
    m_host.all.return_value = [(ep_id, "app-server-1")]
    # Conversations query
    m2 = MagicMock()
    m2.all.return_value = [mock_conv_row]

    mock_session.execute.side_effect = [m1, m_host, m2]

    resp = test_client.get("/api/v1/bandwidth/top-talkers?window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["top_endpoints"]) == 1
    assert data["data"]["top_endpoints"][0]["ip_address"] == "192.168.1.50"
    assert data["data"]["top_endpoints"][0]["hostname"] == "app-server-1"
    assert len(data["data"]["top_conversations"]) == 1
    assert data["data"]["top_conversations"][0]["dst_port"] == 5432


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

