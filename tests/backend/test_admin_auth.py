import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.core.asgi import HTTPException
from app.services.auth_service import (
    hash_password,
    verify_password,
    clear_failed_attempts,
    record_failed_attempt,
    is_account_locked,
    _failed_attempts,
)
from app.seed_admin import seed_admin


class TestAdminAuthSeeding(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        _failed_attempts.clear()

    def tearDown(self) -> None:
        _failed_attempts.clear()
        self.loop.close()

    def test_password_hash_and_verification(self) -> None:
        hashed = hash_password("admin")
        self.assertTrue(verify_password("admin", hashed))
        self.assertFalse(verify_password("wrongpassword", hashed))

    def test_account_lockout_and_clear(self) -> None:
        username = "admin"
        for _ in range(5):
            record_failed_attempt(username)
        self.assertTrue(is_account_locked(username))

        clear_failed_attempts(username)
        self.assertFalse(is_account_locked(username))

    def test_login_schema_parsing(self) -> None:
        from app.routers.auth import LoginRequest
        req = LoginRequest(username="admin", password="admin")
        self.assertEqual(req.username, "admin")
        self.assertEqual(req.password, "admin")


if __name__ == "__main__":
    unittest.main()
