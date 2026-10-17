"""Ephemres Network Monitoring Platform — Alert Delivery Log Model.

This module defines the delivery audit log model recording asynchronous dispatch
results, round-trip HTTP latencies, HTTP status codes, retry counts, and suppression
reason codes for incident alerts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, Integer, String, Text, func, text
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertDeliveryLog(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the delivery audit trail for outbound alert notifications.
    """

    __tablename__ = "alert_delivery_logs"

    # Unique delivery log identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Reference to destination alert channel
    channel_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("alert_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Snapshot of channel display name at time of dispatch
    channel_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Optional endpoint ID generating the alert
    endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )

    # Snapshot of endpoint name at time of dispatch
    endpoint_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Monitoring event type (STATE_TRANSITION, RCA_INCIDENT, TEST)
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Delivery outcome status (SUCCESS, FAILED, SUPPRESSED)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Outbound HTTP status code received (null for SMTP/internal suppression)
    status_code: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    # Number of dispatch retry attempts executed
    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )

    # Upstream server response or internal error details
    response_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Network round-trip transmission latency in milliseconds
    latency_ms: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    # Dispatch timestamp (UTC)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # Relational link to parent alert channel
    channel: Mapped["AlertChannel"] = relationship(  # noqa: F821
        "AlertChannel",
        back_populates="delivery_logs",
    )
