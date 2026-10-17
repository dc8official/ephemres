from __future__ import annotations

import asyncio
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from app.core.schema import ValidationError

from app.config import ConfigurationError, Settings, load_settings
from app.models import (
    AppSetting,
    AuditLog,
    Endpoint,
    EndpointBaselineRoute,
    EndpointDiagnosticTrace,
    EndpointEvent,
    EndpointRCAIncident,
    Role,
    SystemSetting,
    User,
)
from app.repositories import (
    AuthRepository,
    BaseRepository,
    EndpointRepository,
    ReportRepository,
)
from app.schemas.auth import ChangePasswordRequest, LoginRequest, LoginResponse
from app.schemas.common import APIResponse, ErrorDetail, PaginationMeta
from app.schemas.endpoints import EndpointDetail, EndpointSummary
from app.schemas.events import EventRecord
from app.schemas.monitoring import MonitoringStatus
from app.schemas.reports import IncidentRecord, UptimeReport
from app.schemas.users import (
    CreateUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserSummary,
)


class TestDeclarativeModels(unittest.TestCase):
    """Test SQLAlchemy 2.0 declarative models and attributes."""

    def test_models_table_names_and_defaults(self) -> None:
        self.assertEqual(Endpoint.__tablename__, "endpoints")
        self.assertEqual(User.__tablename__, "users")
        self.assertEqual(Role.__tablename__, "roles")
        self.assertEqual(AuditLog.__tablename__, "audit_logs")
        self.assertEqual(EndpointEvent.__tablename__, "endpoint_events")
        self.assertEqual(AppSetting.__tablename__, "app_settings")
        self.assertEqual(SystemSetting.__tablename__, "app_settings")
        self.assertEqual(EndpointDiagnosticTrace.__tablename__, "endpoint_diagnostic_traces")
        self.assertEqual(EndpointBaselineRoute.__tablename__, "endpoint_baseline_routes")
        self.assertEqual(EndpointRCAIncident.__tablename__, "endpoint_rca_incidents")

    def test_endpoint_model_instantiation(self) -> None:
        ep_id = uuid4()
        ep = Endpoint(
            id=ep_id,
            ip_address="192.168.1.10",
            hostname="gateway-1",
            device_type="ROUTER",
            location="HQ-DC",
            monitoring_enabled=True,
            endpoint_status="ACTIVE",
            allow_incident_trace=True,
            allow_topology_discovery=True,
            enable_rca=True,
            enable_scheduled_discovery=True,
            is_l2_segment=False,
        )
        self.assertEqual(ep.id, ep_id)
        self.assertEqual(ep.ip_address, "192.168.1.10")
        self.assertEqual(ep.hostname, "gateway-1")
        self.assertTrue(ep.monitoring_enabled)

    def test_user_and_role_model_instantiation(self) -> None:
        role_id = uuid4()
        role = Role(id=role_id, role_name="ADMIN", description="Administrator")
        self.assertEqual(role.role_name, "ADMIN")

        user_id = uuid4()
        user = User(
            id=user_id,
            username="secadmin",
            password_hash="argon2id$...",
            role_id=role_id,
            is_active=True,
            must_change_password=True,
        )
        self.assertEqual(user.username, "secadmin")
        self.assertTrue(user.is_active)

    def test_audit_log_model_instantiation(self) -> None:
        target_id = uuid4()
        log = AuditLog(
            action="ENDPOINT:CREATE",
            target_type="endpoints",
            target_id=target_id,
            details={"ip": "10.0.0.1"},
        )
        self.assertEqual(log.action, "ENDPOINT:CREATE")
        self.assertEqual(log.details, {"ip": "10.0.0.1"})

    def test_endpoint_event_model_instantiation(self) -> None:
        ep_id = uuid4()
        now = datetime.now(timezone.utc)
        ev = EndpointEvent(
            endpoint_id=ep_id,
            operational_state="UP",
            detailed_state="UP",
            success_count=10,
            failed_count=0,
            health_score=100.0,
            avg_rtt_ms=5.4,
            start_time=now,
            monitoring_cycle_count=1,
        )
        self.assertEqual(ev.operational_state, "UP")
        self.assertEqual(ev.health_score, 100.0)


class TestRepositoryLayer(unittest.TestCase):
    """Test Repository Layer query construction and business logic."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_cycle_detection_self_parent(self) -> None:
        ep_id = uuid4()
        mock_db = AsyncMock()
        repo = EndpointRepository(mock_db)
        is_cycle = self.loop.run_until_complete(
            repo.detect_parent_cycle(ep_id, ep_id)
        )
        self.assertTrue(is_cycle)

    def test_cycle_detection_no_parent(self) -> None:
        ep_id = uuid4()
        mock_db = AsyncMock()
        repo = EndpointRepository(mock_db)
        is_cycle = self.loop.run_until_complete(
            repo.detect_parent_cycle(ep_id, None)
        )
        self.assertFalse(is_cycle)

    def test_cycle_detection_transitive_cycle(self) -> None:
        # A -> B -> C -> A
        a_id = uuid4()
        b_id = uuid4()
        c_id = uuid4()

        mock_db = AsyncMock()
        # First lookup for b_id returns c_id, then for c_id returns a_id
        res_b = MagicMock()
        res_b.scalar_one_or_none.return_value = c_id
        res_c = MagicMock()
        res_c.scalar_one_or_none.return_value = a_id

        mock_db.execute.side_effect = [res_b, res_c]

        repo = EndpointRepository(mock_db)
        is_cycle = self.loop.run_until_complete(
            repo.detect_parent_cycle(a_id, b_id)
        )
        self.assertTrue(is_cycle)

    def test_report_repository_pagination_limit_offset(self) -> None:
        mock_db = AsyncMock()
        repo = ReportRepository(mock_db)

        ep_id = uuid4()
        start = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)

        count_res = MagicMock()
        count_res.scalar.return_value = 150
        data_res = MagicMock()
        data_res.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [count_res, data_res]

        events, total = self.loop.run_until_complete(
            repo.get_events(ep_id, start, end, limit=25, offset=50)
        )

        self.assertEqual(total, 150)
        self.assertEqual(mock_db.execute.call_count, 2)
        # Verify LIMIT and OFFSET are passed to the SQL statement
        paginated_stmt = mock_db.execute.call_args[0][0]
        self.assertEqual(paginated_stmt._limit, 25)
        self.assertEqual(paginated_stmt._offset, 50)


class TestConfigModernization(unittest.TestCase):
    """Test modernized configuration loading and error handling."""

    def test_configuration_error_on_invalid_file(self) -> None:
        with patch.dict(os.environ, {"NETMON_CONFIG_PATH": "/tmp/does_not_exist_xyz.toml"}):
            with self.assertRaises(ConfigurationError) as ctx:
                load_settings()
            self.assertIn("does not exist", str(ctx.exception))

    def test_env_override_db_password(self) -> None:
        with patch.dict(os.environ, {"NETMON_DB_PASSWORD": "custom_super_secret_pw"}):
            cfg = load_settings()
            self.assertEqual(cfg.database.password, "custom_super_secret_pw")
            self.assertIn("custom_super_secret_pw", cfg.db_url)


class TestSchemasAndUTCBoundary(unittest.TestCase):
    """Test Pydantic v2 schemas and strict UTC datetime serialization."""

    def test_endpoint_summary_utc_serialization(self) -> None:
        now_utc = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
        ep = EndpointSummary(
            id=uuid4(),
            hostname="core-switch-1",
            ip_address="10.10.10.1",
            device_type="SWITCH",
            location="Building-A",
            endpoint_status="ACTIVE",
            current_operational_state="UP",
            current_detailed_state="UP",
            current_health_score=98.5,
            uptime_percentage_24h=99.9,
            last_seen=now_utc,
        )
        serialized = ep.model_dump(mode="json")
        self.assertIn("2026-08-31T12:00:00", serialized["last_seen"])

    def test_uptime_report_percentage_validator(self) -> None:
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValidationError):
            UptimeReport(
                endpoint_id=uuid4(),
                period_start=now,
                period_end=now,
                total_seconds=100,
                uptime_seconds=100,
                downtime_seconds=0,
                unknown_seconds=0,
                uptime_percentage=105.0,  # Invalid > 100
                incident_count=0,
            )

    def test_api_response_schema_generic(self) -> None:
        res = APIResponse.success(data={"key": "value"})
        self.assertTrue(res.success)
        self.assertEqual(res.status, "success")
        self.assertEqual(res.data, {"key": "value"})


if __name__ == "__main__":
    unittest.main()
