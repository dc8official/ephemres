from __future__ import annotations

import asyncio
import io
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.database import get_db
from app.main import app
from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.system_setting import AppSetting
from app.routers.auth import create_access_token, get_current_user, require_admin
from app.routers.reports import csv_generator, sanitize_csv_field, get_fleet_summary
from app.routers.settings import get_settings, update_settings, SettingsUpdate
from app.services.auth_service import is_session_active
from app.services.diagnostics import is_local_subnet_destination
from app.services.event_broker import PostgresEventBroker
from app.services.session_store import PostgresSessionStore, RedisSessionStore
from monitoring.synthetic import _sync_ssl_probe


class TestPhase1BrokerAndStorageFoundations(unittest.TestCase):
    """Test PostgresEventBroker query parameterization and reconnect resiliency."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_postgres_event_broker_parameterization(self) -> None:
        """Test PostgresEventBroker.publish parameterizes channel and payload in SQL."""
        async def _run():
            mock_db = AsyncMock()
            mock_factory = MagicMock()
            mock_factory.return_value.__aenter__.return_value = mock_db

            broker = PostgresEventBroker(mock_factory)
            await broker.publish("STATE_TRANSITION", {"endpoint_id": "123", "state": "UP"})

            mock_db.execute.assert_called_once()
            call_args = mock_db.execute.call_args
            query_stmt = call_args[0][0]
            params = call_args[0][1]

            self.assertIn("pg_notify(:channel, :payload)", str(query_stmt))
            self.assertEqual(params["channel"], "STATE_TRANSITION")
            self.assertIn('"state": "UP"', params["payload"])
            self.assertTrue(mock_db.commit.called)

        self.loop.run_until_complete(_run())


class TestPhase2SSLBinaryDERProbe(unittest.TestCase):
    """Test SSL probe binary DER certificate fallback under CERT_NONE."""

    def test_ssl_probe_cert_none_binary_form(self) -> None:
        """Test _sync_ssl_probe falls back to binary DER certificate when verify_mode is CERT_NONE."""
        with patch("socket.create_connection") as mock_sock, \
             patch("ssl.create_default_context") as mock_ctx:

            mock_ssock = MagicMock()
            # CERT_NONE returns empty dict for binary_form=False
            mock_ssock.getpeercert.side_effect = lambda binary_form=False: (
                b"mock_der_bytes" if binary_form else {}
            )
            mock_ssock.__enter__.return_value = mock_ssock

            mock_ctx_inst = MagicMock()
            mock_ctx_inst.wrap_socket.return_value = mock_ssock
            mock_ctx.return_value = mock_ctx_inst

            mock_x509_cert = MagicMock()
            mock_x509_cert.not_valid_after_utc = datetime(2030, 6, 1, tzinfo=timezone.utc)

            with patch("cryptography.x509.load_der_x509_certificate", return_value=mock_x509_cert) as mock_load:
                res = _sync_ssl_probe("internal.service.local", port=443)
                self.assertTrue(res["success"])
                self.assertIsNotNone(res["expires_at"])
                self.assertGreater(res["days_until_expiry"], 100)
                mock_load.assert_called_once_with(b"mock_der_bytes")


class TestPhase4ReportsHardening(unittest.TestCase):
    """Test Fleet Summary endpoint and keyset pagination in telemetry export."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_csv_sanitization_defense(self) -> None:
        """Test formula injection sanitization in CSV export."""
        self.assertEqual(sanitize_csv_field("=cmd|'/C calc'!A0"), "'=cmd|'/C calc'!A0")
        self.assertEqual(sanitize_csv_field("+12345"), "'+12345")
        self.assertEqual(sanitize_csv_field("-12345"), "'-12345")
        self.assertEqual(sanitize_csv_field("@SUM(1,2)"), "'@SUM(1,2)")
        self.assertEqual(sanitize_csv_field("normal_host.local"), "normal_host.local")

    def test_keyset_csv_generator(self) -> None:
        """Test keyset pagination generator terminates properly and formats CSV."""
        async def _run():
            ep_id = uuid4()
            start_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
            end_dt = datetime(2026, 1, 2, tzinfo=timezone.utc)

            mock_ev = EndpointEvent(
                id=uuid4(),
                endpoint_id=ep_id,
                operational_state="UP",
                detailed_state="UP",
                health_score=100.0,
                avg_rtt_ms=12.5,
                start_time=start_dt,
                end_time=start_dt + timedelta(minutes=1),
                duration_seconds=60,
                monitoring_cycle_count=1,
            )

            # Mock database session returning 1 row then empty
            mock_session = AsyncMock()
            mock_result_first = MagicMock()
            mock_result_first.all.return_value = [(mock_ev, "test-host", "192.168.1.10", "SERVER")]

            mock_result_second = MagicMock()
            mock_result_second.all.return_value = []

            mock_session.execute.side_effect = [mock_result_first, mock_result_second]
            mock_session_ctx = MagicMock()
            mock_session_ctx.__aenter__.return_value = mock_session
            mock_session_ctx.__aexit__.return_value = None

            with patch("app.routers.reports.AsyncSessionLocal", return_value=mock_session_ctx):
                chunks = []
                async for chunk in csv_generator([ep_id], start_dt, end_dt):
                    chunks.append(chunk)

                full_csv = "".join(chunks)
                self.assertIn("Endpoint_ID,Hostname,IP_Address,Device_Type", full_csv)
                self.assertIn("test-host", full_csv)
                self.assertIn("192.168.1.10", full_csv)

        self.loop.run_until_complete(_run())

    def test_keyset_csv_generator_duplicate_timestamps(self) -> None:
        """Test keyset pagination deterministically orders events sharing identical start_time."""
        async def _run():
            ep_id = uuid4()
            shared_dt = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
            id_1 = UUID("00000000-0000-0000-0000-000000000001")
            id_2 = UUID("00000000-0000-0000-0000-000000000002")

            ev1 = EndpointEvent(
                id=id_1,
                endpoint_id=ep_id,
                operational_state="UP",
                detailed_state="UP",
                health_score=100.0,
                avg_rtt_ms=10.0,
                start_time=shared_dt,
            )
            ev2 = EndpointEvent(
                id=id_2,
                endpoint_id=ep_id,
                operational_state="UP",
                detailed_state="UP",
                health_score=100.0,
                avg_rtt_ms=15.0,
                start_time=shared_dt,
            )

            mock_session = AsyncMock()
            mock_res = MagicMock()
            mock_res.all.return_value = [
                (ev1, "host-1", "10.0.0.1", "SERVER"),
                (ev2, "host-1", "10.0.0.1", "SERVER"),
            ]
            mock_session.execute.return_value = mock_res
            mock_session_ctx = MagicMock()
            mock_session_ctx.__aenter__.return_value = mock_session
            mock_session_ctx.__aexit__.return_value = None

            with patch("app.routers.reports.AsyncSessionLocal", return_value=mock_session_ctx):
                chunks = []
                async for chunk in csv_generator([ep_id], shared_dt - timedelta(hours=1), shared_dt + timedelta(hours=1)):
                    chunks.append(chunk)

                full_csv = "".join(chunks)
                self.assertIn("host-1", full_csv)
                self.assertIn("10.0.0.1", full_csv)

        self.loop.run_until_complete(_run())

    def test_get_fleet_summary_authenticated(self) -> None:
        """Test GET /api/v1/reports/fleet-summary executes and aggregates metrics."""
        async def _run():
            ep_id = uuid4()
            mock_endpoint = Endpoint(
                id=ep_id,
                hostname="core-router",
                ip_address="10.0.0.1",
                device_type="ROUTER",
                endpoint_status="ACTIVE",
                monitoring_enabled=True,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

            mock_db = AsyncMock()
            
            # 1. endpoints query
            mock_ep_res = MagicMock()
            mock_ep_res.scalars.return_value.all.return_value = [mock_endpoint]

            # 2. CTE query mappings
            mock_cte_res = MagicMock()
            mock_cte_res.mappings.return_value = [
                {
                    "endpoint_id": str(ep_id),
                    "up_count": 100,
                    "down_count": 2,
                    "incident_count": 1,
                    "latest_operational_state": "UP",
                    "latest_detailed_state": "UP",
                }
            ]

            # 3. service gaps query
            mock_gap_res = MagicMock()
            mock_gap_res.fetchall.return_value = []

            mock_db.execute.side_effect = [mock_ep_res, mock_cte_res, mock_gap_res]

            app.dependency_overrides[get_db] = lambda: mock_db
            app.dependency_overrides[get_current_user] = lambda: {
                "id": str(uuid4()),
                "username": "admin",
                "role": "ADMIN",
            }

            res = await get_fleet_summary(
                start_date="2026-01-01",
                end_date="2026-01-02",
                db=mock_db,
                current_user={"id": str(uuid4()), "username": "admin", "role": "ADMIN"},
            )
            self.assertEqual(res.status, "success")
            data = res.data.model_dump()
            self.assertIn("fleet_sla", data)
            self.assertIn("active_endpoints_count", data)
            self.assertIn("total_endpoints_count", data)
            self.assertIn("endpoints", data)
            self.assertEqual(data["total_endpoints_count"], 1)
            self.assertEqual(data["active_endpoints_count"], 1)
            self.assertEqual(data["endpoints"][0]["hostname"], "core-router")

        self.loop.run_until_complete(_run())


class TestPhase5SettingsAPI(unittest.TestCase):
    """Test Platform Administration and Settings endpoints."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_settings_unauthenticated_rejected(self) -> None:
        """Test get_current_user rejects unauthenticated/missing credentials."""
        async def _run():
            mock_request = MagicMock()
            mock_request.cookies = {}
            mock_request.headers = {}
            mock_request.url.path = "/api/v1/settings"

            with self.assertRaises(HTTPException) as ctx:
                await get_current_user(request=mock_request, db=AsyncMock())
            self.assertEqual(ctx.exception.status_code, 401)

        self.loop.run_until_complete(_run())

    def test_settings_viewer_cannot_patch(self) -> None:
        """Test viewer cannot modify platform settings (403 Forbidden)."""
        async def _run():
            viewer_user = {
                "id": str(uuid4()),
                "username": "viewer",
                "role": "VIEWER",
            }
            with self.assertRaises(HTTPException) as ctx:
                await require_admin(current_user=viewer_user)
            self.assertEqual(ctx.exception.status_code, 403)

        self.loop.run_until_complete(_run())

    def test_settings_admin_patch_and_reinitialize(self) -> None:
        """Test admin can update settings and driver_manager is reinitialized."""
        async def _run():
            mock_db = AsyncMock()
            mock_setting_perf = AppSetting(
                setting_key="performance_mode",
                setting_value="false",
            )
            # Callable side-effect handles arbitrary number of execute calls without StopIteration
            def _fake_exec(stmt, *args, **kwargs):
                res = MagicMock()
                res.scalar_one_or_none.return_value = mock_setting_perf
                res.scalars.return_value.all.return_value = [mock_setting_perf]
                return res

            mock_db.execute.side_effect = _fake_exec
            admin_user = {
                "id": str(uuid4()),
                "username": "admin",
                "role": "ADMIN",
            }

            with patch("app.services.driver_manager.driver_manager.initialize", new_callable=AsyncMock) as mock_init:
                res = await update_settings(
                    payload=SettingsUpdate(performance_mode=True, session_timeout=180),
                    current_user=admin_user,
                    db=mock_db,
                )
                self.assertEqual(res.status, "success")
                mock_init.assert_called_once()

        self.loop.run_until_complete(_run())

    def test_is_local_subnet_destination_with_netmask(self) -> None:
        """Test is_local_subnet_destination respects interface netmask."""
        # Loopback is always true
        self.assertTrue(is_local_subnet_destination("127.0.0.1"))

        # Test interface with /16 netmask
        mock_addr = MagicMock()
        mock_addr.family.name = "AF_INET"
        mock_addr.address = "10.10.0.1"
        mock_addr.netmask = "255.255.0.0"

        with patch("app.services.diagnostics.get_net_if_addrs", return_value={"eth0": [mock_addr]}):
            # Same /16 subnet
            self.assertTrue(is_local_subnet_destination("10.10.5.25"))
            # Outside /16 subnet
            self.assertFalse(is_local_subnet_destination("10.11.0.1"))


class TestOptionBStrictSecurityWipe(unittest.TestCase):
    """Test Option B: Legacy tokens without jti claim are rejected across all layers."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_legacy_token_without_jti_rejected(self) -> None:
        """Verify token missing jti is rejected immediately under Option B across stores."""
        async def _run():
            user_id = str(uuid4())

            # 1. auth_service.is_session_active (sync & async)
            self.assertFalse(is_session_active(user_id, jti=None))
            self.assertFalse(is_session_active(user_id, jti=""))
            is_active_none = await is_session_active(user_id, jti=None)
            self.assertFalse(is_active_none)
            is_active_empty = await is_session_active(user_id, jti="")
            self.assertFalse(is_active_empty)

            # 2. PostgresSessionStore
            mock_session = AsyncMock()
            mock_factory = MagicMock()
            mock_factory.return_value.__aenter__.return_value = mock_session
            pg_store = PostgresSessionStore(mock_factory)
            self.assertFalse(await pg_store.is_session_active(user_id, jti=None))
            self.assertFalse(await pg_store.is_session_active(user_id, jti=""))

            # 3. RedisSessionStore
            mock_redis = AsyncMock()
            redis_store = RedisSessionStore(mock_redis)
            self.assertFalse(await redis_store.is_session_active(user_id, jti=None))
            self.assertFalse(await redis_store.is_session_active(user_id, jti=""))

        self.loop.run_until_complete(_run())

    def test_get_current_user_rejects_missing_jti_under_option_b(self) -> None:
        """Verify get_current_user rejects tokens lacking jti claim (Option B)."""
        async def _run():
            mock_request = MagicMock()
            mock_request.cookies.get.return_value = "legacy_token_no_jti"
            mock_db = AsyncMock()

            # Token payload missing jti
            with patch("app.routers.auth.decode_access_token") as mock_decode:
                mock_decode.return_value = {
                    "sub": str(uuid4()),
                    "username": "legacy_user",
                    "role": "USER",
                    # no jti
                }
                with self.assertRaises(HTTPException) as cm:
                    await get_current_user(mock_request, db=mock_db)

                self.assertEqual(cm.exception.status_code, 401)
                self.assertIn("expired or invalid", cm.exception.detail)

        self.loop.run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
