from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.core.asgi import FastAPI
from app.core.asgi import TestClient

from app.database import get_db
from app.routers.auth import get_current_user, require_admin
from app.routers.settings import router as settings_router

test_app = FastAPI()
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
    mock_session.add = MagicMock()

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


def test_rule_a_flow_without_perf_mode_rejected(client):
    """
    Rule A: PATCH /api/v1/settings with flow_ingestion_enabled: True when performance_mode: False
    must be rejected with HTTP 400 Bad Request.
    """
    test_client, mock_session = client

    # Mock current settings in DB: performance_mode=False, flow_ingestion_enabled=False
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_res

    payload = {"flow_ingestion_enabled": True, "performance_mode": False}
    resp = test_client.patch("/api/v1/settings", json=payload)
    assert resp.status_code == 400
    assert "Redis Memory Acceleration must be activated first" in resp.json()["detail"]


def test_rule_b_flow_with_failing_redis_rejected(client):
    """
    Rule B: PATCH /api/v1/settings with flow_ingestion_enabled: True when Redis prerequisite
    fails (e.g. unreachable) must be rejected with HTTP 400 Bad Request.
    """
    test_client, mock_session = client

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_res

    with patch("app.routers.settings.check_redis_flow_prerequisites", AsyncMock(return_value=(False, "Connection refused"))):
        payload = {"flow_ingestion_enabled": True, "performance_mode": True}
        resp = test_client.patch("/api/v1/settings", json=payload)
        assert resp.status_code == 400
        assert "Redis prerequisite check failed" in resp.json()["detail"]


def test_rule_b_flow_with_healthy_redis_accepted(client):
    """
    Rule B (Success): PATCH /api/v1/settings with flow_ingestion_enabled: True and performance_mode: True
    with healthy Redis must return 200 OK.
    """
    test_client, mock_session = client

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = mock_res

    with patch("app.routers.settings.check_redis_flow_prerequisites", AsyncMock(return_value=(True, "Redis v7.0.12 ready"))):
        with patch("app.services.driver_manager.driver_manager.initialize", AsyncMock()):
            with patch("app.services.settings_sync.broadcast_settings_reload", AsyncMock()):
                payload = {"flow_ingestion_enabled": True, "performance_mode": True}
                resp = test_client.patch("/api/v1/settings", json=payload)
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "success"


def test_rule_c_disable_redis_while_flow_active_rejected(client):
    """
    Rule C: PATCH /api/v1/settings with performance_mode: False when flow_ingestion_enabled
    is currently active must be rejected with HTTP 400 Bad Request.
    """
    test_client, mock_session = client

    # Mock DB where flow_ingestion_enabled is currently "true"
    setting_flow = MagicMock()
    setting_flow.setting_key = "flow_ingestion_enabled"
    setting_flow.setting_value = "true"

    setting_perf = MagicMock()
    setting_perf.setting_key = "performance_mode"
    setting_perf.setting_value = "true"

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [setting_flow, setting_perf]
    mock_session.execute.return_value = mock_res

    # Try disabling Redis without disabling Flow Ingestion
    payload = {"performance_mode": False}
    resp = test_client.patch("/api/v1/settings", json=payload)
    assert resp.status_code == 400
    assert "Cannot disable Redis while Network Flow Telemetry is enabled" in resp.json()["detail"]
