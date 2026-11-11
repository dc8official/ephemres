"""Per-account sessions (v3.3.6 build plan, phase 1), without a database.

What these pin:
* the idle timeout choices per role, and how a role change fits the timeout to the new role;
* which end applies first (idle, the admin 12-hour cap, the viewer backstop);
* a refused session answers 401 with X-Session-End-Reason, so the sign-in page can say why;
* the Users API accepts only the role's choices, fits the timeout on a role change, and lets an
  admin change their own timeout but not their own role;
* POST /auth/activity is what restarts the idle timeout.

The same rules against real PostgreSQL (timeouts, the sweep, two workers) are in
tests/integration/test_sessions_idle.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.core.asgi import HTTPException
from app.services import session_policy
from app.services.driver_manager import driver_manager
from app.services.session_store import _timed_out_reason
from tests.backend.session_store_double import InMemorySessionStore


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- policy ---------------------------------------------------------------------------------


def test_choices_and_defaults_per_role():
    assert session_policy.idle_choices("ADMIN") == (15, 30, 60)
    assert session_policy.idle_choices("VIEWER") == (30, 60, 120, 240)
    assert session_policy.default_idle_minutes("ADMIN") == 30
    assert session_policy.default_idle_minutes("VIEWER") == 120
    # Anything that is not ADMIN gets the viewer rules (accounts without a role included).
    assert session_policy.idle_choices(None) == session_policy.VIEWER_IDLE_CHOICES


@pytest.mark.parametrize(
    "role, minutes, expected",
    [
        ("ADMIN", 240, 60),  # a viewer's 4 hours becomes an admin's 1 hour
        ("ADMIN", 45, 30),
        ("ADMIN", 15, 15),
        ("VIEWER", 15, 30),  # an admin's 15 minutes becomes a viewer's 30
        ("VIEWER", 60, 60),
        ("VIEWER", 1440, 240),  # the old 24-hour global timeout
        ("VIEWER", "junk", 120),
    ],
)
def test_clamp_fits_a_timeout_to_the_role(role, minutes, expected):
    assert session_policy.clamp_idle_minutes(role, minutes) == expected


def test_token_outlives_the_session_by_the_grace():
    assert session_policy.token_lifetime("ADMIN") == timedelta(hours=13)
    assert session_policy.token_lifetime("VIEWER") == timedelta(days=7, hours=1)


# --- which end applies ----------------------------------------------------------------------

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _row(role, idle_minutes, created_ago, active_ago, expires_in):
    return (
        NOW - created_ago,
        NOW - active_ago,
        NOW + expires_in,
        idle_minutes,
        role,
    )


def test_recent_activity_keeps_a_session_alive():
    row = _row("VIEWER", 30, timedelta(hours=3), timedelta(minutes=29), timedelta(days=6))
    assert _timed_out_reason(row, NOW) is None


def test_idle_timeout_ends_a_session():
    row = _row("VIEWER", 30, timedelta(hours=3), timedelta(minutes=30), timedelta(days=6))
    assert _timed_out_reason(row, NOW) == session_policy.IDLE_TIMEOUT


def test_viewers_with_different_timeouts_end_at_different_times():
    two_hours = _row("VIEWER", 120, timedelta(hours=4), timedelta(hours=3), timedelta(days=6))
    four_hours = _row("VIEWER", 240, timedelta(hours=4), timedelta(hours=3), timedelta(days=6))
    assert _timed_out_reason(two_hours, NOW) == session_policy.IDLE_TIMEOUT
    assert _timed_out_reason(four_hours, NOW) is None


def test_admin_session_ends_at_12_hours_despite_activity():
    # expires_at says much later (a session created before a promotion, say): the cap still holds.
    row = _row("ADMIN", 60, timedelta(hours=12), timedelta(seconds=5), timedelta(days=1))
    assert _timed_out_reason(row, NOW) == session_policy.ABSOLUTE_TIMEOUT


def test_viewer_has_no_12_hour_cap():
    row = _row("VIEWER", 240, timedelta(hours=30), timedelta(minutes=5), timedelta(days=5))
    assert _timed_out_reason(row, NOW) is None


def test_the_earlier_end_decides_the_reason():
    # Idle since 11 h 0 m (ended at 11 h 15 m), cap at 12 h: idle came first.
    row = _row("ADMIN", 15, timedelta(hours=12, minutes=5), timedelta(hours=1, minutes=5), timedelta(days=1))
    assert _timed_out_reason(row, NOW) == session_policy.IDLE_TIMEOUT


# --- 401 explains itself --------------------------------------------------------------------


def _request(token="tok"):
    req = MagicMock()
    req.cookies.get.side_effect = lambda name: token if name == "ephemres_access_token" else None
    req.headers.get.return_value = None
    return req


def _authenticate_with(store, sub, jti):
    from app.routers import auth

    payload = {"sub": sub, "jti": jti, "username": "u", "role": "VIEWER"}
    with patch.object(auth, "decode_access_token", return_value=dict(payload)), \
         patch.object(driver_manager, "get_session_store", return_value=store):
        return _run(auth.get_current_user_allow_password_change(_request(), db=AsyncMock()))


def test_evicted_session_is_told_it_signed_in_elsewhere():
    store = InMemorySessionStore()
    sub = str(uuid4())
    _run(store.register_session(sub, "first", max_sessions=1))
    _run(store.register_session(sub, "second", max_sessions=1))
    with pytest.raises(HTTPException) as exc:
        _authenticate_with(store, sub, "first")
    assert exc.value.status_code == 401
    assert exc.value.headers == {"X-Session-End-Reason": "signed_in_elsewhere"}
    assert "signed in elsewhere" in exc.value.detail


def test_idle_session_401_carries_the_minutes():
    store = InMemorySessionStore()
    sub = str(uuid4())
    store.state.ended["idle-jti"] = (session_policy.IDLE_TIMEOUT, 30)
    with pytest.raises(HTTPException) as exc:
        _authenticate_with(store, sub, "idle-jti")
    assert exc.value.headers == {"X-Session-End-Reason": "idle_timeout", "X-Session-Idle-Minutes": "30"}


def test_unknown_reason_is_a_plain_401():
    """No record (an old token, or the store could not say): refused all the same, no header."""
    store = InMemorySessionStore()
    with pytest.raises(HTTPException) as exc:
        _authenticate_with(store, str(uuid4()), "never-seen")
    assert exc.value.status_code == 401
    assert exc.value.headers == {}


def test_reason_lookup_failure_still_refuses_with_401():
    store = InMemorySessionStore()
    store.session_end_reason = AsyncMock(side_effect=RuntimeError("store hiccup"))
    with pytest.raises(HTTPException) as exc:
        _authenticate_with(store, str(uuid4()), "gone")
    assert exc.value.status_code == 401


def test_password_change_tells_other_sessions_why():
    from app.services.auth_service import invalidate_all_user_sessions_async

    store = InMemorySessionStore()
    sub = str(uuid4())
    _run(store.register_session(sub, "laptop"))
    _run(store.register_session(sub, "phone"))
    with patch.object(driver_manager, "get_session_store", return_value=store):
        _run(invalidate_all_user_sessions_async(sub, except_jti="laptop", reason="password_changed"))
    assert store.state.ended["phone"] == ("password_changed", None)
    assert "laptop" not in store.state.ended


# --- sign-in uses the account's own limit and lifetime ---------------------------------------


def test_login_registers_with_the_accounts_limit_and_role_lifetime():
    from app.routers import auth
    from app.schemas.auth import LoginRequest

    user = SimpleNamespace(
        id=uuid4(), username="ops", is_active=True, must_change_password=False,
        password_hash="$scrypt$x", role=SimpleNamespace(role_name="ADMIN"),
        max_sessions=1, session_idle_minutes=15,
    )
    store = InMemorySessionStore()
    response = MagicMock()
    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch.object(auth, "AuthRepository") as repo_cls, \
         patch.object(auth, "verify_password_async", AsyncMock(return_value=True)), \
         patch.object(auth, "needs_rehash", return_value=False):
        repo = repo_cls.return_value
        repo.get_user_by_username = AsyncMock(return_value=user)
        repo.update_last_login = AsyncMock()
        repo.create_audit_log = AsyncMock()
        req = MagicMock()
        req.url.scheme = "https"
        req.headers.get.side_effect = lambda name, default=None: (
            "Mozilla/5.0 (Windows NT 10.0) Chrome/130" if name.lower() == "user-agent" else default
        )
        with patch.object(auth, "get_client_ip", return_value="127.0.0.1"):
            _run(auth.login(LoginRequest(username="ops", password="pw-123456"), response, req, db=AsyncMock()))
            _run(auth.login(LoginRequest(username="ops", password="pw-123456"), response, req, db=AsyncMock()))

    # The address and browser are kept for the Users tab's session list.
    assert set(store.state.devices.values()) == {("127.0.0.1", "Mozilla/5.0 (Windows NT 10.0) Chrome/130")}
    # max_sessions=1: the second sign-in ended the first.
    assert len(store.state.sessions[str(user.id)]) == 1
    assert store.last_ttl_seconds == 12 * 3600
    max_age = response.set_cookie.call_args.kwargs["max_age"]
    assert max_age == 13 * 3600


# --- Users API ------------------------------------------------------------------------------


def _target(role="VIEWER", idle=120, max_sessions=2):
    return SimpleNamespace(
        id=uuid4(), username="op", role_id=uuid4(), role=SimpleNamespace(role_name=role),
        session_idle_minutes=idle, max_sessions=max_sessions,
    )


def _update(target, request, current_sub=None):
    from app.routers import users

    with patch.object(users, "AuthRepository") as repo_cls, \
         patch.object(users, "invalidate_all_user_sessions_async", new=AsyncMock()) as revoke:
        repo = repo_cls.return_value
        repo.get_user_by_id = AsyncMock(return_value=target)
        repo.get_role_by_name = AsyncMock(
            side_effect=lambda name: SimpleNamespace(id=uuid4(), role_name=name)
        )
        repo.update_user = AsyncMock()
        repo.create_audit_log = AsyncMock()
        _run(users.update_user(
            target.id, request,
            current_user={"sub": current_sub or str(uuid4()), "role": "ADMIN"}, db=AsyncMock(),
        ))
        return repo.update_user, revoke


def test_promotion_fits_the_timeout_to_an_admin_choice():
    from app.schemas.users import UpdateUserRequest

    update, revoke = _update(_target(idle=240), UpdateUserRequest(role="ADMIN"))
    assert update.await_args.kwargs["session_idle_minutes"] == 60
    revoke.assert_awaited_once()


def test_a_timeout_the_role_does_not_offer_is_refused():
    from app.schemas.users import UpdateUserRequest

    with pytest.raises(HTTPException) as exc:
        _update(_target(role="ADMIN", idle=30), UpdateUserRequest(session_idle_minutes=240))
    assert exc.value.status_code == 400
    assert "15, 30, 60" in exc.value.detail


def test_timeout_change_does_not_sign_the_account_out():
    """It applies to live sessions on their next request instead (the store reads it each time)."""
    from app.schemas.users import UpdateUserRequest

    update, revoke = _update(_target(idle=120), UpdateUserRequest(session_idle_minutes=240, max_sessions=3))
    assert update.await_args.kwargs == {"session_idle_minutes": 240, "max_sessions": 3}
    revoke.assert_not_awaited()


def test_admin_can_set_own_timeout_but_not_own_role():
    from app.schemas.users import UpdateUserRequest

    me = _target(role="ADMIN", idle=30)
    update, _ = _update(me, UpdateUserRequest(session_idle_minutes=60), current_sub=str(me.id))
    assert update.await_args.kwargs == {"session_idle_minutes": 60}

    with pytest.raises(HTTPException) as exc:
        _update(me, UpdateUserRequest(role="VIEWER"), current_sub=str(me.id))
    assert exc.value.status_code == 400


def test_create_uses_role_default_and_config_limit():
    from app.routers import users
    from app.schemas.users import CreateUserRequest

    with patch.object(users, "AuthRepository") as repo_cls, \
         patch.object(users, "hash_password_async", AsyncMock(return_value="h")):
        repo = repo_cls.return_value
        repo.get_user_by_username = AsyncMock(return_value=None)
        repo.get_role_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        repo.create_user = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        repo.create_audit_log = AsyncMock()
        _run(users.create_user(
            CreateUserRequest(username="wall", password="pw-123456", role="VIEWER"),
            current_user={"sub": str(uuid4()), "role": "ADMIN"}, db=AsyncMock(),
        ))
        kwargs = repo.create_user.await_args.kwargs
    assert kwargs["session_idle_minutes"] == 120
    assert kwargs["max_sessions"] == session_policy.default_max_sessions()

    with pytest.raises(HTTPException) as exc, patch.object(users, "AuthRepository") as repo_cls:
        repo_cls.return_value.get_user_by_username = AsyncMock(return_value=None)
        repo_cls.return_value.get_role_by_name = AsyncMock(return_value=SimpleNamespace(id=uuid4()))
        _run(users.create_user(
            CreateUserRequest(username="boss", role="ADMIN", session_idle_minutes=120),
            current_user={"sub": str(uuid4()), "role": "ADMIN"}, db=AsyncMock(),
        ))
    assert exc.value.status_code == 400


# --- activity -------------------------------------------------------------------------------


def test_activity_route_touches_only_its_own_session():
    from app.routers import auth

    store = InMemorySessionStore()
    sub = str(uuid4())
    _run(store.register_session(sub, "mine"))
    _run(store.register_session(sub, "other"))
    with patch.object(driver_manager, "get_session_store", return_value=store):
        _run(auth.record_activity(current_user={"sub": sub, "jti": "mine"}))
    assert store.state.touches == {"mine": 1}


def test_ordinary_authentication_does_not_count_as_activity():
    """Polling and the live-event stream authenticate like any request; that must not touch."""
    store = InMemorySessionStore()
    sub = str(uuid4())
    _run(store.register_session(sub, "poller"))
    user = SimpleNamespace(id=uuid4(), is_active=True, must_change_password=False,
                           role=SimpleNamespace(role_name="VIEWER"))
    from app.routers import auth

    db = AsyncMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = user
    db.execute.return_value = res
    payload = {"sub": sub, "jti": "poller", "username": "u", "role": "VIEWER"}
    with patch.object(auth, "decode_access_token", return_value=dict(payload)), \
         patch.object(driver_manager, "get_session_store", return_value=store):
        for _ in range(3):
            _run(auth.get_current_user(_request(), db=db))
    assert store.state.touches == {}


# --- active sessions list (Users tab) -------------------------------------------------------


def _sessions_setup():
    store = InMemorySessionStore()
    target = str(uuid4())
    _run(store.register_session(target, "phone", client_ip="10.0.0.5", user_agent="Mozilla/5.0 (iPhone)"))
    _run(store.register_session(target, "laptop", client_ip="10.0.0.9", user_agent="Mozilla/5.0 (Windows)"))
    return store, target


def test_session_list_never_returns_the_token_id():
    from app.routers import users

    store, target = _sessions_setup()
    with patch.object(driver_manager, "get_session_store", return_value=store):
        res = _run(users.list_user_sessions(target, current_user={"sub": target, "jti": "laptop", "role": "ADMIN"}))
    rows = res.data if hasattr(res, "data") else res["data"]
    assert [r["client_ip"] for r in rows] == ["10.0.0.9", "10.0.0.5"]
    assert [r["is_current"] for r in rows] == [True, False]
    assert all("jti" not in r for r in rows)


def _end(store, target, session_id, current_user):
    from app.routers import users

    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch.object(users, "AuthRepository") as repo_cls:
        repo_cls.return_value.create_audit_log = AsyncMock()
        _run(users.end_user_session(target, session_id, current_user=current_user, db=AsyncMock()))
        return repo_cls.return_value.create_audit_log


def test_admin_ends_one_session_and_it_is_told_why():
    store, target = _sessions_setup()
    audit = _end(store, target, store.row_id_of("phone"), {"sub": str(uuid4()), "jti": "x", "role": "ADMIN"})
    assert store.state.sessions[target] == ["laptop"]
    assert store.state.ended["phone"] == ("ended_by_admin", None)
    assert audit.await_args.kwargs["action"] == "USER:END_SESSION"
    assert audit.await_args.kwargs["details"]["client_ip"] == "10.0.0.5"


def test_own_current_session_is_refused_and_unknown_is_404():
    store, target = _sessions_setup()
    with pytest.raises(HTTPException) as exc:
        _end(store, target, store.row_id_of("laptop"), {"sub": target, "jti": "laptop", "role": "ADMIN"})
    assert exc.value.status_code == 400
    assert "laptop" in store.state.sessions[target]

    with pytest.raises(HTTPException) as exc:
        _end(store, target, str(uuid4()), {"sub": str(uuid4()), "jti": "x", "role": "ADMIN"})
    assert exc.value.status_code == 404


def test_end_all_on_own_account_keeps_this_browser():
    from app.routers import users

    store, me = _sessions_setup()
    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch.object(users, "AuthRepository") as repo_cls:
        repo_cls.return_value.create_audit_log = AsyncMock()
        _run(users.end_all_user_sessions(me, current_user={"sub": me, "jti": "laptop", "role": "ADMIN"}, db=AsyncMock()))
    assert store.state.sessions[me] == ["laptop"]
    assert store.state.ended["phone"] == ("ended_by_admin", None)


def test_end_all_on_another_account_ends_everything():
    from app.routers import users

    store, target = _sessions_setup()
    with patch.object(driver_manager, "get_session_store", return_value=store), \
         patch.object(users, "AuthRepository") as repo_cls:
        repo_cls.return_value.create_audit_log = AsyncMock()
        _run(users.end_all_user_sessions(target, current_user={"sub": str(uuid4()), "jti": "x", "role": "ADMIN"}, db=AsyncMock()))
    assert store.state.sessions[target] == []


def test_user_list_carries_live_session_counts_and_survives_a_store_error():
    from app.routers import users

    store, target = _sessions_setup()
    account = SimpleNamespace(
        id=target, username="op", is_active=True, must_change_password=False, last_login=None,
        created_at=NOW, role=SimpleNamespace(role_name="VIEWER"), session_idle_minutes=120, max_sessions=2,
    )

    def listed(s):
        with patch.object(driver_manager, "get_session_store", return_value=s), \
             patch.object(users, "AuthRepository") as repo_cls:
            repo_cls.return_value.list_users = AsyncMock(return_value=[account])
            res = _run(users.list_users(current_user={"sub": "a", "role": "ADMIN"}, db=AsyncMock()))
        return (res.data if hasattr(res, "data") else res["data"])[0]["active_sessions"]

    assert listed(store) == 2
    broken = InMemorySessionStore()
    broken.active_session_counts = AsyncMock(side_effect=RuntimeError("down"))
    assert listed(broken) is None
