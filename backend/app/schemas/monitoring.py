from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from app.core.schema import BaseModel, ConfigDict, model_validator, field_serializer


class MonitoringStatus(BaseModel):
    service_status: Literal["RUNNING", "DEGRADED", "UNAVAILABLE"]
    monitored_endpoint_count: int
    last_cycle_completed_at: Optional[datetime] = None
    active_gap: bool
    gap_start_time: Optional[datetime] = None
    app_version: str

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_gap_start_time(self) -> "MonitoringStatus":
        if self.active_gap and self.gap_start_time is None:
            raise ValueError("gap_start_time is required when active_gap is True")
        return self

    @field_serializer("last_cycle_completed_at")
    def serialize_last_cycle(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()

    @field_serializer("gap_start_time")
    def serialize_gap_start_time(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()
