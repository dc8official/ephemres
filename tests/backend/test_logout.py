"""Signing out ends the session and clears the cookie (M3 coverage review, 2026-11-10).

Before these tests nothing ran routers/auth.logout or Response.delete_cookie. The route has two
jobs, and a pen test checks both: the token must stop working on the server (not only vanish
from the browser), and the cookie must be overwritten with the same attributes it was set with,
or the browser keeps the old one.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.config import settings
from app.core.asgi import Depends, EphemresApp, Request, Response, TestClient
from app.database import get_db
from app.routers.auth import LoginRequest, cookie_name, get_current_user, login, logout
from app.services.auth_service import create_access_token, hash_password, register_session_async
from app.services.driver_manager import driver_manager
from tests.backend.session_store_double import InMemorySessionStore

PASSWORD = "TestSecret123!"


@pytest.fixture(autouse=True)
def session_store():
    store = InMemorySessionStore()
    driver_manager._session_store = store
    yield store


def _app() -> EphemresApp:
    """The real login, logout and auth dependency, with the database replaced by a mock that
    always finds the same user."""
    app = EphemresApp()
    user = MagicMock(
        id=uuid4(), username="alice", password_hash=hash_password(PASSWORD),
        is_active=True, must_change_password=False, role_name="OPERATOR",
    )
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user
    result.fetchone.return_value = user
    db.execute.return_value = result

    async def get_test_db():
        yield db

    @app.post("/auth/login")
    async def route_login(payload: LoginRequest, response: Response, request: Request, db=Depends(get_test_db)):
        return await login(payload=payload, response=response, http_request=request, db=db)

    app.post("/auth/logout")(logout)

    @app.get("/me")
    async def me(current_user: dict = Depends(get_current_user)):
        return {"user": current_user["username"]}

    app.dependency_overrides[get_db] = get_test_db
    return app


def _cleared_cookie(resp) -> str:
    """The Set-Cookie header that clears the session cookie (there must be exactly one)."""
    headers = [h for h in resp.headers.getlist("set-cookie") if h.startswith(f"{cookie_name}=")]
    assert len(headers) == 1, headers
    return headers[0]


def test_logout_revokes_the_token_on_the_server():
    client = TestClient(_app())
    assert client.post("/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 200
    token = client.cookies[cookie_name]
    assert client.get("/me").status_code == 200

    assert client.post("/auth/logout").status_code == 200

    # Replaying the old token, as an attacker who copied it would, must fail.
    replay = TestClient(client.app, cookies={cookie_name: token})
    assert replay.get("/me").status_code == 401


def test_logout_clears_the_cookie_with_the_attributes_it_was_set_with():
    client = TestClient(_app())
    client.post("/auth/login", json={"username": "alice", "password": PASSWORD})
    header = _cleared_cookie(client.post("/auth/logout"))

    # Empty value, expired now and in the past (Max-Age for current browsers, Expires for old
    # ones), and the same Path, HttpOnly and SameSite as at login so it replaces that cookie.
    assert header.startswith(f"{cookie_name}=;")
    for part in ("Path=/", "Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", "HttpOnly", "SameSite=Lax"):
        assert part in header
    assert client.cookies.get(cookie_name, "") == ""


def test_logout_over_https_marks_the_cleared_cookie_secure():
    client = TestClient(_app(), base_url="https://testserver")
    assert "Secure" in _cleared_cookie(client.post("/auth/logout"))


def test_logout_over_http_with_hsts_marks_the_cleared_cookie_secure(monkeypatch):
    # Behind nginx the request reaches the API as http even when the browser used HTTPS.
    monkeypatch.setattr(settings.security, "hsts_enabled", True)
    client = TestClient(_app())
    assert "Secure" in _cleared_cookie(client.post("/auth/logout"))


@pytest.mark.anyio
async def test_logout_with_a_bearer_header_revokes_that_session(session_store):
    user_id = str(uuid4())
    await register_session_async(user_id, "jti-1")
    token = create_access_token(user_id, "bob", "VIEWER", jti="jti-1")

    resp = TestClient(_app()).post("/auth/logout", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert not await session_store.is_session_active(user_id, "jti-1")


@pytest.mark.parametrize("cookies", [{}, {cookie_name: "not-a-jwt"}, {cookie_name: "a.b.c"}])
def test_logout_always_succeeds_and_clears_the_cookie(cookies):
    # A browser holding an expired or garbage token must still be able to sign out.
    resp = TestClient(_app(), cookies=cookies).post("/auth/logout")
    assert resp.status_code == 200
    assert "Max-Age=0" in _cleared_cookie(resp)
