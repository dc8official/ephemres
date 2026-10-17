import pytest
from app.core.asgi import TestClient
from app.main import app


def test_sse_endpoint_requires_authentication():
    """Verify BUG-03 / SEC-01: /api/v1/events/stream requires valid authentication."""
    client = TestClient(app)

    response = client.get("/api/v1/events/stream")
    assert response.status_code == 401
    assert "Not authenticated" in response.text or "Unauthorized" in response.text or "detail" in response.json()
