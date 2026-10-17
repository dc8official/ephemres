"""Ephemres Network Monitoring Platform — Endpoint Event TimescaleDB Hypertable Model.

This module defines the high-throughput time-series event model partitioned as a
TimescaleDB hypertable over start_time. It records operational state transitions,
packet loss rates, round-trip times (RTT), and SLA downtime intervals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointEvent(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the endpoint_events TimescaleDB hypertable for time-series state
    transitions.
    """

    __tablename__ = "endpoint_events"
    __table_args__ = (
        Index("idx_endpoint_events_start_time_id", "start_time", "id"),
    )

    # Unique event surrogate ID (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        default=uuid4,
        server_default=func.gen_random_uuid(),
        nullable=False,
    )

    # Reference to the monitored endpoint
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id"),
        nullable=False,
    )

    # Coarse operational state (UP, DEGRADED, DOWN, INFERRED_DOWN)
    operational_state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    # Granular diagnostic state (HEALTHY, HIGH_LATENCY, PACKET_LOSS, OFFLINE)
    detailed_state: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )

    # Number of successful ICMP/synthetic probe responses in cycle
    success_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )

    # Number of dropped or timed-out probe packets in cycle
    failed_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )

    # Calculated composite health percentage (0.00% – 100.00%)
    health_score: Mapped[float] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )

    # Average ICMP round-trip latency in milliseconds (null if 100% loss)
    avg_rtt_ms: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )

    # Flag indicating whether this record was split across midnight/month boundary for SLA accounting
    is_split_event: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    # Hypertable time-partitioning primary key column (UTC timestamp)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )

    # End timestamp of this continuous state interval (null while state remains active)
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Total duration of state in seconds (end_time - start_time)
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )

    # Number of consecutive monitoring cycles this state persisted
    monitoring_cycle_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )

    # Record insertion timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
