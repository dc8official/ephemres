import pytest
from app.core.asgi import TestClient
from app.main import app


def test_sse_endpoint_requires_authentication():
    """Verify BUG-03 / SEC-01: /api/v1/events/stream requires valid authentication."""
    client = TestClient(app)

    response = client.get("/api/v1/events/stream")
    assert response.status_code == 401
    assert "Not authenticated" in response.text or "Unauthorized" in response.text or "detail" in response.json()


def test_sse_endpoint_streaming_headers_and_greeting():
    """Verify that authenticated SSE streams without content-length header and emits CONNECTED envelope."""
    from unittest.mock import AsyncMock
    from app.routers.auth import get_current_user_sse

    mock_user = {
        "sub": "00000000-0000-0000-0000-000000000001",
        "jti": "mock-jti",
        "username": "admin",
        "role": "ADMIN",
    }

    app.dependency_overrides[get_current_user_sse] = AsyncMock(return_value=mock_user)
    try:
        client = TestClient(app)
        response = client.get("/api/v1/events/stream")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        assert "content-length" not in response.headers, "SSE response must not contain content-length"
        assert "CONNECTED" in response.text
        assert "Ephemres v3.3.5 SSE stream established" in response.text
    finally:
        app.dependency_overrides.pop(get_current_user_sse, None)
