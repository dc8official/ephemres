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
from app.services.driver_manager import driver_manager
from app.services.auth_service import (
    generate_readable_password,
    hash_password,
    hash_password_async,
    invalidate_all_user_sessions,
    invalidate_all_user_sessions_async,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/")
async def list_users(
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    auth_repo = AuthRepository(db)
    users = await auth_repo.list_users()

    users_data = []
    for user in users:
        role_name = "VIEWER"
        if hasattr(user, "role_name") and isinstance(user.role_name, str):
            role_name = user.role_name
        elif hasattr(user, "role"):
            if isinstance(user.role, str):
                role_name = user.role
            elif hasattr(user.role, "role_name") and isinstance(user.role.role_name, str):
                role_name = user.role.role_name
        users_data.append({
            "id": str(user.id),
            "username": user.username,
            "is_active": user.is_active,
            "must_change_password": user.must_change_password,
            "last_login": user.last_login,
            "created_at": user.created_at,
            "role": str(role_name),
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
            },
        )

    await db.commit()

    res_data = {
        "id": str(new_user_id),
        "username": request.username,
        "role": request.role,
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
    await invalidate_all_user_sessions_async(str(user_id))

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
    # Safety Check: Prevent modifying self
    if str(user_id) == str(current_user.get("sub")):
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

    if request.role is not None:
        role_row = await auth_repo.get_role_by_name(request.role)
        if not role_row:
            raise HTTPException(status_code=400, detail="Invalid role specified.")
        updates["role_id"] = role_row.id
        audit_details["role"] = request.role

    if request.is_active is not None:
        updates["is_active"] = request.is_active
        audit_details["is_active"] = request.is_active

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

    if request.is_active is False:
        await invalidate_all_user_sessions_async(str(user_id))

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
    await invalidate_all_user_sessions_async(str(user_id))

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
