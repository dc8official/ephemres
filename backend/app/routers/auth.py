"""Sign-in, sign-out and change-password routes, and the dependencies that authenticate every
other API request (get_current_user, require_admin).

A request is accepted when its token is validly signed and unexpired, its session (jti) is still
in the session store and within the account's idle timeout (and, for admin, under 12 hours
old), and the account still exists and is active. Role and the forced password change come from
the database row each time, not from the token.

A refused session answers 401 with X-Session-End-Reason (session_policy.END_REASONS) and, for an
idle timeout, X-Session-Idle-Minutes, so the sign-in page can say why the user was signed out.
POST /auth/activity is the only call that counts as interaction.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Request, Response
from app.core.orm import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal, get_db
from app.repositories.auth_repo import AuthRepository
from app.schemas import APIResponse
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
)
import secrets
from app.services import session_policy
from app.services.driver_manager import driver_manager
from app.services.auth_service import (
    burn_password_check_async,
    create_access_token,
    decode_access_token,
    get_trusted_client_ip,
    hash_password,
    hash_password_async,
    invalidate_all_user_sessions_async,
    invalidate_session_async,
    is_session_active,
    needs_rehash,
    register_session_async,
    session_end_reason_async,
    touch_session_async,
    verify_password,
    verify_password_async,
)

logger = logging.getLogger(__name__)

cookie_name = "ephemres_access_token"


def get_client_ip(request: Request) -> str:
    """Extracts client source IP address with trusted proxy validation."""
    return get_trusted_client_ip(request)


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
):
    client_ip = get_client_ip(http_request)
    # Lockouts live in the shared session store only (the auth_login_failures table), so every
    # worker sees the same counters and a restart or junk-username flood cannot reset them.
    store = driver_manager.get_session_store()
    try:
        is_locked = await store.is_account_locked(client_ip, payload.username)
    except Exception:
        logger.exception("Lockout check failed; refusing sign-in")
        raise HTTPException(status_code=503, detail="Sign-in is temporarily unavailable.")

    if is_locked:
        raise HTTPException(
            status_code=403,
            detail="Account temporarily locked for 15 minutes due to multiple failed login attempts from this location.",
        )

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_username(payload.username)
    # Give the connection back before the slow scrypt check and the lockout write, which takes
    # a connection of its own: holding one while waiting for another deadlocks the pool once a
    # sign-in flood fills it. Later statements check a connection out again.
    await db.rollback()

    is_active = getattr(user, "is_active", False) if user else False
    password_hash = getattr(user, "password_hash", "") if user else ""

    if not user or not is_active:
        # Same scrypt cost as a real account, so response time does not reveal usernames.
        await burn_password_check_async(payload.password)
        is_valid = False
    else:
        is_valid = await verify_password_async(payload.password, password_hash)

    if not is_valid:
        try:
            await store.record_failed_attempt(client_ip, payload.username)
        except Exception:
            logger.exception("Could not record failed sign-in for %s", client_ip)
        logger.warning("Failed sign-in for username %r from %s", payload.username, client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    try:
        await store.clear_failed_attempts(client_ip, payload.username)
    except Exception:
        logger.exception("Could not clear failed sign-ins for %s", client_ip)

    if needs_rehash(password_hash):
        user.password_hash = await hash_password_async(payload.password)
        await db.commit()

    user_id = getattr(user, "id", None)
    username = getattr(user, "username", payload.username)
    # For the token and the response (what the browser shows); access checks re-read the role
    # from the database on every request.
    role_name = "VIEWER"
    if hasattr(user, "role_name") and isinstance(user.role_name, str):
        role_name = user.role_name
    elif hasattr(user, "role"):
        if isinstance(user.role, str):
            role_name = user.role
        elif hasattr(user.role, "role_name") and isinstance(user.role.role_name, str):
            role_name = user.role.role_name
    must_change_password = bool(getattr(user, "must_change_password", False))

    now = datetime.now(timezone.utc)
    if user_id:
        await auth_repo.update_last_login(user_id, now)
        await auth_repo.create_audit_log(
            user_id=user_id,
            action="USER:LOGIN",
            target_type="users",
            target_id=user_id,
            details={"username": payload.username, "ip": client_ip},
        )
        await db.commit()

    # Register the session before handing out the token. Past the account's own session limit
    # (users.max_sessions) the oldest session of the account is ended, and its browser is told
    # it was signed in elsewhere.
    session_id = str(secrets.token_hex(16))
    max_sess = getattr(user, "max_sessions", None)
    if not isinstance(max_sess, int):
        max_sess = session_policy.default_max_sessions()
    # Address and browser go with the session only so the Users tab can tell devices apart.
    user_agent = http_request.headers.get("user-agent") if hasattr(http_request, "headers") else None
    await register_session_async(
        str(user_id), session_id, max_sessions=max_sess, role_name=str(role_name),
        client_ip=client_ip, user_agent=user_agent if isinstance(user_agent, str) else None,
    )

    token = create_access_token(
        user_id=str(user_id),
        username=username,
        role_name=str(role_name),
        jti=session_id,
    )

    response_body = LoginResponse(
        username=username,
        role=str(role_name),
        must_change_password=must_change_password,
        message="Login successful.",
    )

    # Behind nginx the request can arrive here as plain http even when the browser used HTTPS, so
    # an HSTS-enabled install also marks the cookie Secure.
    is_secure = http_request.url.scheme == "https" or getattr(
        settings.security, "hsts_enabled", False
    )

    # HttpOnly: page scripts can't read it. SameSite=Lax: other sites can't make the browser
    # send it with a POST, the main defence against cross-site request forgery here.
    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/",
        # The cookie lives as long as the token (the session's absolute end plus a grace); the
        # session row ends it earlier when the account is idle.
        max_age=int(session_policy.token_lifetime(str(role_name)).total_seconds()),
    )

    return APIResponse.success(data=response_body)


@router.post("/logout")
async def logout(request: Request, response: Response):
    """End this session in the store and clear the cookie. Always succeeds, even with an
    expired or invalid token, so the browser can always sign out."""
    token = request.cookies.get(cookie_name)
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
    if token:
        payload = decode_access_token(token)
        if payload and payload.get("sub") and payload.get("jti"):
            u_id = str(payload.get("sub"))
            jti = str(payload.get("jti"))
            await invalidate_session_async(u_id, jti)

    is_secure = request.url.scheme == "https" or getattr(
        settings.security, "hsts_enabled", False
    )
    response.delete_cookie(
        key=cookie_name,
        path="/",
        secure=is_secure,
        httponly=True,
        samesite="lax",
    )
    return APIResponse.success(data={"message": "Logged out."})


def _role_name_of(user) -> str:
    """The account's current role from its database row (never from the token)."""
    role_name = getattr(user, "role_name", None)
    if isinstance(role_name, str) and role_name:
        return role_name
    role = getattr(user, "role", None)
    if isinstance(role, str) and role:
        return role
    nested = getattr(role, "role_name", None)
    if isinstance(nested, str) and nested:
        return nested
    return "VIEWER"


# Plain-language 401 messages for scripts and API clients; the browser builds its own from the
# X-Session-End-Reason header.
_SESSION_ENDED_DETAIL = {
    session_policy.IDLE_TIMEOUT: "Session ended after a period without activity.",
    session_policy.ABSOLUTE_TIMEOUT: "Session reached its maximum length. Please sign in again.",
    session_policy.SIGNED_IN_ELSEWHERE: "Session ended because this account signed in elsewhere.",
    session_policy.SIGNED_OUT: "Session was signed out.",
    session_policy.PASSWORD_CHANGED: "Session ended because the account's password was changed.",
    session_policy.ENDED_BY_ADMIN: "Session was ended by an administrator.",
}


def _session_end_headers(reason: Optional[str], idle_minutes: Optional[int]) -> dict:
    if not reason:
        return {}
    headers = {"X-Session-End-Reason": reason}
    if reason == session_policy.IDLE_TIMEOUT and idle_minutes:
        headers["X-Session-Idle-Minutes"] = str(int(idle_minutes))
    return headers


async def get_current_user_allow_password_change(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Authenticates the request without the forced-password-change gate.

    Only the change-password route uses this directly; every other route goes through
    get_current_user, which refuses accounts that still have to change their password.
    """
    # The cookie, the cookie's name in earlier releases, or a Bearer header (scripts and API
    # clients).
    token = request.cookies.get(cookie_name) or request.cookies.get("lnmp_access_token")
    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]
    if not token:
        logger.warning(
            "Authentication failed: Missing credentials for request to %s",
            request.url.path,
        )
        raise HTTPException(status_code=401, detail="Not authenticated.")

    payload = decode_access_token(token)
    if payload is None:
        logger.warning(
            "Authentication failed: Session token is expired, tampered, or invalid"
        )
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    user_id_str = payload.get("sub")
    jti = payload.get("jti")
    if not user_id_str or not jti:
        logger.warning(
            "Authentication failed: Token missing required claims (sub or jti)"
        )
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    # Signed and unexpired is not enough: the session must not have been ended by sign-out, a
    # password change or reset, a newer sign-in (the per-user session limit), or a timeout.
    if not await is_session_active(str(user_id_str), jti):
        found = await session_end_reason_async(str(user_id_str), jti)
        reason, idle_minutes = found if found else (None, None)
        logger.info("Session refused for user %s: %s", user_id_str, reason or "unknown reason")
        raise HTTPException(
            status_code=401,
            detail=_SESSION_ENDED_DETAIL.get(reason, "Your session has ended. Please sign in again."),
            headers=_session_end_headers(reason, idle_minutes),
        )

    try:
        user_uuid = UUID(str(user_id_str))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid token claims.")

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_id(user_uuid)

    is_active = getattr(user, "is_active", False) if user else False
    if not user or not is_active:
        logger.warning(
            "Authentication rejected: User account is inactive or disabled"
        )
        raise HTTPException(
            status_code=401, detail="User account is inactive or disabled."
        )

    # Authorization comes from the database row on every request: a demoted or re-promoted
    # account takes effect immediately instead of when its token expires.
    payload["role"] = _role_name_of(user)
    payload["must_change_password"] = bool(getattr(user, "must_change_password", False))
    return payload


PASSWORD_CHANGE_REQUIRED_DETAIL = "Password change required before using Ephemres."


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Authenticated user for every API route except change-password.

    Accounts flagged must_change_password (new accounts, admin resets, the installer's admin)
    get 403 until they change it. The browser shows the change form, but the API enforces it,
    so a temporary or default password cannot be used with plain HTTP calls.
    """
    payload = await get_current_user_allow_password_change(request=request, db=db)
    if payload.get("must_change_password"):
        raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED_DETAIL)
    return payload


async def get_current_user_sse(request: Request) -> dict:
    """Dedicated authentication dependency for long-lived Server-Sent Events (SSE) connections.

    Standard ASGI route dependencies holding an AsyncSession via Depends(get_db) do not close
    their database connection until the response generator completes. For long-lived or
    infinite streams like SSE, this monopolizes a connection from the native asyncpg connection
    pool indefinitely, causing database pool starvation for concurrent HTTP requests.

    This function uses a short-lived AsyncSessionLocal() context manager strictly for the
    initial authentication and user status check, releasing the database connection immediately
    before the SSE stream begins yielding events.
    """
    async with AsyncSessionLocal() as session:
        return await get_current_user(request=request, db=session)


async def require_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Admin-only routes. The role here is the database's (set by get_current_user)."""
    if current_user.get("role") != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required.")
    return current_user


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user_allow_password_change),
    db: AsyncSession = Depends(get_db),
):
    try:
        user_uuid = UUID(str(current_user.get("sub")))
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="User not found.")

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_id(user_uuid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    # Same as login: no pooled connection held across scrypt and the session-store calls.
    await db.rollback()

    password_hash = getattr(user, "password_hash", "")

    # Always required, also for a forced change: the temporary password proves the caller is
    # the account holder and not someone holding a copied session cookie.
    if not (request.old_password and request.old_password.strip()):
        raise HTTPException(status_code=400, detail="Current password is required.")
    is_valid = await verify_password_async(request.old_password.strip(), password_hash)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid current password.")

    if not request.new_password or len(request.new_password.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 8 characters long.",
        )

    clean_new_pass = request.new_password.strip()
    hashed = await hash_password_async(clean_new_pass)

    await auth_repo.update_password(user_uuid, hashed, must_change_password=False)
    # Sign out every other session of this account (another device, or whoever had the old
    # password); the one making the change stays signed in.
    current_jti = current_user.get("jti")
    await invalidate_all_user_sessions_async(
        str(user_uuid), except_jti=current_jti, reason=session_policy.PASSWORD_CHANGED
    )
    await auth_repo.create_audit_log(
        user_id=user_uuid,
        action="USER:CHANGE_PASSWORD",
        target_type="users",
        target_id=user_uuid,
        details={"username": current_user.get("username")},
    )
    await db.commit()

    return APIResponse.success(data={"message": "Password changed successfully."})


@router.post("/activity")
async def record_activity(
    current_user: dict = Depends(get_current_user_allow_password_change),
):
    """The browser reports real interaction (clicks, key presses, navigation) here, at most once
    a minute; it restarts the session's idle timeout. Nothing else does: dashboard polling and
    the live-event stream go through other routes, so a screen left open still idles out.

    Allowed while a password change is pending, so the change form does not time out under a
    user who is typing. A user could call this from a script to stay signed in; that is
    accepted, as they could equally keep clicking (v3.3.6 build plan).
    """
    await touch_session_async(str(current_user.get("sub")), current_user.get("jti"))
    return APIResponse.success(data={"recorded": True})
