from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.services.auth_service import (
    register_session,
    is_session_active,
    create_access_token,
    _active_user_sessions,
)
from app.core.http_client import validate_outbound_url
from app.database import check_database_connection


@pytest.mark.anyio
async def test_session_eviction_deduplication():
    """Verify that multiple registrations of the same jti do not evict prior sessions."""
    user_id = str(uuid4())
    _active_user_sessions.pop(user_id, None)

    # Session 1
    register_session(user_id, "token-1", max_sessions=2)
    assert _active_user_sessions[user_id] == ["token-1"]

    # Re-registering session 1 must be a no-op (no duplicate, no eviction)
    register_session(user_id, "token-1", max_sessions=2)
    assert _active_user_sessions[user_id] == ["token-1"]

    # Session 2
    register_session(user_id, "token-2", max_sessions=2)
    assert _active_user_sessions[user_id] == ["token-1", "token-2"]

    # Re-registering session 2 must not push out session 1
    register_session(user_id, "token-2", max_sessions=2)
    assert _active_user_sessions[user_id] == ["token-1", "token-2"]
    assert await is_session_active(user_id, "token-1") is True
    assert await is_session_active(user_id, "token-2") is True

    # Session 3 should evict oldest session 1 (FIFO)
    register_session(user_id, "token-3", max_sessions=2)
    assert _active_user_sessions[user_id] == ["token-2", "token-3"]
    assert await is_session_active(user_id, "token-1") is False
    assert await is_session_active(user_id, "token-2") is True
    assert await is_session_active(user_id, "token-3") is True


def test_create_access_token_is_stateless():
    """Verify that create_access_token does not modify _active_user_sessions."""
    user_id = str(uuid4())
    _active_user_sessions.pop(user_id, None)

    token = create_access_token(user_id, "testuser", "ADMIN", jti="stateless-jti")
    assert isinstance(token, str)
    assert user_id not in _active_user_sessions


def test_smtp_private_ip_allowed():
    """Verify private RFC 1918 and loopback IPs are allowed for SMTP."""
    # Loopback allowed with allow_loopback=True (used by SMTP)
    validate_outbound_url("http://127.0.0.1", allow_private=True, allow_loopback=True)
    validate_outbound_url("http://localhost", allow_private=True, allow_loopback=True)

    # Private RFC 1918 allowed with allow_private=True
    validate_outbound_url("http://192.168.1.50", allow_private=True)
    validate_outbound_url("http://10.0.0.1", allow_private=True)
    validate_outbound_url("http://172.16.0.5", allow_private=True)

    # Cloud metadata MUST still be blocked even with allow_private=True and allow_loopback=True
    with pytest.raises(ValueError, match="SSRF violation: Access to cloud metadata IP"):
        validate_outbound_url("http://169.254.169.254", allow_private=True, allow_loopback=True)


@pytest.mark.anyio
async def test_database_connection_retries_and_succeeds():
    """Verify that check_database_connection retries on transient error and succeeds."""
    mock_conn = AsyncMock()
    attempts = 0

    class MockContextManager:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise ConnectionRefusedError("PostgreSQL starting up...")
            return mock_conn

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

    mock_engine = MagicMock()
    mock_engine.connect.return_value = MockContextManager()

    with patch("app.database.async_engine", mock_engine):
        # Retries with 0.01s delay for fast test
        await check_database_connection(max_retries=4, retry_delay=0.01)
        assert attempts == 3
