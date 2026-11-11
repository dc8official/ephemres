"""User management (admin only): list, create, reset a password, edit, deactivate.

An admin can't reset, deactivate or change the role of their own account here; their own
password changes through /auth/change-password. They can set their own session timeout and
session limit, which only shorten or lengthen their own sign-ins.

Each account has its own idle timeout and session limit (v3.3.6, services/session_policy.py).
A changed timeout applies to the account's live sessions on their next request, because the
session check reads it from the account each time.

An admin can see an account's live sessions (when and where each signed in, when it ends if
left alone) and end one or all of them. Ended sessions are told "ended by an administrator". A reset sets a new password (generated when none is given and
returned once), forces a change at the next sign-in, and signs the user out everywhere.
"Delete" deactivates: the account and its audit history stay.
"""
from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException
from app.core.orm import AsyncSession

from app.database import get_db
from app.repositories.auth_repo import AuthRepository
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse
from app.schemas.users import (
    CreateUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserSummary,
)
from app.services import session_policy
from app.services.driver_manager import driver_manager
from app.services.auth_service import (
    generate_readable_password,
    hash_password,
    hash_password_async,
    invalidate_all_user_sessions_async,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


def _role_name_of(user) -> str:
    role_name = "VIEWER"
    if hasattr(user, "role_name") and isinstance(user.role_name, str):
        role_name = user.role_name
    elif hasattr(user, "role"):
        if isinstance(user.role, str):
            role_name = user.role
        elif hasattr(user.role, "role_name") and isinstance(user.role.role_name, str):
            role_name = user.role.role_name
    return str(role_name)


def _check_idle_choice(role_name: str, minutes: int) -> None:
    choices = session_policy.idle_choices(role_name)
    if minutes not in choices:
        allowed = ", ".join(str(c) for c in choices)
        raise HTTPException(
            status_code=400,
            detail=f"Session timeout for {role_name} accounts must be one of: {allowed} minutes.",
        )


@router.get("/session-options")
async def session_options(current_user: dict = Depends(require_admin)):
    """The timeout choices and defaults per role, so the Users form offers exactly what the API
    accepts instead of keeping its own copy."""
    return APIResponse.success(data={
        "idle_minutes": {
            "ADMIN": list(session_policy.ADMIN_IDLE_CHOICES),
            "VIEWER": list(session_policy.VIEWER_IDLE_CHOICES),
        },
        "default_idle_minutes": {
            "ADMIN": session_policy.ADMIN_IDLE_DEFAULT,
            "VIEWER": session_policy.VIEWER_IDLE_DEFAULT,
        },
        "admin_absolute_hours": int(session_policy.ADMIN_ABSOLUTE_LIFETIME.total_seconds() // 3600),
        "max_sessions": {
            "min": session_policy.MAX_SESSIONS_MIN,
            "max": session_policy.MAX_SESSIONS_MAX,
            "default": session_policy.default_max_sessions(),
        },
    })


@router.get("/")
async def list_users(
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    auth_repo = AuthRepository(db)
    users = await auth_repo.list_users()

    # Live session counts for the table. Not worth failing the whole list over: without them
    # the column just shows no count.
    try:
        counts = await driver_manager.get_session_store().active_session_counts()
    except Exception:
        logger.exception("Could not count active sessions")
        counts = None

    users_data = []
    for user in users:
        users_data.append({
            "id": str(user.id),
            "username": user.username,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "last_login": user.last_login,
            "created_at": user.created_at,
            "role": _role_name_of(user),
            "session_idle_minutes": getattr(user, "session_idle_minutes", None),
            "max_sessions": getattr(user, "max_sessions", None),
            "active_sessions": counts.get(str(user.id), 0) if isinstance(counts, dict) else None,
        })

    return APIResponse.success(data=users_data)


@router.post("/", status_code=201)
async def create_user(
    request: CreateUserRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    auth_repo = AuthRepository(db)

    # Check if username already exists
    dup_user = await auth_repo.get_user_by_username(request.username)
    if dup_user:
        raise HTTPException(
            status_code=409, detail="Username is already taken."
        )

    # Get role_id
    role_row = await auth_repo.get_role_by_name(request.role)
    if not role_row:
        raise HTTPException(status_code=400, detail="Invalid role specified.")

    idle_minutes = request.session_idle_minutes
    if idle_minutes is None:
        idle_minutes = session_policy.default_idle_minutes(request.role)
    _check_idle_choice(request.role, idle_minutes)
    max_sessions = request.max_sessions or session_policy.default_max_sessions()

    # Set default password if not provided
    generated_pass = None
    if request.password and request.password.strip():
        plain_password = request.password.strip()
    else:
        plain_password = generate_readable_password()
        generated_pass = plain_password

    hashed = await hash_password_async(plain_password)

    new_user = await auth_repo.create_user(
        username=request.username,
        password_hash=hashed,
        role_id=role_row.id,
        session_idle_minutes=idle_minutes,
        max_sessions=max_sessions,
    )

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    new_user_id = getattr(new_user, "id", None)
    if new_user_id:
        await auth_repo.create_audit_log(
            user_id=admin_uuid,
            action="USER:CREATE",
            target_type="users",
            target_id=new_user_id,
            details={
                "username": request.username,
                "role": request.role,
                "session_idle_minutes": idle_minutes,
                "max_sessions": max_sessions,
            },
        )

    await db.commit()

    res_data = {
        "id": str(new_user_id),
        "username": request.username,
        "role": request.role,
        "session_idle_minutes": idle_minutes,
        "max_sessions": max_sessions,
        "message": f"User account '{request.username}' created successfully.",
    }
    if generated_pass:
        res_data["generated_password"] = generated_pass

    return APIResponse.success(data=res_data)


@router.post("/{user_id}/reset-password")
async def reset_password(
    user_id: UUID,
    request: ResetPasswordRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if str(user_id) == str(current_user.get("sub")):
        raise HTTPException(
            status_code=400,
            detail="Use the password change menu to update your own password.",
        )

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    generated_pass = None
    if request.password and request.password.strip():
        plain_password = request.password.strip()
    else:
        plain_password = generate_readable_password()
        generated_pass = plain_password

    hashed = await hash_password_async(plain_password)

    await auth_repo.update_password(user_id, hashed, must_change_password=True)
    await invalidate_all_user_sessions_async(str(user_id), reason=session_policy.PASSWORD_CHANGED)

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    await auth_repo.create_audit_log(
        user_id=admin_uuid,
        action="USER:RESET_PASSWORD",
        target_type="users",
        target_id=user_id,
        details={"username": user.username},
    )

    await db.commit()

    res_data = {
        "message": f"Password for user '{user.username}' reset successfully."
    }
    if generated_pass:
        res_data["generated_password"] = generated_pass

    return APIResponse.success(data=res_data)


@router.patch("/{user_id}")
async def update_user(
    user_id: UUID,
    request: UpdateUserRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Safety check: an admin can't change their own role or status (they could lock themselves
    # out, or leave no admin). Their own session timeout and limit are fine to change.
    if str(user_id) == str(current_user.get("sub")) and (
        request.role is not None or request.is_active is not None
    ):
        raise HTTPException(
            status_code=400,
            detail="Administrative roles cannot modify their own privileges or status.",
        )

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    updates = {}
    audit_details = {}
    role_changed = False
    new_role = _role_name_of(user)

    if request.role is not None:
        role_row = await auth_repo.get_role_by_name(request.role)
        if not role_row:
            raise HTTPException(status_code=400, detail="Invalid role specified.")
        updates["role_id"] = role_row.id
        audit_details["role"] = request.role
        role_changed = str(getattr(user, "role_id", "")) != str(role_row.id)
        new_role = request.role

    if request.is_active is not None:
        updates["is_active"] = request.is_active
        audit_details["is_active"] = request.is_active

    # The timeout is checked against the role the account will have after this request. A role
    # change without a new timeout fits the old one to the new role's choices (a viewer's 4
    # hours becomes an admin's 1 hour), so no account is left with a value its role can't have.
    current_idle = getattr(user, "session_idle_minutes", None)
    if request.session_idle_minutes is not None:
        _check_idle_choice(new_role, request.session_idle_minutes)
        if request.session_idle_minutes != current_idle:
            updates["session_idle_minutes"] = request.session_idle_minutes
            audit_details["session_idle_minutes"] = request.session_idle_minutes
    elif role_changed:
        fitted = session_policy.clamp_idle_minutes(new_role, current_idle)
        if fitted != current_idle:
            updates["session_idle_minutes"] = fitted
            audit_details["session_idle_minutes"] = fitted

    if request.max_sessions is not None and request.max_sessions != getattr(user, "max_sessions", None):
        # Applies from the account's next sign-in: sessions already open are not ended.
        updates["max_sessions"] = request.max_sessions
        audit_details["max_sessions"] = request.max_sessions

    if not updates:
        return APIResponse.success(data={"message": "No updates provided."})

    await auth_repo.update_user(user_id, **updates)

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    await auth_repo.create_audit_log(
        user_id=admin_uuid,
        action="USER:UPDATE",
        target_type="users",
        target_id=user_id,
        details=audit_details,
    )

    # Requests already read the role from the database; signing the account out on a role
    # change also drops its open live-event streams and the browser's cached role.
    if request.is_active is False or role_changed:
        await invalidate_all_user_sessions_async(str(user_id), reason=session_policy.ENDED_BY_ADMIN)

    await db.commit()

    return APIResponse.success(
        data={"message": f"User '{user.username}' updated successfully."}
    )


@router.delete("/{user_id}")
async def delete_user(
    user_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Safety Check: Prevent deleting self
    if str(user_id) == str(current_user.get("sub")):
        raise HTTPException(
            status_code=400,
            detail="Administrators cannot delete or deactivate their own active accounts.",
        )

    auth_repo = AuthRepository(db)
    user = await auth_repo.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    await auth_repo.deactivate_user(user_id)
    await invalidate_all_user_sessions_async(str(user_id), reason=session_policy.ENDED_BY_ADMIN)

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    await auth_repo.create_audit_log(
        user_id=admin_uuid,
        action="USER:DEACTIVATE",
        target_type="users",
        target_id=user_id,
        details={"username": user.username},
    )

    await db.commit()

    return APIResponse.success(
        data={"message": f"User '{user.username}' deactivated successfully."}
    )


def _admin_uuid(current_user: dict) -> Optional[UUID]:
    try:
        return UUID(str(current_user.get("sub")))
    except (ValueError, TypeError):
        return None


@router.get("/{user_id}/sessions")
async def list_user_sessions(
    user_id: UUID,
    current_user: dict = Depends(require_admin),
):
    """The account's live sessions, newest first. The token id (jti) never leaves the server:
    each session is named by its row id, and `is_current` marks the caller's own session."""
    sessions = await driver_manager.get_session_store().list_sessions(str(user_id))
    own_jti = current_user.get("jti")
    return APIResponse.success(data=[
        {
            "id": str(row["id"]),
            "created_at": row["created_at"],
            "last_activity_at": row["last_activity_at"],
            "ends_at": row["ends_at"],
            "client_ip": row.get("client_ip"),
            "user_agent": row.get("user_agent"),
            "is_current": bool(own_jti) and row.get("jti") == own_jti,
        }
        for row in sessions
    ])


@router.delete("/{user_id}/sessions/{session_id}")
async def end_user_session(
    user_id: UUID,
    session_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ends one session. The admin's own current session is refused: Sign Out does that, and
    ending it here would leave the page half-working until the next request fails."""
    store = driver_manager.get_session_store()
    own_jti = current_user.get("jti")
    sessions = await store.list_sessions(str(user_id))
    target = next((r for r in sessions if str(r["id"]) == str(session_id)), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Session not found or already ended.")
    if own_jti and target.get("jti") == own_jti:
        raise HTTPException(status_code=400, detail="Use Sign Out to end your own current session.")

    if not await store.end_session_by_id(str(user_id), str(session_id), session_policy.ENDED_BY_ADMIN):
        raise HTTPException(status_code=404, detail="Session not found or already ended.")

    auth_repo = AuthRepository(db)
    await auth_repo.create_audit_log(
        user_id=_admin_uuid(current_user),
        action="USER:END_SESSION",
        target_type="users",
        target_id=user_id,
        details={"session_id": str(session_id), "client_ip": target.get("client_ip")},
    )
    await db.commit()
    return APIResponse.success(data={"message": "Session ended."})


@router.delete("/{user_id}/sessions")
async def end_all_user_sessions(
    user_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Ends every session of the account, for example a shared login that is still open on
    other screens. On the admin's own account their current session is kept."""
    keep = current_user.get("jti") if str(user_id) == str(current_user.get("sub")) else None
    await invalidate_all_user_sessions_async(
        str(user_id), except_jti=keep, reason=session_policy.ENDED_BY_ADMIN
    )
    auth_repo = AuthRepository(db)
    await auth_repo.create_audit_log(
        user_id=_admin_uuid(current_user),
        action="USER:END_ALL_SESSIONS",
        target_type="users",
        target_id=user_id,
        details={"kept_own_session": bool(keep)},
    )
    await db.commit()
    return APIResponse.success(data={"message": "Sessions ended."})
