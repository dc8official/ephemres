import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import asyncio
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from app.core.schema import ValidationError

from app.core.asgi import HTTPException
from app.routers.auth import login, change_password, get_current_user, LoginRequest, ChangePasswordRequest
from app.routers.users import create_user, reset_password, update_user, delete_user, CreateUserRequest, ResetPasswordRequest, UpdateUserRequest
from app.routers.endpoints import CreateEndpointRequest, list_endpoints
from app.routers.reports import _validate_date_range, parse_datetime_param
from app.services.auth_service import hash_password, verify_password, is_account_locked, record_failed_attempt, clear_failed_attempts, _failed_attempts
from app.services.topology import TopologyGraphManager
from app.services.diagnostics import sanitize_traceroute_hops
from app.database import get_db
from monitoring.engine import safe_create_task, _active_background_tasks


class TestEndToEndSmoke(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        _failed_attempts.clear()

    def tearDown(self) -> None:
        _failed_attempts.clear()
        self.loop.close()

    # -------------------------------------------------------------
    # 1. Full Authentication & Password Security Lifecycle
    # -------------------------------------------------------------
    def test_e2e_auth_lifecycle(self) -> None:
        admin_id = uuid4()
        role_id = uuid4()
        initial_hash = hash_password("admin")

        mock_payload = LoginRequest(username="admin", password="admin")
        mock_response = MagicMock()
        mock_http_request = MagicMock()
        mock_http_request.url.scheme = "http"
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        user_row = MagicMock()
        user_row.id = admin_id
        user_row.username = "admin"
        user_row.password_hash = initial_hash
        user_row.is_active = True
        user_row.must_change_password = True
        user_row.role_name = "ADMIN"

        db_res = MagicMock()
        db_res.fetchone.return_value = user_row
        db_res.scalar_one_or_none.return_value = user_row
        mock_db.execute.return_value = db_res

        # 1.1 Login with default admin credentials
        login_res = self.loop.run_until_complete(
            login(payload=mock_payload, response=mock_response, http_request=mock_http_request, db=mock_db)
        )
        self.assertEqual(login_res.data.username, "admin")
        self.assertTrue(login_res.data.must_change_password)
        self.assertEqual(login_res.data.role, "ADMIN")

        # 1.2 Change password
        current_admin = {
            "sub": str(admin_id),
            "username": "admin",
            "role": "ADMIN",
            "must_change_password": True
        }
        change_payload = ChangePasswordRequest(old_password="admin", new_password="NewSecurePassword123!")
        change_res = self.loop.run_until_complete(
            change_password(request=change_payload, current_user=current_admin, db=mock_db)
        )
        self.assertTrue(change_res.success)
        mock_db.commit.assert_awaited()

    # -------------------------------------------------------------
    # 2. Endpoint IP Validation & Query Filter Security
    # -------------------------------------------------------------
    def test_e2e_endpoint_ip_and_status_validation(self) -> None:
        # 2.1 Valid RFC 1918 and IPv6 formats
        valid_ips = ["192.168.1.1", "10.0.0.1", "172.16.5.20", "127.0.0.1", "2001:db8::1"]
        for ip in valid_ips:
            req = CreateEndpointRequest(ip_address=ip, hostname=f"host-{ip.replace(':', '_')}", device_type="ROUTER")
            self.assertEqual(req.ip_address, ip)

        # 2.2 Rejection of invalid IP formats
        invalid_ips = ["not_an_ip", "999.999.999.999", "192.168.1.500", "http://target.com", ""]
        for bad_ip in invalid_ips:
            with self.assertRaises(ValidationError):
                CreateEndpointRequest(ip_address=bad_ip, hostname="bad-host", device_type="ROUTER")

        # 2.3 Status filter validation in list_endpoints
        mock_db = AsyncMock()
        mock_db.execute.return_value = MagicMock(fetchall=MagicMock(return_value=[]))
        mock_user = {"id": uuid4(), "username": "admin", "role": "ADMIN"}

        # Valid status query
        res = self.loop.run_until_complete(
            list_endpoints(status="ACTIVE", current_user=mock_user, db=mock_db)
        )
        self.assertTrue(res.success)
        self.assertEqual(res.meta.total, 0)
        self.assertEqual(res.meta.page_size, 1)

        # Invalid status query
        with self.assertRaises(HTTPException) as ctx:
            self.loop.run_until_complete(
                list_endpoints(status="MALICIOUS_STATUS", current_user=mock_user, db=mock_db)
            )
        self.assertEqual(ctx.exception.status_code, 400)

    # -------------------------------------------------------------
    # 3. Topology Graph DAG Rebuild & Mutex Safety
    # -------------------------------------------------------------
    def test_e2e_topology_mutex_and_state_transitions(self) -> None:
        manager = TopologyGraphManager.get_instance()
        ep_id = uuid4()
        ep_str = str(ep_id)

        # Initialize node
        manager._nodes[ep_str] = {"id": ep_str, "state": "UP", "status": "UP", "type": "monitored"}
        
        # Async node status mutation with mutex
        self.loop.run_until_complete(manager.update_node_status(ep_str, "DOWN"))
        cached = manager.get_cached_graph()
        target_node = next((n for n in cached["nodes"] if n["id"] == ep_str), None)
        self.assertIsNotNone(target_node)
        self.assertEqual(target_node["state"], "DOWN")

        # Async path update with mutex
        self.loop.run_until_complete(
            manager.update_endpoint_path(ep_id, [{"hop": 1, "ip": "10.10.10.1"}, {"hop": 2, "ip": "10.10.10.2"}])
        )
        cached_updated = manager.get_cached_graph()
        transit_nodes = {n["id"] for n in cached_updated["nodes"]}
        self.assertIn("transit:10.10.10.1", transit_nodes)
        self.assertIn("transit:10.10.10.2", transit_nodes)

    # -------------------------------------------------------------
    # 4. Background Task Lifetime & Error Callback Handler
    # -------------------------------------------------------------
    def test_e2e_safe_create_task_error_logging(self) -> None:
        async def run_test():
            async def failing_task():
                raise RuntimeError("Diagnostic trace simulation failed")

            task = safe_create_task(failing_task(), "smoke_test_failure")
            self.assertIn(task, _active_background_tasks)
            await asyncio.sleep(0.05)
            self.assertNotIn(task, _active_background_tasks)

        self.loop.run_until_complete(run_test())

    # -------------------------------------------------------------
    # 5. Smart Conditional Database Commit Mechanics
    # -------------------------------------------------------------
    def test_e2e_smart_conditional_commit(self) -> None:
        async def run_get_db_flow(explicit_commit: bool):
            mock_session = AsyncMock()
            mock_session.is_active = True
            mock_session.in_transaction = MagicMock(return_value=True)

            mock_session_local = MagicMock()
            mock_session_local.return_value.__aenter__.return_value = mock_session
            mock_session_local.return_value.__aexit__.return_value = AsyncMock()()

            with patch("app.database.AsyncSessionLocal", mock_session_local):
                gen = get_db()
                db = await anext(gen)
                if explicit_commit:
                    await db.commit()
                    mock_session.in_transaction.return_value = False
                try:
                    await anext(gen)
                except StopAsyncIteration:
                    pass
            return mock_session

        # Case 1: Route explicitly called commit -> get_db avoids double-commit
        sess1 = self.loop.run_until_complete(run_get_db_flow(explicit_commit=True))
        self.assertEqual(sess1.commit.await_count, 1)

        # Case 2: Route did NOT explicitly call commit -> get_db commits automatically
        sess2 = self.loop.run_until_complete(run_get_db_flow(explicit_commit=False))
        self.assertEqual(sess2.commit.await_count, 1)

    # -------------------------------------------------------------
    # 6. Report Date Range & Sub-second Validation
    # -------------------------------------------------------------
    def test_e2e_report_date_range_validation(self) -> None:
        now = datetime.now(timezone.utc)
        valid_start = now - timedelta(days=30)
        valid_end = now

        # Valid date range should not raise
        _validate_date_range(valid_start, valid_end)

        # Invalid date range (start > end)
        with self.assertRaises(HTTPException) as ctx1:
            _validate_date_range(valid_end, valid_start)
        self.assertEqual(ctx1.exception.status_code, 400)

        # Exceeds max allowable span (730 days)
        with self.assertRaises(HTTPException) as ctx2:
            _validate_date_range(now - timedelta(days=731), now)
        self.assertEqual(ctx2.exception.status_code, 400)
