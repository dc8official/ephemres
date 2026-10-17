"""Ephemres Network Monitoring Platform — Endpoint Inventory Data Model.

This module defines the primary inventory model for monitored network devices,
including IP configuration, device roles, Layer-2 auto-bypass flags, RCA graph
hierarchies, and passive NetFlow/IPFIX flow exporter metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from app.core.orm import Boolean, DateTime, ForeignKey, String, Text, func
from app.core.orm import ARRAY, INET, JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class Endpoint(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    the primary inventory of monitored hosts, routers, switches, and network
    services.
    """

    __tablename__ = "endpoints"

    # Unique surrogate primary key (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Primary target IP address probed during monitoring sweeps (IPv4 / IPv6)
    ip_address: Mapped[str] = mapped_column(
        INET,
        unique=True,
        nullable=False,
    )

    # Human-readable DNS hostname or system label
    hostname: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Device category classification (e.g. ROUTER, SWITCH, SERVER, GATEWAY)
    device_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    # Physical data center, rack, or geographical location
    location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    # Freeform operator description or asset notes
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    # Master polling toggle for ICMP and synthetic probes
    monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # High-level operational status (ACTIVE, MAINTENANCE, DECOMMISSIONED)
    endpoint_status: Mapped[str] = mapped_column(
        String(20),
        default="ACTIVE",
        nullable=False,
    )

    # Triggers automatic high-fidelity traceroute diagnostic sweep on state drop
    allow_incident_trace: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Permitted to participate in topology route discovery sweeps
    allow_topology_discovery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Enables topological Root Cause Analysis (RCA) cascade inference
    enable_rca: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Participates in recurring automated route baseline discovery
    enable_scheduled_discovery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )

    # Directly connected Layer-2 broadcast domain device; bypasses multi-hop traceroute
    is_l2_segment: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )

    # Secondary exporter IP addresses used by NetFlow/IPFIX for O(1) in-memory resolution
    flow_exporter_ips: Mapped[list[str]] = mapped_column(
        ARRAY(INET),
        default=list,
        server_default="{}",
        nullable=False,
    )

    # Custom operator interface aliases mapped to SNMP ifIndex keys (e.g. {"1": "GigabitEthernet0/0/1"})
    flow_interface_aliases: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default="{}",
        nullable=False,
    )

    # Hierarchical role within topology DAG (ROOT_GATEWAY, AGGREGATION, ACCESS, ACTIVE_HOST)
    device_role: Mapped[str] = mapped_column(
        String(30),
        default="ACTIVE_HOST",
        server_default="ACTIVE_HOST",
        nullable=False,
    )

    # Manual parent node override for topological hierarchy and RCA suppression
    manual_parent_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="SET NULL"),
        nullable=True,
    )

    # User ID of the administrator who enrolled this endpoint
    created_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )

    # Record creation and update timestamps (UTC)
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

    # Soft-deletion timestamp for historical audit retention
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
