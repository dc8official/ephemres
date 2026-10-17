"""Ephemres Network Monitoring Platform — User Session Data Model.

This module defines the persistent session model used by the PostgreSQL-native
session driver (PostgresSessionStore) for managing active JWT JTI tokens,
sliding inactivity timeouts, and concurrent device quotas.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, String, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class UserSession(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    active authenticated web sessions in the user_sessions table.
    """

    __tablename__ = "user_sessions"

    # Unique session record identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # Reference to the authenticated user account
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Unique JWT ID (JTI) claim associated with this session token
    jti: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )

    # Session creation timestamp (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Absolute expiration timestamp of this session (evaluated on request)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # Relational link to the associated User account
    user: Mapped["User"] = relationship("User", lazy="joined")  # noqa: F821
