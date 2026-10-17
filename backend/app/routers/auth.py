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
from app.services.auth_service import (
    clear_failed_attempts,
    create_access_token,
    decode_access_token,
    get_trusted_client_ip,
    hash_password,
    hash_password_async,
    invalidate_all_user_sessions,
    invalidate_session,
    is_account_locked,
    is_session_active,
    record_failed_attempt,
    register_session,
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
    store = driver_manager.get_session_store()
    is_locked = False
    try:
        is_locked = await store.is_account_locked(client_ip, payload.username)
    except Exception:
        pass
    if not is_locked:
        is_locked = is_account_locked(client_ip, payload.username)

    if is_locked:
        raise HTTPException(
            status_code=403,
            detail="Account temporarily locked for 15 minutes due to multiple failed login attempts from this location.",
        )

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_username(payload.username)

    is_active = getattr(user, "is_active", False) if user else False
    password_hash = getattr(user, "password_hash", "") if user else ""

    if not user or not is_active:
        record_failed_attempt(client_ip, payload.username)
        try:
            await store.record_failed_attempt(client_ip, payload.username)
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    is_valid = await verify_password_async(payload.password, password_hash)
    if not is_valid:
        record_failed_attempt(client_ip, payload.username)
        try:
            await store.record_failed_attempt(client_ip, payload.username)
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid username or password.")

    clear_failed_attempts(client_ip, payload.username)
    try:
        await store.clear_failed_attempts(client_ip, payload.username)
    except Exception:
        pass

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
    store = driver_manager.get_session_store()
    await store.register_session(str(user_id), session_id, max_sessions=max_sess)
    register_session(str(user_id), session_id, max_sessions=max_sess)

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
        max_age=settings.security.session_timeout_minutes * 60,
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
            store = driver_manager.get_session_store()
            await store.invalidate_session(u_id, jti)
            invalidate_session(u_id, jti)

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


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    client_ip = get_client_ip(request)
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

    store = driver_manager.get_session_store()
    if not await store.is_session_active(str(user_id_str), jti):
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

    payload["must_change_password"] = getattr(
        user, "must_change_password", False
    )
    return payload


async def get_current_user_sse(request: Request) -> dict:
    """Dedicated authentication dependency for long-lived Server-Sent Events (SSE) connections.

    Standard FastAPI dependencies holding an AsyncSession via Depends(get_db) do not close
    their database connection until the response generator completes. For long-lived or
    infinite streams like SSE, this monopolizes a connection from the SQLAlchemy connection
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


async def require_operator_or_admin(
    current_user: dict = Depends(get_current_user),
) -> dict:
    role = current_user.get("role")
    if role not in ("ADMIN", "OPERATOR"):
        raise HTTPException(
            status_code=403,
            detail="Operator or Admin access required to export reports.",
        )
    return current_user


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    current_user: dict = Depends(get_current_user),
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

    password_hash = getattr(user, "password_hash", "")
    must_change_password = getattr(user, "must_change_password", False)

    if request.old_password and request.old_password.strip():
        is_valid = await verify_password_async(
            request.old_password.strip(), password_hash
        )
        if not is_valid:
            raise HTTPException(status_code=400, detail="Invalid current password.")
    elif not must_change_password:
        raise HTTPException(status_code=400, detail="Current password is required.")

    if not request.new_password or len(request.new_password.strip()) < 8:
        raise HTTPException(
            status_code=400,
            detail="New password must be at least 8 characters long.",
        )

    clean_new_pass = request.new_password.strip()
    hashed = await hash_password_async(clean_new_pass)

    await auth_repo.update_password(user_uuid, hashed, must_change_password=False)
    store = driver_manager.get_session_store()
    await store.invalidate_all_user_sessions(str(user_uuid))
    invalidate_all_user_sessions(str(user_uuid))
    await auth_repo.create_audit_log(
        user_id=user_uuid,
        action="USER:CHANGE_PASSWORD",
        target_type="users",
        target_id=user_uuid,
        details={"username": current_user.get("username")},
    )
    await db.commit()

    return APIResponse.success(data={"message": "Password changed successfully."})
