import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.asgi import HTTPException
from app.routers.auth import login, change_password, get_current_user, LoginRequest, ChangePasswordRequest
from app.routers.users import create_user, reset_password, update_user, delete_user, CreateUserRequest, ResetPasswordRequest, UpdateUserRequest
from app.services.auth_service import hash_password, verify_password, clear_failed_attempts, _failed_attempts


class TestFullAuthUserFlow(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        _failed_attempts.clear()

    def tearDown(self) -> None:
        _failed_attempts.clear()
        pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def test_login_success_path(self) -> None:
        admin_id = uuid4()
        role_id = uuid4()
        hashed_pass = hash_password("admin")

        mock_payload = LoginRequest(username="admin", password="admin")
        mock_response = MagicMock()
        mock_http_request = MagicMock()
        mock_http_request.url.scheme = "http"
        mock_db = AsyncSessionLocal = AsyncMock()
        mock_db.add = MagicMock()

        # Mock SELECT user
        user_row = MagicMock()
        user_row.id = admin_id
        user_row.username = "admin"
        user_row.password_hash = hashed_pass
        user_row.is_active = True
        user_row.must_change_password = True
        user_row.role_name = "ADMIN"

        db_res = MagicMock()
        db_res.fetchone.return_value = user_row
        db_res.scalar_one_or_none.return_value = user_row
        mock_db.execute.return_value = db_res

        res = self.loop.run_until_complete(
            login(payload=mock_payload, response=mock_response, http_request=mock_http_request, db=mock_db)
        )

        self.assertEqual(res.data.username, "admin")
        self.assertEqual(res.data.role, "ADMIN")
        self.assertTrue(res.data.must_change_password)
        mock_response.set_cookie.assert_called_once()

    def test_login_invalid_password_returns_401(self) -> None:
        hashed_pass = hash_password("admin")
        mock_payload = LoginRequest(username="admin", password="wrongpassword")
        mock_response = MagicMock()
        mock_http_request = MagicMock()
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        user_row = MagicMock()
        user_row.username = "admin"
        user_row.password_hash = hashed_pass
        user_row.is_active = True

        db_res = MagicMock()
        db_res.fetchone.return_value = user_row
        db_res.scalar_one_or_none.return_value = user_row
        mock_db.execute.return_value = db_res

        with self.assertRaises(HTTPException) as cm:
            self.loop.run_until_complete(
                login(payload=mock_payload, response=mock_response, http_request=mock_http_request, db=mock_db)
            )

        self.assertEqual(cm.exception.status_code, 401)

    def test_change_password_initial_forced_reset(self) -> None:
        user_id = uuid4()
        current_user = {"sub": str(user_id), "username": "admin", "role": "ADMIN"}
        mock_req = ChangePasswordRequest(new_password="NewSecurePassword123!")
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        user_row = MagicMock()
        user_row.id = user_id
        user_row.password_hash = hash_password("admin")
        user_row.must_change_password = True

        db_res = MagicMock()
        db_res.fetchone.return_value = user_row
        db_res.scalar_one_or_none.return_value = user_row
        mock_db.execute.return_value = db_res

        res = self.loop.run_until_complete(
            change_password(request=mock_req, current_user=current_user, db=mock_db)
        )

        self.assertEqual(res.data["message"], "Password changed successfully.")

    def test_create_user_admin_only(self) -> None:
        mock_req = CreateUserRequest(username="operator1", password=None, role="VIEWER")
        current_user = {"sub": str(uuid4()), "role": "ADMIN"}
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        # Mock no dup user
        dup_res = MagicMock()
        dup_res.fetchone.return_value = None
        dup_res.scalar_one_or_none.return_value = None

        # Mock role search
        role_row = MagicMock(id=uuid4())
        role_res = MagicMock()
        role_res.fetchone.return_value = role_row
        role_res.scalar_one_or_none.return_value = role_row

        # Mock insert RETURNING id
        new_user_row = MagicMock(id=uuid4())
        insert_res = MagicMock()
        insert_res.fetchone.return_value = new_user_row
        insert_res.scalar_one_or_none.return_value = new_user_row

        mock_db.execute.side_effect = [dup_res, role_res, insert_res, MagicMock()]

        res = self.loop.run_until_complete(
            create_user(request=mock_req, current_user=current_user, db=mock_db)
        )

        self.assertEqual(res.data["username"], "operator1")
        self.assertEqual(res.data["role"], "VIEWER")
        self.assertIn("generated_password", res.data)

    def test_admin_cannot_self_reset(self) -> None:
        admin_id = uuid4()
        mock_req = ResetPasswordRequest(password="newpassword123")
        current_user = {"sub": str(admin_id), "role": "ADMIN"}
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        with self.assertRaises(HTTPException) as cm:
            self.loop.run_until_complete(
                reset_password(user_id=admin_id, request=mock_req, current_user=current_user, db=mock_db)
            )

        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("own password", cm.exception.detail)

    def test_admin_cannot_deactivate_self(self) -> None:
        admin_id = uuid4()
        current_user = {"sub": str(admin_id), "role": "ADMIN"}
        mock_db = AsyncMock()
        mock_db.add = MagicMock()

        with self.assertRaises(HTTPException) as cm:
            self.loop.run_until_complete(
                delete_user(user_id=admin_id, current_user=current_user, db=mock_db)
            )

        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("deactivate their own", cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
