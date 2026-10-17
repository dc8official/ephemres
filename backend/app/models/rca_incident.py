"""Ephemres Network Monitoring Platform — Root Cause Analysis (RCA) Incident Model.

This module defines the RCA incident snapshot model, storing root cause failure
hop identifications, last known good upstream hops, and baseline delta summaries
used to isolate root infrastructure failures and suppress downstream alert storms.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from app.core.orm import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointRCAIncident(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    a Root Cause Analysis (RCA) diagnostic incident record.
    """

    __tablename__ = "endpoint_rca_incidents"

    # Unique incident record identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )

    # Monitored endpoint experiencing the failure condition
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    # Incident detection timestamp (UTC)
    incident_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Endpoint state when RCA was triggered (DOWN, DEGRADED)
    status_at_execution: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    # Hop sequence index where packet loss was first identified
    failed_hop_number: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    # IP address of the first failing network router
    failed_hop_ip: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )

    # IP address of the last responsive upstream network router
    last_known_good_hop_ip: Mapped[Optional[str]] = mapped_column(
        String(45),
        nullable=True,
    )

    # Natural language summary of the diagnostic finding
    rca_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Snapshot of the baseline route hops at the time of the incident
    baseline_snapshot: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )

    # Snapshot of the active incident traceroute hop trace
    failure_trace_snapshot: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )

    # Flag indicating whether this incident has been resolved
    is_resolved: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        nullable=False,
    )
