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
from app.services.driver_manager import driver_manager
from app.services.runtime_settings import get_runtime_settings
from app.services.auth_service import (
    burn_password_check_async,
    clear_failed_attempts,
    create_access_token,
    decode_access_token,
    get_trusted_client_ip,
    hash_password,
    hash_password_async,
    invalidate_all_user_sessions,
    invalidate_all_user_sessions_async,
    invalidate_session,
    invalidate_session_async,
    is_account_locked,
    is_session_active,
    needs_rehash,
    record_failed_attempt,
    register_session,
    register_session_async,
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
    # Lockouts live in the shared session store only (PostgreSQL table or FastStore), so every
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

    session_id = str(secrets.token_hex(16))
    max_sess = getattr(settings.security, "max_active_sessions_per_user", 2)
    await register_session_async(str(user_id), session_id, max_sessions=max_sess)

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

    is_secure = http_request.url.scheme == "https" or getattr(
        settings.security, "hsts_enabled", False
    )

    response.set_cookie(
        key=cookie_name,
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/",
        max_age=get_runtime_settings().session_timeout_minutes * 60,
    )

    return APIResponse.success(data=response_body)


@router.post("/logout")
async def logout(request: Request, response: Response):
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


async def get_current_user_allow_password_change(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Authenticates the request without the forced-password-change gate.

    Only the change-password route uses this directly; every other route goes through
    get_current_user, which refuses accounts that still have to change their password.
    """
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

    if not await is_session_active(str(user_id_str), jti):
        logger.warning(
            "Authentication session evicted (quota exceeded or expired)"
        )
        raise HTTPException(
            status_code=401,
            detail="Session terminated: maximum active sessions reached or session expired.",
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
    current_jti = current_user.get("jti")
    await invalidate_all_user_sessions_async(str(user_uuid), except_jti=current_jti)
    await auth_repo.create_audit_log(
        user_id=user_uuid,
        action="USER:CHANGE_PASSWORD",
        target_type="users",
        target_id=user_uuid,
        details={"username": current_user.get("username")},
    )
    await db.commit()

    return APIResponse.success(data={"message": "Password changed successfully."})
