from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from app.core.orm import func, select, update as sa_update
from app.core.orm import AsyncSession
from app.core.orm import joinedload

from app.models.audit_log import AuditLog
from app.models.user import Role, User
from app.repositories.base import BaseRepository


class AuthRepository(BaseRepository[User]):
    """
    Repository for User authentication, credential lookup, and audit logging.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(User, session)

    async def get_user_by_username(self, username: str) -> Optional[User]:
        """Fetch a user by username with eager-loaded role."""
        stmt = (
            select(User)
            .options(joinedload(User.role))
            .where(User.username == username)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_user_by_id(self, user_id: UUID) -> Optional[User]:
        """Fetch a user by UUID primary key with eager-loaded role."""
        stmt = (
            select(User)
            .options(joinedload(User.role))
            .where(User.id == user_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_users(self) -> Sequence[User]:
        """List all users ordered by creation date descending."""
        stmt = (
            select(User)
            .options(joinedload(User.role))
            .order_by(User.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_role_by_name(self, role_name: str) -> Optional[Role]:
        """Fetch a role by role_name."""
        stmt = select(Role).where(Role.role_name == role_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_role_by_id(self, role_id: UUID) -> Optional[Role]:
        """Fetch a role by UUID primary key."""
        stmt = select(Role).where(Role.id == role_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_user(
        self,
        username: str,
        password_hash: str,
        role_id: UUID,
    ) -> User:
        """Create a new user account."""
        user = User(
            username=username,
            password_hash=password_hash,
            role_id=role_id,
            is_active=True,
            must_change_password=True,
        )
        self.session.add(user)
        await self.session.flush()
        return user

    async def update_password(
        self,
        user_id: UUID,
        password_hash: str,
        must_change_password: bool = False,
    ) -> None:
        """Update a user's password hash and change password flag."""
        stmt = (
            sa_update(User)
            .where(User.id == user_id)
            .values(
                password_hash=password_hash,
                must_change_password=must_change_password,
                updated_at=func.now(),
            )
        )
        await self.session.execute(stmt)

    async def update_last_login(
        self,
        user_id: UUID,
        last_login: datetime,
    ) -> None:
        """Update last login timestamp for a user."""
        stmt = (
            sa_update(User)
            .where(User.id == user_id)
            .values(last_login=last_login)
        )
        await self.session.execute(stmt)

    async def update_user(
        self,
        user_id: UUID,
        **updates: Any,
    ) -> Optional[User]:
        """Update user fields."""
        if not updates:
            return await self.get_user_by_id(user_id)
        if "updated_at" not in updates:
            updates["updated_at"] = func.now()
        stmt = (
            sa_update(User)
            .where(User.id == user_id)
            .values(**updates)
            .returning(User)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def deactivate_user(self, user_id: UUID) -> Optional[User]:
        """Soft delete / deactivate a user account."""
        stmt = (
            sa_update(User)
            .where(User.id == user_id)
            .values(
                is_active=False,
                updated_at=func.now(),
            )
        )
        await self.session.execute(stmt)
        return await self.get_user_by_id(user_id)

    async def create_audit_log(
        self,
        user_id: Optional[UUID],
        action: str,
        target_type: str,
        target_id: UUID,
        details: Optional[dict[str, Any] | list[Any]] = None,
    ) -> AuditLog:
        """Record an immutable audit log entry."""
        log_entry = AuditLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        self.session.add(log_entry)
        await self.session.flush()
        return log_entry
