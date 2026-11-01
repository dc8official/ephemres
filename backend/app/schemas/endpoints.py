"""Ephemres Network Monitoring Platform — Endpoint API Schemas.

This module defines request and response validation models for endpoint
management, fleet summaries, detailed diagnostic configurations, interface aliases,
and operational status serialization.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_serializer, field_validator
from app.schemas.bandwidth import normalize_interface_aliases


class InterfaceConfig(BaseModel):
    """Network router interface configuration attributes."""

    name: str = Field(..., max_length=64, pattern=r"^[a-zA-Z0-9_\-\./ :]{1,64}$", description="Interface name label")
    # None leaves the speed unset: utilization is then not shown rather than measured against a guess.
    speed_mbps: Optional[int] = Field(default=None, gt=0, le=10000000, description="Interface link capacity speed in Mbps")
    # "wan": link to the outside (internet or provider uplink); fleet totals count only these.
    role: Literal["wan", "lan"] = Field(default="lan", description="Interface role")

    model_config = ConfigDict(from_attributes=True)


class EndpointSummary(BaseModel):
    """Compact summary schema for fleet table listing and status cards."""

    id: UUID
    hostname: str
    ip_address: str
    device_type: str
    location: Optional[str] = None
    endpoint_status: Literal["ACTIVE", "DISABLED", "DELETED"]
    current_operational_state: Literal["UP", "DOWN", "PASSIVE", "UNMONITORED"]
    current_detailed_state: Literal[
        "UP", "UP-UNSTABLE", "DOWN-UNSTABLE", "DOWN", "PASSIVE", "UNMONITORED"
    ]
    current_health_score: float
    uptime_percentage_24h: float
    device_role: Optional[str] = "ACTIVE_HOST"
    last_seen: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("last_seen")
    def serialize_last_seen(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()


class EndpointDetail(EndpointSummary):
    """Detailed endpoint model including configuration toggles, flow aliases, and audit timestamps."""

    description: Optional[str] = None
    monitoring_enabled: bool
    allow_incident_trace: bool = True
    allow_topology_discovery: bool = True
    enable_rca: bool = True
    enable_scheduled_discovery: bool = True
    is_l2_segment: bool = False
    flow_exporter_ips: list[str] = []
    flow_interface_aliases: dict[str, InterfaceConfig] = Field(default_factory=dict)
    manual_parent_id: Optional[UUID] = None
    created_by: Optional[UUID] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("flow_interface_aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, v):
        return normalize_interface_aliases(v)

    @field_serializer("created_at", "updated_at")
    def serialize_created_updated(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()
