"""Ephemres Network Monitoring Platform — Endpoint Diagnostic Trace Model.

This module defines the diagnostic trace model storing on-demand and incident-triggered
traceroute execution outputs, individual hop IP addresses, and per-probe latencies.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, String, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointDiagnosticTrace(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    an ICMP traceroute diagnostic execution record.
    """

    __tablename__ = "endpoint_diagnostic_traces"

    # Unique trace run identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Monitored endpoint targeted by the traceroute
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Execution timestamp (UTC)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Trigger mechanism (ON_DEMAND, INCIDENT_AUTO_TRIGGER, SCHEDULED_SWEEP)
    trigger_reason: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Raw structured traceroute hop output and latencies
    trace_data: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )
