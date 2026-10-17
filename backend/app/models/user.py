"""Ephemres Network Monitoring Platform — User & Role RBAC Models.

This module defines the authentication and role-based access control (RBAC)
models for platform operators and administrators, including Scrypt password
derivation, forced password change requirements, and session relationships.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import Boolean, DateTime, ForeignKey, String, Text, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    system authorization roles (ADMIN, OPERATOR).
    """

    __tablename__ = "roles"

    # Unique role identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Unique uppercase role name ('ADMIN', 'OPERATOR')
    role_name: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        nullable=False,
    )

    # Functional description of role privileges
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Role creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class User(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    authenticated user accounts in the users table.
    """

    __tablename__ = "users"

    # Unique user identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Unique login username
    username: Mapped[str] = mapped_column(
        String(50),
        unique=True,
        nullable=False,
    )

    # Cryptographically secure password hash (Scrypt with constant-time verification)
    password_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Foreign key link to user's assigned role
    role_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("roles.id"),
        nullable=True,
    )

    # Account activation status
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Requires user to set a new password on next login (forced initial reset)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Timestamp of the most recent successful login
    last_login: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Account creation and update timestamps (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationship to user role (eager joined load)
    role: Mapped[Optional[Role]] = relationship("Role", lazy="joined")
