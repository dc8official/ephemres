from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, field_validator, field_serializer


class EventRecord(BaseModel):
    id: UUID
    endpoint_id: UUID
    operational_state: Literal["UP", "DOWN"]
    detailed_state: Literal[
        "UP", "UP-UNSTABLE", "DOWN-UNSTABLE", "DOWN"
    ]
    health_score: float
    avg_rtt_ms: Optional[float] = None
    is_split_event: bool
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    monitoring_cycle_count: int

    model_config = ConfigDict(from_attributes=True)

    @field_validator("health_score")
    @classmethod
    def validate_health_score(cls, v: float) -> float:
        if not (0.0 <= v <= 100.0):
            raise ValueError("health_score must be between 0.0 and 100.0 inclusive")
        return v

    @field_serializer("start_time")
    def serialize_start_time(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()

    @field_serializer("end_time")
    def serialize_end_time(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()
