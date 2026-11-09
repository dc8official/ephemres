import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.asgi import HTTPException
from app.routers.auth import get_current_user
from app.routers.reports import sanitize_csv_field


class TestSecurityAuditFixes(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_inactive_user_token_rejection(self) -> None:
        mock_request = MagicMock()
        mock_request.cookies.get.return_value = "valid_token"

        mock_db = AsyncMock()

        u_id = str(uuid4())
        test_jti = "test_jti_active"
        # Mock payload decoded cleanly
        with patch("app.routers.auth.decode_access_token") as mock_decode, \
             patch("app.routers.auth.driver_manager.get_session_store") as mock_get_store:
            mock_decode.return_value = {"sub": u_id, "username": "disabled_user", "role": "USER", "jti": test_jti}
            mock_store = AsyncMock()
            mock_store.is_session_active.return_value = True
            mock_get_store.return_value = mock_store

            # Case 1: Inactive user in DB
            db_res = MagicMock()
            db_res.scalar_one_or_none.return_value = MagicMock(is_active=False, must_change_password=False)
            db_res.fetchone.return_value = MagicMock(is_active=False, must_change_password=False)
            mock_db.execute.return_value = db_res

            with self.assertRaises(HTTPException) as cm:
                self.loop.run_until_complete(get_current_user(mock_request, db=mock_db))

            self.assertEqual(cm.exception.status_code, 401)
            self.assertIn("inactive", cm.exception.detail)


    def test_csv_field_formula_injection_escaping(self) -> None:
        self.assertEqual(sanitize_csv_field("=CMD|' /C calc'!A0"), "'=CMD|' /C calc'!A0")
        self.assertEqual(sanitize_csv_field("+12345"), "'+12345")
        self.assertEqual(sanitize_csv_field("-999"), "'-999")
        self.assertEqual(sanitize_csv_field("@SUM(1,2)"), "'@SUM(1,2)")
        self.assertEqual(sanitize_csv_field("NORMAL_DATA"), "NORMAL_DATA")

    @patch("asyncio.create_subprocess_exec")
    def test_ping_command_option_flag_injection_prevention(self, mock_exec: AsyncMock) -> None:
        from monitoring.ping import run_system_ping_fallback
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"1 packets transmitted, 1 received", b"")
        mock_exec.return_value = mock_proc

        target = "-h"
        self.loop.run_until_complete(run_system_ping_fallback(target, 1, 0.2, 1.0))

        # Ensure '--' separator is passed before target IP to prevent option injection
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        self.assertIn("--", args)
        self.assertEqual(args[args.index("--") + 1], "-h")

    def test_get_current_user_executes_uuid_cast_query(self) -> None:
        mock_request = MagicMock()
        mock_request.cookies.get.return_value = "valid_token"

        mock_db = AsyncMock()
        test_user_id = str(uuid4())

        with patch("app.routers.auth.decode_access_token") as mock_decode, \
             patch("app.routers.auth.driver_manager.get_session_store") as mock_get_store:
            mock_decode.return_value = {"sub": test_user_id, "username": "active_user", "role": "ADMIN", "jti": "jti_123"}
            mock_store = AsyncMock()
            mock_store.is_session_active.return_value = True
            mock_get_store.return_value = mock_store

            db_res = MagicMock()
            db_res.scalar_one_or_none.return_value = MagicMock(is_active=True, must_change_password=False)
            db_res.fetchone.return_value = MagicMock(is_active=True, must_change_password=False)
            mock_db.execute.return_value = db_res

            res = self.loop.run_until_complete(get_current_user(mock_request, db=mock_db))

            self.assertEqual(res["username"], "active_user")
            mock_db.execute.assert_called_once()
            called_sql = str(mock_db.execute.call_args[0][0])
            self.assertTrue("users.id =" in called_sql or "users.id = :" in called_sql or "WHERE id = CAST(:user_id AS uuid)" in called_sql)

    def test_concurrent_session_fifo_rotation(self) -> None:
        from app.services.auth_service import register_session_async, is_session_active, invalidate_session_async
        from app.services.driver_manager import driver_manager
        from tests.backend.session_store_double import InMemorySessionStore

        async def _run():
            user_id = str(uuid4())

            # Session 1 & Session 2 registered
            await register_session_async(user_id, "jti_session_1", max_sessions=2)
            await register_session_async(user_id, "jti_session_2", max_sessions=2)

            self.assertTrue(await is_session_active(user_id, "jti_session_1"))
            self.assertTrue(await is_session_active(user_id, "jti_session_2"))

            # Session 3 registered -> FIFO evicts Session 1
            await register_session_async(user_id, "jti_session_3", max_sessions=2)
            self.assertFalse(await is_session_active(user_id, "jti_session_1"))
            self.assertTrue(await is_session_active(user_id, "jti_session_2"))
            self.assertTrue(await is_session_active(user_id, "jti_session_3"))

            # Logout session 2
            await invalidate_session_async(user_id, "jti_session_2")
            self.assertFalse(await is_session_active(user_id, "jti_session_2"))
            self.assertTrue(await is_session_active(user_id, "jti_session_3"))

        with patch.object(driver_manager, "get_session_store", return_value=InMemorySessionStore()):
            self.loop.run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
