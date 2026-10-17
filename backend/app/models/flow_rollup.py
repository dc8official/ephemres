from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from app.core.orm import BigInteger, DateTime, Integer, SmallInteger, func
from app.core.orm import INET, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class FlowMinuteRollup(Base):
    """
    SQLAlchemy 2.0 declarative model for flow_minute_rollups hypertable.
    Retained for 7 days, compressed after 1 day.
    """

    __tablename__ = "flow_minute_rollups"

    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=func.text("'00000000-0000-0000-0000-000000000000'::uuid"),
    )
    src_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    dst_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    src_ip: Mapped[str] = mapped_column(
        INET,
        primary_key=True,
        nullable=False,
    )
    dst_ip: Mapped[str] = mapped_column(
        INET,
        primary_key=True,
        nullable=False,
    )
    protocol: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )
    dst_port: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )
    bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    packets: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    flow_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
        nullable=False,
    )


class FlowHourlyRollup(Base):
    """
    SQLAlchemy declarative model representing flow_hourly_rollups continuous aggregate.
    Retained for 30 days.
    """

    __tablename__ = "flow_hourly_rollups"

    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    src_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    dst_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    protocol: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )
    dst_port: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )
    bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    packets: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    flow_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class FlowDailyRollup(Base):
    """
    SQLAlchemy declarative model representing flow_daily_rollups continuous aggregate.
    Retained for 365 days (1 Year).
    """

    __tablename__ = "flow_daily_rollups"

    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    protocol: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )
    dst_port: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )
    bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    packets: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    flow_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )


class FlowInterfaceMinuteRollup(Base):
    """
    SQLAlchemy declarative model representing flow_interface_minute_rollups hypertable.
    Retained for 7 days.
    """

    __tablename__ = "flow_interface_minute_rollups"

    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )
    interface_idx: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )
    in_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    out_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    in_packets: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    out_packets: Mapped[int] = mapped_column(
        BigInteger,
        default=0,
        server_default="0",
        nullable=False,
    )
    flow_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
