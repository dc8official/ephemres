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


def test_bandwidth_top_talkers(client):
    from tests.backend.flow_mocks import conversation_row, talker_row, top_talkers_results

    test_client, mock_session = client

    ep_id = uuid4()
    mock_session.execute.side_effect = top_talkers_results(
        talkers=[talker_row("192.168.1.50", ep_id, ingress=0, egress=1000000, flows=800)],
        hostnames=[(ep_id, "app-server-1")],
        conversations=[conversation_row("192.168.1.50", "192.168.1.100", 6, 5432, 2000000, 1200)],
        conversation_hostnames=[("192.168.1.50", "app-server-1")],
    )

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


def test_flow_preflight_success(client):
    """The preflight checks what flowd needs: FastStore opens and the ports saved in Settings are
    free (not config.toml's). There is no Redis version or stream-write check any more."""
    from types import SimpleNamespace

    test_client, _ = client
    store = AsyncMock()
    store.ping = AsyncMock(return_value=True)
    runtime = SimpleNamespace(flow_netflow_port=9995, flow_ipfix_port=9996)
    with patch("app.core.faststore.FastStoreClient", return_value=store), \
         patch("app.routers.settings.get_runtime_settings", return_value=runtime), \
         patch("app.routers.settings._udp_port_conflicts", AsyncMock(return_value=[])) as ports:
        resp = test_client.post("/api/v1/settings/flow/preflight")
    assert resp.status_code == 200
    res = resp.json()["data"]
    assert res["faststore_connected"] is True and res["udp_ports_available"] is True
    assert res["ready"] is True
    assert "stream_write_success" not in res and "redis_version" not in res
    assert ports.await_args.args[1] == (9995, 9996)


def test_flow_preflight_reports_a_port_conflict(client):
    from types import SimpleNamespace

    test_client, _ = client
    store = AsyncMock()
    store.ping = AsyncMock(return_value=True)
    runtime = SimpleNamespace(flow_netflow_port=2055, flow_ipfix_port=4739)
    with patch("app.core.faststore.FastStoreClient", return_value=store), \
         patch("app.routers.settings.get_runtime_settings", return_value=runtime), \
         patch("app.routers.settings._udp_port_conflicts", AsyncMock(return_value=[2055])):
        res = test_client.post("/api/v1/settings/flow/preflight").json()["data"]
    assert res["ready"] is False and res["port_conflicts"] == [2055]
    assert "2055" in res["message"]


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

    # Verify that the endpoint statement (after the SNMP device lookup) compiled cleanly with
    # the recent_exporters subqueries; the exporter health query runs after it.
    call_stmt = mock_session.execute.call_args_list[1][0][0]
    compiled_sql = call_stmt.compile()
    assert "IN (SELECT DISTINCT" in compiled_sql


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


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/bandwidth/traffic-series",
        "/api/v1/bandwidth/top-talkers",
        "/api/v1/bandwidth/conversations",
        "/api/v1/bandwidth/applications",
        f"/api/v1/bandwidth/interfaces?exporter_id={uuid4()}",
    ],
)
def test_unknown_window_is_rejected(client, path):
    # It used to fall back to 1h while the response echoed the unknown window.
    test_client, _ = client
    sep = "&" if "?" in path else "?"
    resp = test_client.get(f"{path}{sep}window=2h")
    assert resp.status_code == 400
    assert "1h, 6h, 24h, 7d, 30d, 1y" in resp.text


@pytest.mark.parametrize(
    "path, longest",
    [
        ("/api/v1/bandwidth/top-talkers?window=1y", "30d"),
        ("/api/v1/bandwidth/conversations?window=1y", "30d"),
        (f"/api/v1/bandwidth/traffic-series?window=1y&endpoint_id={uuid4()}", "30d"),
        (f"/api/v1/bandwidth/applications?window=1y&endpoint_id={uuid4()}", "30d"),
    ],
)
def test_window_longer_than_the_data_is_refused(client, path, longest):
    """A 1y answer holding 30 or 90 days looked like a year in which traffic had dropped."""
    test_client, _ = client
    resp = test_client.get(path)
    assert resp.status_code == 400
    assert f"longest window here is {longest}" in resp.json()["detail"]


def test_report_says_what_a_long_range_leaves_out():
    import datetime

    from app.services.report_compiler import flow_coverage_notes

    now = datetime.datetime(2026, 11, 7, tzinfo=datetime.timezone.utc)
    assert flow_coverage_notes(now - datetime.timedelta(days=30), now) == []
    notes = flow_coverage_notes(now - datetime.timedelta(days=200), now)
    assert len(notes) == 1 and "2026-08-09 onward" in notes[0] and "kept 90 days" in notes[0]
    assert len(flow_coverage_notes(now - datetime.timedelta(days=500), now)) == 2


def test_restore_waits_for_a_running_refresh_job(monkeypatch):
    """TimescaleDB refuses a refresh while the aggregate's policy job runs; restore must not skip it."""
    import asyncio

    from app.core import restore

    calls = []

    async def fake_execute(conn, sql):
        calls.append(sql)
        if len(calls) < 3:
            raise RuntimeError('could not refresh continuous aggregate "x" due to a concurrent refresh')

    monkeypatch.setattr(restore, "_execute_sql", fake_execute)
    monkeypatch.setattr(restore, "AGGREGATE_REFRESH_RETRY_SECONDS", 0)
    asyncio.run(restore._refresh_aggregate_waiting_for_job(None, "x"))
    assert len(calls) == 3
