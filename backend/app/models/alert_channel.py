"""Ephemres Network Monitoring Platform — Alert Channel Configuration Model.

This module defines the notification channel model for outbound incident alerts
dispatched to external collaboration platforms (Microsoft Teams, Discord, Slack,
corporate SMTP relays, and generic webhooks) with AES-256-GCM encrypted secrets.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from app.core.orm import Boolean, DateTime, String, Text, func, text
from app.core.orm import JSONB
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertChannel(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    an outbound notification target configuration.
    """

    __tablename__ = "alert_channels"

    # Unique channel identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Human-readable channel display name
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Channel integration type (TEAMS, DISCORD, SLACK, EMAIL, WEBHOOK)
    channel_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Active dispatch toggle
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
        index=True,
    )

    # Serialized JSON configuration encrypted with AES-256-GCM (webhook URLs, SMTP passwords)
    config: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Target endpoint UUID filtering (empty list indicates all fleet endpoints)
    endpoint_ids: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )

    # Subnet CIDR filtering (e.g. ["10.0.0.0/24", "192.168.1.0/24"])
    subnet_filters: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )

    # Severity states triggering alert dispatch (e.g. ["DOWN", "DEGRADED", "RECOVERED"])
    severity_filters: Mapped[list] = mapped_column(
        JSONB,
        default=lambda: ["DOWN", "RECOVERED"],
        server_default=text("'[\"DOWN\", \"RECOVERED\"]'::jsonb"),
        nullable=False,
    )

    # Creation and update timestamps (UTC)
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

    # Relational link to historical delivery audit logs
    delivery_logs: Mapped[List["AlertDeliveryLog"]] = relationship(  # noqa: F821
        "AlertDeliveryLog",
        back_populates="channel",
        cascade="all, delete-orphan",
    )
