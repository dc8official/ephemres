"""Ephemres Network Monitoring Platform — Endpoint Baseline Route Model.

This module defines the topology route baseline model storing hop-by-hop ICMP
paths, total hop counts, and parent node derivations used by the Sugiyama &
Gansner crossing-free visualizer and Root Cause Analysis inference engine.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from app.core.orm import Boolean, DateTime, ForeignKey, Integer, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointBaselineRoute(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the single latest verified online route baseline per endpoint.
    """

    __tablename__ = "endpoint_baseline_routes"

    # Monitored endpoint UUID (1:1 relationship acting as primary key)
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # Timestamp of the most recent successful route baseline discovery (UTC)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Total network hop count from monitoring poller to target endpoint
    total_hops: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    # Ordered list of hop objects: [{"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.2}, ...]
    hops: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
    )

    # v3.3.6 (migration 0024): `hops` above is the CURRENT path. The columns below hold the
    # NORMAL path and the hold-down state; services/route_paths.py owns all of them.
    current_since: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    normal_hops: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    normal_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    normal_pinned: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False, nullable=False)
    pending_hops: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    pending_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    flapping_since: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_logged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
