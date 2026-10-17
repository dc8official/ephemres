from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, SmallInteger, String, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointEvent(Base):
    """
    SQLAlchemy 2.0 model for the endpoint_events TimescaleDB hypertable.
    """

    __tablename__ = "endpoint_events"
    __table_args__ = (
        Index("idx_endpoint_events_start_time_id", "start_time", "id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        default=uuid4,
        server_default=func.gen_random_uuid(),
        nullable=False,
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id"),
        nullable=False,
    )
    operational_state: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    detailed_state: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
    )
    success_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )
    failed_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
    )
    health_score: Mapped[float] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
    avg_rtt_ms: Mapped[Optional[float]] = mapped_column(
        Numeric(10, 2),
        nullable=True,
    )
    is_split_event: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )
    end_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )
    monitoring_cycle_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
