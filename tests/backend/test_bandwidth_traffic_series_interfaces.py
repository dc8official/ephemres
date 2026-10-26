import datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.core.asgi import FastAPI
from app.core.asgi import TestClient

from app.database import get_db
from app.routers.auth import get_current_user, require_admin
from app.routers.bandwidth import router as bandwidth_router

test_app = FastAPI()
test_app.include_router(bandwidth_router, prefix="/api/v1")


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


def test_traffic_series_exporter_queries_interface_rollup(client):
    test_client, mock_session = client
    exporter_id = uuid4()
    bucket_dt = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    # Simulate 11 Mbps transit traffic on interface rollup:
    # 11 Mbps = 11,000,000 bps -> in_bytes in 60s bucket = (11,000,000 * 60) / 8 = 82,500,000 bytes
    in_bytes = 82_500_000
    out_bytes = 41_250_000

    mock_row = (bucket_dt, in_bytes, out_bytes)
    mock_res = MagicMock()
    mock_res.all.return_value = [mock_row]
    mock_session.execute = AsyncMock(return_value=mock_res)

    res = test_client.get(f"/api/v1/bandwidth/traffic-series?window=1h&exporter_id={exporter_id}")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["window"] == "1h"
    assert len(data["points"]) == 1

    pt = data["points"][0]
    # 82,500,000 * 8 / 60 = 11,000,000 bps = 11.0 Mbps!
    assert pt["ingress_bps"] == 11000000.0
    # 41,250,000 * 8 / 60 = 5,500,000 bps = 5.5 Mbps
    assert pt["egress_bps"] == 5500000.0


def test_traffic_series_interface_idx_isolation(client):
    test_client, mock_session = client
    exporter_id = uuid4()
    bucket_dt = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    # Interface #2 specific traffic
    in_bytes = 15_000_000
    out_bytes = 7_500_000

    mock_row = (bucket_dt, in_bytes, out_bytes)
    mock_res = MagicMock()
    mock_res.all.return_value = [mock_row]
    mock_session.execute = AsyncMock(return_value=mock_res)

    res = test_client.get(
        f"/api/v1/bandwidth/traffic-series?window=1h&exporter_id={exporter_id}&interface_idx=2"
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data["points"]) == 1
    pt = data["points"][0]
    assert pt["ingress_bps"] == 2000000.0
    assert pt["egress_bps"] == 1000000.0


def test_traffic_series_fleet_transit_traffic_preservation(client):
    test_client, mock_session = client
    bucket_dt = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)

    # Fleet-wide rollup: 11 Mbps of transit (82.5 MB with no monitored endpoint on either side),
    # while monitored endpoints only exchanged 60 bytes each way.
    transit_bytes = 82_500_000
    monitored_in = 60
    monitored_eg = 60

    # (bucket, flow_count, ingress bytes/packets, egress bytes/packets, transit bytes/packets)
    mock_row = (bucket_dt, 3, monitored_in, 1, monitored_eg, 1, transit_bytes, 60_000)
    mock_res = MagicMock()
    mock_res.all.return_value = [mock_row]
    mock_session.execute = AsyncMock(return_value=mock_res)

    res = test_client.get("/api/v1/bandwidth/traffic-series?window=1h")
    assert res.status_code == 200
    data = res.json()["data"]
    assert len(data["points"]) == 1
    pt = data["points"][0]
    # The 11 Mbps of transit is preserved as its own series instead of being split 50/50 into
    # ingress/egress (v3.3.2.v behaviour) or dropped.
    assert pt["transit_bps"] == 11_000_000.0
    assert pt["ingress_bps"] == 8.0
    assert pt["egress_bps"] == 8.0
