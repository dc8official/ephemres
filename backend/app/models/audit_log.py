"""Ephemres Network Monitoring Platform — Operator Audit Log Model.

This module defines the persistent security audit logging model for tracking
administrative mutations, user role adjustments, endpoint provisioning, and
system setting alterations with JSON change payload details.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, String, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class AuditLog(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the audit_logs table for administrative accountability.
    """

    __tablename__ = "audit_logs"

    # Unique audit record identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # User ID of the actor initiating the administrative action (null for system automated tasks)
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )

    # Action verb (e.g. USER_CREATE, ENDPOINT_DELETE, SETTING_UPDATE)
    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Type of entity acted upon (USER, ENDPOINT, ALERT_CHANNEL, SETTING)
    target_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Identifier of the affected entity
    target_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )

    # Before/after change diff, metadata attributes, or contextual detail
    details: Mapped[Optional[dict | list]] = mapped_column(
        JSONB,
        nullable=True,
    )

    # Timestamp when the action was recorded (UTC)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
