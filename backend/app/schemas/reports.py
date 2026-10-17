from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, field_validator, field_serializer


class UptimeReport(BaseModel):
    endpoint_id: UUID
    period_start: datetime
    period_end: datetime
    total_seconds: int
    uptime_seconds: int
    downtime_seconds: int
    unknown_seconds: int
    uptime_percentage: float
    incident_count: int

    model_config = ConfigDict(from_attributes=True)

    @field_validator("uptime_percentage")
    @classmethod
    def validate_uptime_percentage(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("uptime_percentage must be between 0.0 and 100.0 inclusive")
        return v

    @field_serializer("period_start", "period_end")
    def serialize_period(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()


class IncidentRecord(BaseModel):
    endpoint_id: UUID
    incident_start: datetime
    incident_end: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    peak_detailed_state: Literal[
        "UP", "UP-UNSTABLE", "DOWN-UNSTABLE", "DOWN"
    ]
    contributing_event_count: int

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("incident_start")
    def serialize_incident_start(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()

    @field_serializer("incident_end")
    def serialize_incident_end(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()


class FleetEndpointSummary(BaseModel):
    id: UUID
    hostname: str
    ip_address: str
    device_type: str
    operational_state: str
    detailed_state: str
    monitoring_enabled: bool
    uptime_percentage: float
    incident_count: int
    uptime_seconds: int
    downtime_seconds: int
    total_seconds: int
    device_role: Optional[str] = "ACTIVE_HOST"

    model_config = ConfigDict(from_attributes=True)


class FleetSummaryResponse(BaseModel):
    fleet_sla: float
    active_endpoints_count: int
    total_endpoints_count: int
    total_incident_count: int
    total_downtime_seconds: int
    endpoints: list[FleetEndpointSummary]

    model_config = ConfigDict(from_attributes=True)

