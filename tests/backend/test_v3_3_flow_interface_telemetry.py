import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4, UUID

import pytest
from app.core.asgi import FastAPI
from app.core.asgi import TestClient

from app.database import get_db
from app.routers.auth import get_current_user, require_admin
from app.routers.bandwidth import router as bandwidth_router
from app.routers.reports import router as reports_router
from app.schemas.endpoints import (
    EndpointDetail,
    EndpointSummary,
    InterfaceConfig,
)
from app.schemas.bandwidth import (
    InterfaceTelemetryItem,
    InterfaceTelemetryResponse,
    EnrollExporterRequest,
    FlowExporterItem,
)
from monitoring.flow.aggregator import FlowAggregator, SENTINEL_UUID

test_app = FastAPI()
test_app.include_router(bandwidth_router, prefix="/api/v1")
test_app.include_router(reports_router, prefix="/api/v1")


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


# ==============================================================================
# 1. Schema & Polymorphic Alias Validation Tests
# ==============================================================================


def test_endpoint_device_role_and_defaults():
    now_dt = datetime.now(timezone.utc)
    # Standard role default
    ep = EndpointDetail(
        id=uuid4(),
        hostname="router1",
        ip_address="10.0.0.1",
        device_type="Router",
        endpoint_status="ACTIVE",
        current_operational_state="UP",
        current_detailed_state="UP",
        current_health_score=100.0,
        uptime_percentage_24h=100.0,
        monitoring_enabled=True,
        created_at=now_dt,
        updated_at=now_dt,
    )
    assert ep.device_role == "ACTIVE_HOST"

    # Dedicated flow exporter role
    ep_exp = EndpointDetail(
        id=uuid4(),
        hostname="edge-router",
        ip_address="10.1.1.1",
        device_type="Router",
        endpoint_status="ACTIVE",
        current_operational_state="PASSIVE",
        current_detailed_state="PASSIVE",
        current_health_score=100.0,
        uptime_percentage_24h=100.0,
        device_role="FLOW_EXPORTER",
        monitoring_enabled=False,
        created_at=now_dt,
        updated_at=now_dt,
    )
    assert ep_exp.device_role == "FLOW_EXPORTER"
    assert ep_exp.monitoring_enabled is False
    assert ep_exp.current_operational_state == "PASSIVE"


def test_endpoint_polymorphic_flow_interface_aliases():
    now_dt = datetime.now(timezone.utc)
    # Legacy format: {"1": "GigabitEthernet0/1", "2": "TenGigabit0/2"}
    legacy_data = {
        "id": uuid4(),
        "hostname": "legacy-router",
        "ip_address": "192.168.1.1",
        "device_type": "Router",
        "endpoint_status": "ACTIVE",
        "current_operational_state": "PASSIVE",
        "current_detailed_state": "PASSIVE",
        "current_health_score": 100.0,
        "uptime_percentage_24h": 100.0,
        "monitoring_enabled": False,
        "created_at": now_dt,
        "updated_at": now_dt,
        "flow_interface_aliases": {
            "1": "GigabitEthernet0/1",
            "2": "TenGigabit0/2",
        },
    }
    ep = EndpointDetail(**legacy_data)
    assert ep.flow_interface_aliases["1"].name == "GigabitEthernet0/1"
    assert ep.flow_interface_aliases["1"].speed_mbps == 1000  # Default 1Gbps
    assert ep.flow_interface_aliases["2"].name == "TenGigabit0/2"
    assert ep.flow_interface_aliases["2"].speed_mbps == 1000

    # Modern v3.3.0 format: {"1": {"name": "WAN", "speed_mbps": 10000}}
    v33_data = {
        "id": uuid4(),
        "hostname": "v33-router",
        "ip_address": "10.10.10.1",
        "device_type": "Router",
        "endpoint_status": "ACTIVE",
        "current_operational_state": "PASSIVE",
        "current_detailed_state": "PASSIVE",
        "current_health_score": 100.0,
        "uptime_percentage_24h": 100.0,
        "monitoring_enabled": False,
        "created_at": now_dt,
        "updated_at": now_dt,
        "flow_interface_aliases": {
            "1": {"name": "WAN", "speed_mbps": 10000},
            "2": {"name": "LAN", "speed_mbps": 1000},
        },
    }
    ep2 = EndpointDetail(**v33_data)
    assert ep2.flow_interface_aliases["1"].name == "WAN"
    assert ep2.flow_interface_aliases["1"].speed_mbps == 10000
    assert ep2.flow_interface_aliases["2"].name == "LAN"
    assert ep2.flow_interface_aliases["2"].speed_mbps == 1000


def test_operational_state_passive_and_unmonitored():
    summary_passive = EndpointSummary(
        id=uuid4(),
        hostname="router-passive",
        ip_address="10.0.0.2",
        device_type="Router",
        endpoint_status="ACTIVE",
        current_operational_state="PASSIVE",
        current_detailed_state="PASSIVE",
        current_health_score=100.0,
        uptime_percentage_24h=100.0,
        device_role="FLOW_EXPORTER",
    )
    assert summary_passive.current_operational_state == "PASSIVE"

    summary_unmon = EndpointSummary(
        id=uuid4(),
        hostname="router-unmon",
        ip_address="10.0.0.3",
        device_type="Router",
        endpoint_status="ACTIVE",
        current_operational_state="UNMONITORED",
        current_detailed_state="UNMONITORED",
        current_health_score=100.0,
        uptime_percentage_24h=100.0,
    )
    assert summary_unmon.current_operational_state == "UNMONITORED"


def test_flow_exporter_item_polymorphic():
    # Verify FlowExporterItem can serialize legacy string aliases
    item = FlowExporterItem(
        id=uuid4(),
        hostname="core-gw",
        primary_ip="172.16.0.1",
        device_role="FLOW_EXPORTER",
        flow_exporter_ips=["172.16.0.2"],
        interface_aliases={"1": "Gig0/1", "2": {"name": "10G-Uplink", "speed_mbps": 10000}},
    )
    assert item.device_role == "FLOW_EXPORTER"
    assert item.interface_aliases["1"]["name"] == "Gig0/1"
    assert item.interface_aliases["1"]["speed_mbps"] == 1000
    assert item.interface_aliases["2"]["name"] == "10G-Uplink"
    assert item.interface_aliases["2"]["speed_mbps"] == 10000


# ==============================================================================
# 2. FlowAggregator Interface Attribution & Bounds Guards
# ==============================================================================


def test_flow_aggregator_interface_attribution():
    aggregator = FlowAggregator(
        redis_client=MagicMock(), correlator=MagicMock(), db_session_factory=MagicMock()
    )
    exporter_id = uuid4()
    now_dt = datetime.now(timezone.utc)
    bucket = now_dt.replace(second=0, microsecond=0)

    # Ingress interface flow (in_if=1, out_if=0)
    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=exporter_id,
        in_if=1,
        out_if=0,
        flow_bytes=10000,
        flow_packets=10,
    )

    # Egress interface flow (in_if=0, out_if=2)
    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=exporter_id,
        in_if=0,
        out_if=2,
        flow_bytes=20000,
        flow_packets=20,
    )

    # Both interfaces flow (in_if=1, out_if=2)
    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=exporter_id,
        in_if=1,
        out_if=2,
        flow_bytes=5000,
        flow_packets=5,
    )

    key_in = (bucket, exporter_id, 1)
    key_out = (bucket, exporter_id, 2)

    # if1 was in_if twice: 10000 + 5000 = 15000 in_bytes, 0 out_bytes, 2 flows
    assert aggregator._interface_accumulator[key_in][0] == 15000
    assert aggregator._interface_accumulator[key_in][1] == 0
    assert aggregator._interface_accumulator[key_in][2] == 15
    assert aggregator._interface_accumulator[key_in][3] == 0
    assert aggregator._interface_accumulator[key_in][4] == 2

    # if2 was out_if twice: 0 in_bytes, 20000 + 5000 = 25000 out_bytes, 2 flows
    assert aggregator._interface_accumulator[key_out][0] == 0
    assert aggregator._interface_accumulator[key_out][1] == 25000
    assert aggregator._interface_accumulator[key_out][2] == 0
    assert aggregator._interface_accumulator[key_out][3] == 25
    assert aggregator._interface_accumulator[key_out][4] == 2


def test_flow_aggregator_sentinel_and_bounds_rejection():
    aggregator = FlowAggregator(
        redis_client=MagicMock(), correlator=MagicMock(), db_session_factory=MagicMock()
    )
    valid_id = uuid4()
    now_dt = datetime.now(timezone.utc)
    bucket = now_dt.replace(second=0, microsecond=0)

    # Reject SENTINEL_UUID
    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=SENTINEL_UUID,
        in_if=1,
        out_if=2,
        flow_bytes=1000,
        flow_packets=1,
    )
    assert len(aggregator._interface_accumulator) == 0

    # Reject invalid bounds (0 or negative or > 2147483647)
    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=valid_id,
        in_if=0,
        out_if=-5,
        flow_bytes=1000,
        flow_packets=1,
    )
    assert len(aggregator._interface_accumulator) == 0

    aggregator.accumulate_interface_flow(
        bucket=bucket,
        exporter_id=valid_id,
        in_if=2147483648,  # Exceeds signed 32-bit int
        out_if=0,
        flow_bytes=1000,
        flow_packets=1,
    )
    assert len(aggregator._interface_accumulator) == 0


# ==============================================================================
# 3. Bandwidth Router Endpoints Tests
# ==============================================================================


def test_get_interface_telemetry(client):
    test_client, mock_session = client
    exporter_id = uuid4()

    # Mock endpoint query
    mock_ep = MagicMock()
    mock_ep.id = exporter_id
    mock_ep.hostname = "Core-Router"
    mock_ep.device_role = "FLOW_EXPORTER"
    mock_ep.flow_interface_aliases = {
        "1": {"name": "Gi0/1", "speed_mbps": 1000},
        "2": {"name": "Ten0/1", "speed_mbps": 10000},
    }

    mock_ep_res = MagicMock()
    mock_ep_res.scalar_one_or_none.return_value = mock_ep

    # Coverage query: first bucket with data in the window (here: the whole hour).
    import datetime as _dt
    mock_cov_res = MagicMock()
    mock_cov_res.scalar.return_value = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)

    # Interface rollups query: (if_idx, in, out, in_pkts, out_pkts, flows, peak_in, peak_out,
    # sorted per-minute in_bytes, sorted per-minute out_bytes)
    mock_rollup_rows = [
        (1, 75000000, 37500000, 50000, 25000, 1000, 5000000, 2500000, [1250000] * 60, [625000] * 60),
        (2, 500000000, 250000000, 300000, 150000, 5000, 9000000, 4500000, [8333333] * 60, [4166666] * 60),
    ]
    mock_rollups_res = MagicMock()
    mock_rollups_res.all.return_value = mock_rollup_rows

    mock_session.execute.side_effect = [mock_ep_res, mock_cov_res, mock_rollups_res]

    resp = test_client.get(f"/api/v1/bandwidth/interfaces?exporter_id={exporter_id}&window=1h")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["data"]["exporter_id"] == str(exporter_id)
    assert len(data["data"]["interfaces"]) == 2

    if1 = next(i for i in data["data"]["interfaces"] if i["interface_idx"] == 1)
    assert if1["name"] == "Gi0/1"
    assert if1["speed_mbps"] == 1000
    assert if1["in_bps"] > 0
    assert if1["out_bps"] > 0
    assert 0 <= if1["utilization_percentage"] <= 100
    # 75 MB over the covered hour -> 166,667 bps; busiest minute 5 MB -> 666,667 bps
    assert if1["in_bps"] == pytest.approx(75_000_000 * 8 / 3600, rel=0.01)
    assert if1["in_peak_bps"] == pytest.approx(5_000_000 * 8 / 60, rel=0.001)
    assert if1["in_p95_bps"] == pytest.approx(1_250_000 * 8 / 60, rel=0.001)
    assert data["data"]["covered_seconds"] == pytest.approx(3600, abs=2)

    if2 = next(i for i in data["data"]["interfaces"] if i["interface_idx"] == 2)
    assert if2["name"] == "Ten0/1"
    assert if2["speed_mbps"] == 10000


def test_enroll_flow_exporter(client):
    test_client, mock_session = client

    # Mock no existing endpoint found
    mock_find_res = MagicMock()
    mock_find_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_find_res

    # Mock Redis client
    mock_redis = AsyncMock()
    with patch("app.routers.bandwidth.get_flow_redis", return_value=mock_redis):
        payload = {
            "ip_address": "192.168.100.1",
            "hostname": "Branch-Edge-01",
            "device_type": "Router",
            "location": "HQ Data Center",
            "description": "Primary border router",
            "interface_aliases": {
                "1": {"name": "WAN", "speed_mbps": 1000},
                "2": {"name": "LAN", "speed_mbps": 10000},
            },
        }
        resp = test_client.post("/api/v1/bandwidth/exporters/enroll", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["data"]["hostname"] == "Branch-Edge-01"
        assert data["data"]["device_role"] == "FLOW_EXPORTER"
        assert data["data"]["monitoring_enabled"] is False

        # Verify added to session and committed
        assert mock_session.add.called
        assert mock_session.commit.called

        # Verify pruned from Redis unmatched set
        assert mock_redis.zrem.called


def test_update_endpoint_interfaces(client):
    test_client, mock_session = client
    ep_id = uuid4()

    mock_ep = MagicMock()
    mock_ep.id = ep_id
    mock_ep.hostname = "Core-SW"
    mock_ep.flow_interface_aliases = {}

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = mock_ep
    mock_session.execute.return_value = mock_res

    interfaces_payload = {
        "1": {"name": "Uplink-Core", "speed_mbps": 10000},
        "2": {"name": "Access-VLAN10", "speed_mbps": 1000},
    }

    resp = test_client.put(f"/api/v1/bandwidth/endpoints/{ep_id}/interfaces", json=interfaces_payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["data"]["flow_interface_aliases"]["1"]["speed_mbps"] == 10000
    assert mock_session.commit.called


def test_bandwidth_top_talkers_with_exporter_id(client):
    from tests.backend.flow_mocks import conversation_row, talker_row, top_talkers_results

    test_client, mock_session = client
    exporter_id = uuid4()

    mock_session.execute.side_effect = top_talkers_results(
        talkers=[talker_row("10.0.0.5", exporter_id, ingress=0, egress=5000000, flows=500)],
        hostnames=[(exporter_id, "Edge-Router")],
        conversations=[conversation_row("10.0.0.5", "10.0.0.20", 6, 443, 5000000, 500)],
        conversation_hostnames=[],
    )

    resp = test_client.get(f"/api/v1/bandwidth/top-talkers?window=1h&exporter_id={exporter_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert len(data["data"]["top_endpoints"]) == 1
    assert len(data["data"]["top_conversations"]) == 1
    assert data["data"]["top_conversations"][0]["src_ip"] == "10.0.0.5"
    # The exporter filter reaches both the talker and the conversation SQL
    sqls = [str(c.args[0]) for c in mock_session.execute.call_args_list]
    assert sum("exporter_id = CAST(:exporter_id AS uuid)" in q for q in sqls) >= 2


# ==============================================================================
# 4. Reports SLA Exclusion for FLOW_EXPORTER
# ==============================================================================


def test_reports_sla_excludes_flow_exporter(client):
    test_client, mock_session = client

    # Create 1 normal endpoint and 1 flow exporter endpoint
    ep_std = MagicMock()
    ep_std.id = uuid4()
    ep_std.hostname = "std-host"
    ep_std.ip_address = "10.0.0.1"
    ep_std.device_type = "Server"
    ep_std.location = "Rack 1"
    ep_std.description = "Standard Host"
    ep_std.monitoring_enabled = True
    ep_std.endpoint_status = "ACTIVE"
    ep_std.device_role = "STANDARD"
    ep_std.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    ep_flow = MagicMock()
    ep_flow.id = uuid4()
    ep_flow.hostname = "flow-router"
    ep_flow.ip_address = "10.0.0.2"
    ep_flow.device_type = "Router"
    ep_flow.location = "Gateway"
    ep_flow.description = "Flow Exporter"
    ep_flow.monitoring_enabled = False
    ep_flow.endpoint_status = "ACTIVE"
    ep_flow.device_role = "FLOW_EXPORTER"
    ep_flow.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_ep_res = MagicMock()
    mock_ep_res.scalars.return_value.all.return_value = [ep_std, ep_flow]

    # Mock CTE results with stats
    m_cte = MagicMock()
    m_cte.mappings.return_value = [
        {
            "endpoint_id": str(ep_std.id),
            "up_seconds": 24 * 86400,
            "down_seconds": 0,
            "up_count": 100,
            "down_count": 0,
            "incident_count": 0,
            "latest_operational_state": "UP",
            "latest_detailed_state": "UP",
        }
    ]

    mock_session.execute.side_effect = [mock_ep_res, m_cte]

    with patch("app.routers.reports.get_service_gap_intervals", new_callable=AsyncMock, return_value=[]):
        resp = test_client.get("/api/v1/reports/fleet-summary?start_date=2026-09-01T00:00:00Z&end_date=2026-09-25T00:00:00Z")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["data"]["active_endpoints_count"] == 1
        assert data["data"]["total_endpoints_count"] == 2
        assert data["data"]["fleet_sla"] == 100.0


def test_reports_monitored_flow_exporter_reports_probed_state(client):
    """A flow exporter with ICMP monitoring enabled is not PASSIVE: real state, counted in SLA."""
    test_client, mock_session = client

    ep_flow = MagicMock()
    ep_flow.id = uuid4()
    ep_flow.hostname = "flow-router"
    ep_flow.ip_address = "10.0.0.2"
    ep_flow.device_type = "Router"
    ep_flow.location = "Gateway"
    ep_flow.description = "Flow Exporter"
    ep_flow.monitoring_enabled = True
    ep_flow.endpoint_status = "ACTIVE"
    ep_flow.device_role = "FLOW_EXPORTER"
    ep_flow.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    mock_ep_res = MagicMock()
    mock_ep_res.scalars.return_value.all.return_value = [ep_flow]
    m_cte = MagicMock()
    m_cte.mappings.return_value = [
        {
            "endpoint_id": str(ep_flow.id),
            "up_seconds": 12 * 86400,
            "down_seconds": 12 * 86400,
            "up_count": 50,
            "down_count": 50,
            "incident_count": 3,
            "latest_operational_state": "DOWN",
            "latest_detailed_state": "DOWN",
        }
    ]
    mock_session.execute.side_effect = [mock_ep_res, m_cte]

    with patch("app.routers.reports.get_service_gap_intervals", new_callable=AsyncMock, return_value=[]):
        resp = test_client.get("/api/v1/reports/fleet-summary?start_date=2026-09-01T00:00:00Z&end_date=2026-09-25T00:00:00Z")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["active_endpoints_count"] == 1
    summary = data["endpoints"][0]
    assert summary["operational_state"] == "DOWN"
    assert summary["incident_count"] == 3
    assert data["fleet_sla"] < 100.0


def test_enroll_flow_exporter_rejects_non_exporter_role(client):
    """An invalid device_role is a 422 validation error, not an IntegrityError from the DB check constraint."""
    test_client, mock_session = client
    resp = test_client.post(
        "/api/v1/bandwidth/exporters/enroll",
        json={"ip_address": "192.168.100.2", "hostname": "edge-02", "device_role": "ROUTER"},
    )
    assert resp.status_code == 422
    mock_session.execute.assert_not_called()
    mock_session.add.assert_not_called()
