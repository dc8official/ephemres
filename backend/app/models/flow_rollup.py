"""Ephemres Network Monitoring Platform — Network Flow Telemetry Rollup Models.

This module defines the 4-tier hierarchical storage lifecycle models for
NetFlow v5/v9 and IPFIX telemetry, including minute rollups, continuous aggregates
(hourly and daily), and directional interface bandwidth accounting.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from app.core.orm import BigInteger, DateTime, Integer, SmallInteger, func
from app.core.orm import INET, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class FlowMinuteRollup(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the flow_minute_rollups TimescaleDB hypertable (Tier 2 storage).

    Raw flows from FastStore streams are aggregated into 1-minute buckets,
    compressed via TimescaleDB columnar compression after 1 day, and dropped
    after 7 days.
    """

    __tablename__ = "flow_minute_rollups"

    # 1-minute time bucket boundary (UTC)
    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )

    # Exporter router endpoint UUID
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        server_default=func.text("'00000000-0000-0000-0000-000000000000'::uuid"),
    )

    # Monitored inventory endpoint ID if src_ip matches known host
    src_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    # Monitored inventory endpoint ID if dst_ip matches known host
    dst_endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )

    # Source and Destination IP addresses
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

    # IP Protocol number (e.g. 6=TCP, 17=UDP, 1=ICMP)
    protocol: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        nullable=False,
    )

    # Destination service port (normalized ephemeral client ports bucketed into 0)
    dst_port: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )

    # Total accumulated bytes, packets, and distinct flow records in bucket
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
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the flow_hourly_rollups TimescaleDB continuous aggregate (Tier 3 storage).

    Refreshed hourly and retained for 30 days for medium-term capacity
    analysis.
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
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the flow_daily_rollups TimescaleDB continuous aggregate (Tier 4 storage).

    Refreshed daily and retained for 365 days for long-term capacity planning.
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
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the flow_interface_minute_rollups TimescaleDB hypertable.

    Records directional per-interface ingress/egress bytes and packets indexed
    by SNMP ifIndex (interface_idx), retained for 7 days.
    """

    __tablename__ = "flow_interface_minute_rollups"

    # 1-minute time bucket boundary (UTC)
    bucket: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
    )

    # Exporter router endpoint UUID
    exporter_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )

    # SNMP interface index (ifIndex)
    interface_idx: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        nullable=False,
    )

    # Directional ingress metrics
    in_bytes: Mapped[int] = mapped_column(
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

    # Directional egress metrics
    out_bytes: Mapped[int] = mapped_column(
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

    # Distinct flow records evaluated
    flow_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
        nullable=False,
    )
