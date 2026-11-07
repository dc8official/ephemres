"""Regression tests for the 2026-11 authentication hotfix.

Each test pins one finding that was reproduced against
a two-worker stack:

* C1  the forced password change was enforced only by the browser: a temporary or default
      password worked for every API call.
* H1  require_admin trusted the role inside the token, so a demoted admin kept admin rights
      until the token expired.
* H2  a worker accepted a session its own memory remembered even after the shared store had
      revoked it (logout, password change or reset handled by another worker).
* H3  unknown usernames answered without a scrypt check, which revealed valid usernames.
* H4  production fell back to the public development key when the env file was missing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.asgi import HTTPException
from app.services import auth_service
from app.services.driver_manager import driver_manager
from tests.backend.session_store_double import InMemorySessionStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _request(token="tok"):
    req = MagicMock()
    req.cookies.get.side_effect = lambda name: token if name == "ephemres_access_token" else None
    req.client.host = "127.0.0.1"
    req.headers.get.return_value = None
    return req


def _db_with_user(user):
    db = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = user
    res.fetchone.return_value = user
    db.execute.return_value = res
    return db


def _user(role="ADMIN", must_change=False, active=True):
    return SimpleNamespace(
        id=uuid4(), is_active=active, must_change_password=must_change,
        role=SimpleNamespace(role_name=role), password_hash="x",
    )


def _authenticate(user, token_role="ADMIN", gate=True):
    from app.routers import auth

    store = InMemorySessionStore()
    sub, jti = str(uuid4()), "jti-1"
    _run(store.register_session(sub, jti))
    payload = {"sub": sub, "jti": jti, "username": "u", "role": token_role}
    dep = auth.get_current_user if gate else auth.get_current_user_allow_password_change
    with patch.object(auth, "decode_access_token", return_value=dict(payload)), \
         patch.object(driver_manager, "get_session_store", return_value=store):
        return _run(dep(_request(), db=_db_with_user(user)))


# --- C1: forced password change is enforced by the API -------------------------------------

def test_account_that_must_change_password_is_refused_by_every_route():
    with pytest.raises(HTTPException) as exc:
        _authenticate(_user(must_change=True))
    assert exc.value.status_code == 403
    assert "Password change required" in exc.value.detail


def test_change_password_route_still_accepts_that_account():
    payload = _authenticate(_user(must_change=True), gate=False)
    assert payload["must_change_password"] is True


def test_admin_routes_inherit_the_gate():
    """require_admin and the SSE dependency both build on get_current_user."""
    import inspect
    from app.routers import auth

    assert inspect.signature(auth.require_admin).parameters["current_user"].default.dependency is auth.get_current_user
    assert "get_current_user(" in inspect.getsource(auth.get_current_user_sse)


def test_change_password_requires_the_current_password_even_when_forced():
    from app.routers.auth import ChangePasswordRequest, change_password

    user = _user(must_change=True)
    user.password_hash = auth_service.hash_password("Temp-Password-1")
    db = _db_with_user(user)
    current = {"sub": str(user.id), "username": "admin", "jti": "j"}

    with pytest.raises(HTTPException) as exc:
        _run(change_password(ChangePasswordRequest(new_password="NewPassword123!"), current_user=current, db=db))
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        _run(change_password(
            ChangePasswordRequest(old_password="wrong", new_password="NewPassword123!"), current_user=current, db=db))
    assert exc.value.status_code == 400


# --- H1: role comes from the database ------------------------------------------------------

def test_demoted_admin_token_no_longer_grants_admin():
    from app.routers.auth import require_admin

    payload = _authenticate(_user(role="VIEWER"), token_role="ADMIN")
    assert payload["role"] == "VIEWER"
    with pytest.raises(HTTPException) as exc:
        _run(require_admin(current_user=payload))
    assert exc.value.status_code == 403


def test_promoted_viewer_gets_admin_without_signing_in_again():
    assert _authenticate(_user(role="ADMIN"), token_role="VIEWER")["role"] == "ADMIN"


def test_role_change_signs_the_account_out():
    from app.routers.users import UpdateUserRequest, update_user

    target = _user(role="ADMIN")
    target.role_id = uuid4()
    target.username = "op"
    viewer_role = SimpleNamespace(id=uuid4())
    with patch("app.routers.users.AuthRepository") as repo_cls, \
         patch("app.routers.users.invalidate_all_user_sessions_async", new=AsyncMock()) as revoke:
        repo = repo_cls.return_value
        repo.get_user_by_id = AsyncMock(return_value=target)
        repo.get_role_by_name = AsyncMock(return_value=viewer_role)
        repo.update_user = AsyncMock()
        repo.create_audit_log = AsyncMock()
        _run(update_user(target.id, UpdateUserRequest(role="VIEWER"),
                         current_user={"sub": str(uuid4()), "role": "ADMIN"}, db=AsyncMock()))
    revoke.assert_awaited_once_with(str(target.id))


# --- H2: the shared store is the only session authority ------------------------------------

def test_session_revoked_in_the_store_is_rejected_even_if_this_worker_remembers_it():
    user_id = str(uuid4())
    auth_service._active_user_sessions[user_id] = ["jti-x"]
    try:
        with patch.object(driver_manager, "get_session_store", return_value=InMemorySessionStore()):
            assert _run(auth_service.is_session_active(user_id, "jti-x")) is False
    finally:
        auth_service._active_user_sessions.pop(user_id, None)


def test_session_store_failure_fails_closed():
    store = AsyncMock()
    store.is_session_active.side_effect = RuntimeError("store down")
    user_id = str(uuid4())
    auth_service._active_user_sessions[user_id] = ["jti-x"]
    try:
        with patch.object(driver_manager, "get_session_store", return_value=store):
            assert _run(auth_service.is_session_active(user_id, "jti-x")) is False
    finally:
        auth_service._active_user_sessions.pop(user_id, None)


# --- H3: lockout in the shared store, no username timing oracle ----------------------------

def _login(username, password, store, user=None):
    from app.routers.auth import LoginRequest, login

    http = MagicMock()
    http.client.host = "198.51.100.9"
    http.headers.get.return_value = None
    http.url.scheme = "https"
    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch("app.routers.auth.AuthRepository") as repo_cls:
        repo = repo_cls.return_value
        repo.get_user_by_username = AsyncMock(return_value=user)
        repo.update_last_login = AsyncMock()
        repo.create_audit_log = AsyncMock()
        return _run(login(LoginRequest(username=username, password=password), MagicMock(), http, db=AsyncMock()))


def test_unknown_username_spends_a_scrypt_check():
    with patch("app.routers.auth.burn_password_check_async", new=AsyncMock()) as burn:
        with pytest.raises(HTTPException) as exc:
            _login("ghost", "guess", InMemorySessionStore())
    assert exc.value.status_code == 401
    burn.assert_awaited_once_with("guess")


def test_lockout_is_read_from_the_shared_store_only():
    """A lock recorded by another worker applies here, whatever this worker's memory says."""
    store = InMemorySessionStore(lockout_threshold=3)
    auth_service._failed_attempts.clear()
    for _ in range(3):
        _run(store.record_failed_attempt("198.51.100.9", "admin"))
    with pytest.raises(HTTPException) as exc:
        _login("admin", "admin", store, user=_user())
    assert exc.value.status_code == 403


def test_broken_lockout_store_refuses_sign_in():
    store = AsyncMock()
    store.is_account_locked.side_effect = RuntimeError("down")
    with pytest.raises(HTTPException) as exc:
        _login("admin", "admin", store, user=_user())
    assert exc.value.status_code == 503


def test_dummy_check_costs_about_as_much_as_a_real_one():
    import time

    real_hash = auth_service.hash_password("Real-Password-1")
    _run(auth_service.burn_password_check_async("warm-up"))
    t0 = time.perf_counter()
    _run(auth_service.burn_password_check_async("guess"))
    dummy = time.perf_counter() - t0
    t0 = time.perf_counter()
    _run(auth_service.verify_password_async("guess", real_hash))
    real = time.perf_counter() - t0
    assert dummy > real * 0.5


# --- H4: production refuses the development key and short keys -----------------------------

def test_config_under_etc_ephemres_defaults_to_production():
    from app.config import default_environment_for

    assert default_environment_for(Path("/etc/ephemres/config.toml")) == "production"
    assert default_environment_for(Path("/etc/netmon/config.toml")) == "production"
    assert default_environment_for(Path("/home/dev/ephemres/deploy/config.template.toml")) == "development"
    assert default_environment_for(None) == "development"


@pytest.mark.parametrize("key", ["dev-secret-key-change-in-production-min-32-chars", "short-key"])
def test_production_refuses_default_or_short_keys(tmp_path, key):
    with pytest.raises(ValueError, match="Insecure configuration"):
        _validate_with(tmp_path, {"EPHEMRES_ENV": "production", "EPHEMRES_SECRET_KEY": key})


def _validate_with(tmp_path, env):
    from app.config import load_settings

    with patch.dict("os.environ", env, clear=False):
        return load_settings()


def test_production_accepts_a_generated_key(tmp_path):
    import secrets

    s = _validate_with(tmp_path, {"EPHEMRES_ENV": "production", "EPHEMRES_SECRET_KEY": secrets.token_hex(32)})
    assert s.environment == "production"


def test_units_require_the_env_file():
    root = Path(__file__).resolve().parents[2] / "deploy"
    for unit in ("ephemres-api.service", "ephemres-engine.service", "ephemres-flowd.service"):
        text = (root / unit).read_text()
        assert "EnvironmentFile=/etc/ephemres/ephemres.env\n" in text, unit


def test_installer_no_longer_defaults_to_admin_admin():
    install = (Path(__file__).resolve().parents[2] / "deploy" / "install.sh").read_text()
    assert 'ADMIN_PASS="admin"' not in install
    assert "DEFAULT_ADMIN_PASSWORD=$ADMIN_PASS\nEOF" not in install


def test_login_returns_its_connection_before_the_lockout_write():
    """A sign-in flood deadlocked the pool: every request held its lookup connection through
    scrypt while waiting for a second one to record the failure (seen at 32 concurrent)."""
    from app.routers.auth import LoginRequest, login

    events = []
    store = InMemorySessionStore()
    real_record = store.record_failed_attempt

    async def record(*a, **kw):
        events.append("record_failure")
        return await real_record(*a, **kw)

    store.record_failed_attempt = record
    db = AsyncMock()
    db.rollback.side_effect = lambda: events.append("release_connection")
    http = MagicMock()
    http.client.host = "198.51.100.9"
    http.headers.get.return_value = None
    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch("app.routers.auth.AuthRepository") as repo_cls, \
         patch("app.routers.auth.burn_password_check_async",
               new=AsyncMock(side_effect=lambda *_: events.append("scrypt"))):
        repo_cls.return_value.get_user_by_username = AsyncMock(return_value=None)
        with pytest.raises(HTTPException):
            _run(login(LoginRequest(username="ghost", password="x"), MagicMock(), http, db=db))
    assert events == ["release_connection", "scrypt", "record_failure"]
