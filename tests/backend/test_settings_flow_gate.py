"""Turning flow ingestion on in PATCH /settings.

It used to need Performance Mode (and refused to turn Performance Mode off while flows were on)
plus a FastStore stream write: left over from when flowd buffered flows in a FastStore stream.
flowd runs in either mode; turning ingestion on now only needs the flow preflight to pass.
"""
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


def _db_with(mock_session, **current):
    """The settings already saved, as _read_settings_dict reads them."""
    rows = []
    for key, value in current.items():
        row = MagicMock()
        row.setting_key, row.setting_value = key, value
        rows.append(row)
    res = MagicMock()
    res.scalars.return_value.all.return_value = rows
    res.scalar_one_or_none.return_value = None
    mock_session.execute.return_value = res


@pytest.fixture
def no_side_effects():
    """No systemctl, driver re-initialisation or broadcasts from the route under test."""
    with patch("app.routers.settings.manage_flow_service", AsyncMock()), \
         patch("app.services.driver_manager.driver_manager.initialize", AsyncMock()), \
         patch("app.services.settings_sync.broadcast_settings_reload", AsyncMock()):
        yield


def test_flow_ingestion_turns_on_without_performance_mode(client, no_side_effects):
    test_client, mock_session = client
    _db_with(mock_session, performance_mode="false", flow_ingestion_enabled="false")
    with patch("app.routers.settings.check_flow_prerequisites", AsyncMock(return_value=(True, "Ready"))) as check:
        resp = test_client.patch("/api/v1/settings", json={"flow_ingestion_enabled": True})
    assert resp.status_code == 200
    check.assert_awaited_once()


def test_failing_preflight_refuses_flow_ingestion(client, no_side_effects):
    test_client, mock_session = client
    _db_with(mock_session, flow_ingestion_enabled="false")
    with patch("app.routers.settings.check_flow_prerequisites",
               AsyncMock(return_value=(False, "UDP port(s) already in use: 2055."))):
        resp = test_client.patch("/api/v1/settings", json={"flow_ingestion_enabled": True})
    assert resp.status_code == 400
    assert "preflight check failed" in resp.json()["detail"] and "2055" in resp.json()["detail"]


def test_performance_mode_turns_off_while_flows_are_on(client, no_side_effects):
    test_client, mock_session = client
    _db_with(mock_session, performance_mode="true", flow_ingestion_enabled="true")
    with patch("app.routers.settings.check_flow_prerequisites", AsyncMock()) as check:
        resp = test_client.patch("/api/v1/settings", json={"performance_mode": False})
    assert resp.status_code == 200
    check.assert_not_awaited()  # nothing about flows changed


def test_preflight_runs_only_when_turning_flows_on(client, no_side_effects):
    """Saving other settings, or re-saving with flows already on, doesn't re-run it (its port
    check would otherwise be repeated against a running flowd for every unrelated change)."""
    test_client, mock_session = client
    _db_with(mock_session, flow_ingestion_enabled="true")
    with patch("app.routers.settings.check_flow_prerequisites", AsyncMock()) as check:
        assert test_client.patch("/api/v1/settings", json={"flow_ingestion_enabled": True}).status_code == 200
        assert test_client.patch("/api/v1/settings", json={"session_timeout": 60}).status_code == 200
    check.assert_not_awaited()


def test_preflight_checks_the_ports_being_saved(client, no_side_effects):
    test_client, mock_session = client
    _db_with(mock_session, flow_ingestion_enabled="false")
    with patch("app.routers.settings.check_flow_prerequisites", AsyncMock(return_value=(True, "Ready"))) as check:
        resp = test_client.patch(
            "/api/v1/settings", json={"flow_ingestion_enabled": True, "flowNetflowPort": 9995, "flow_ipfix_port": 9996}
        )
    assert resp.status_code == 200
    check.assert_awaited_once_with(9995, 9996)
