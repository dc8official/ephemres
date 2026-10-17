from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.core.asgi import HTTPException

from app.routers.auth import get_current_user_sse
from app.routers.events import stream_events
from app.routers.reports import batch_export_telemetry


class TestSSEAuthPoolDecoupling(unittest.TestCase):
    """
    Test suite verifying that Server-Sent Events (SSE) and streaming endpoints
    decouple database connection pool acquisition from long-lived responses.
    """

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_get_current_user_sse_success_closes_session(self) -> None:
        """
        Verify get_current_user_sse uses a short-lived AsyncSessionLocal context manager
        and returns the user payload without keeping a DB connection open.
        """
        async def _run():
            mock_request = MagicMock()
            mock_request.cookies = {"ephemres_access_token": "valid.token.test"}
            mock_request.headers = {}
            mock_request.url.path = "/api/v1/events/stream"

            mock_session = AsyncMock()
            mock_session.__aenter__.return_value = mock_session
            mock_session.__aexit__.return_value = None

            expected_payload = {
                "sub": str(uuid4()),
                "jti": str(uuid4()),
                "username": "admin",
                "role": "ADMIN",
            }

            with patch("app.routers.auth.AsyncSessionLocal", return_value=mock_session), \
                 patch("app.routers.auth.get_current_user", new=AsyncMock(return_value=expected_payload)) as mock_gcu:
                result = await get_current_user_sse(mock_request)
                self.assertEqual(result, expected_payload)
                mock_gcu.assert_awaited_once_with(request=mock_request, db=mock_session)
                mock_session.__aenter__.assert_awaited_once()
                mock_session.__aexit__.assert_awaited_once()

        self.loop.run_until_complete(_run())

    def test_get_current_user_sse_propagates_unauthenticated(self) -> None:
        """
        Verify get_current_user_sse propagates HTTPException(401) on missing/invalid auth
        while properly releasing the session.
        """
        async def _run():
            mock_request = MagicMock()
            mock_request.cookies = {}
            mock_request.headers = {}
            mock_request.url.path = "/api/v1/events/stream"

            mock_session = AsyncMock()
            mock_session.__aenter__.return_value = mock_session
            mock_session.__aexit__.return_value = None

            with patch("app.routers.auth.AsyncSessionLocal", return_value=mock_session), \
                 patch("app.routers.auth.get_current_user", side_effect=HTTPException(status_code=401, detail="Not authenticated.")):
                with self.assertRaises(HTTPException) as ctx:
                    await get_current_user_sse(mock_request)
                self.assertEqual(ctx.exception.status_code, 401)
                mock_session.__aenter__.assert_awaited_once()
                mock_session.__aexit__.assert_awaited_once()

        self.loop.run_until_complete(_run())

    def test_stream_events_route_uses_get_current_user_sse(self) -> None:
        """
        Verify that stream_events depends on get_current_user_sse rather than standard get_current_user.
        """
        import inspect
        from app.core.asgi import Depends

        sig = inspect.signature(stream_events)
        param = sig.parameters.get("current_user")
        self.assertIsNotNone(param, "stream_events must have current_user parameter")
        self.assertIsInstance(param.default, Depends, "current_user must be a FastAPI Depends")
        self.assertEqual(
            param.default.dependency,
            get_current_user_sse,
            "stream_events must use get_current_user_sse dependency to prevent pool starvation"
        )

    def test_batch_export_telemetry_uses_get_current_user_sse(self) -> None:
        """
        Verify that batch_export_telemetry depends on get_current_user_sse to avoid pool lock during CSV stream.
        """
        import inspect
        from app.core.asgi import Depends

        sig = inspect.signature(batch_export_telemetry)
        param = sig.parameters.get("current_user")
        self.assertIsNotNone(param, "batch_export_telemetry must have current_user parameter")
        self.assertIsInstance(param.default, Depends, "current_user must be a FastAPI Depends")
        self.assertEqual(
            param.default.dependency,
            get_current_user_sse,
            "batch_export_telemetry must use get_current_user_sse dependency"
        )
