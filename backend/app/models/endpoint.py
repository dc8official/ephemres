from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from app.core.orm import Boolean, DateTime, ForeignKey, String, Text, func
from app.core.orm import ARRAY, INET, JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class Endpoint(Base):
    """
    SQLAlchemy 2.0 model for the endpoints table.
    """

    __tablename__ = "endpoints"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )
    ip_address: Mapped[str] = mapped_column(
        INET,
        unique=True,
        nullable=False,
    )
    hostname: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    device_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    monitoring_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    endpoint_status: Mapped[str] = mapped_column(
        String(20),
        default="ACTIVE",
        nullable=False,
    )
    allow_incident_trace: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    allow_topology_discovery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    enable_rca: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    enable_scheduled_discovery: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    is_l2_segment: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
    )
    flow_exporter_ips: Mapped[list[str]] = mapped_column(
        ARRAY(INET),
        default=list,
        server_default="{}",
        nullable=False,
    )
    flow_interface_aliases: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        default=dict,
        server_default="{}",
        nullable=False,
    )
    device_role: Mapped[str] = mapped_column(
        String(30),
        default="ACTIVE_HOST",
        server_default="ACTIVE_HOST",
        nullable=False,
    )
    manual_parent_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
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
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
