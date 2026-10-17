from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from app.core.orm import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointRCAIncident(Base):
    """
    Stores historical incident snapshots for Root Cause Analysis (RCA).
    """

    __tablename__ = "endpoint_rca_incidents"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    incident_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    status_at_execution: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    failed_hop_number: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    failed_hop_ip: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )
    last_known_good_hop_ip: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )
    rca_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    baseline_snapshot: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )
    failure_trace_snapshot: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )
    is_resolved: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
