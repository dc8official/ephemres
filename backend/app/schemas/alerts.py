from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

import ipaddress
from app.core.schema import BaseModel, ConfigDict, Field, field_validator

ChannelType = Literal["TEAMS", "DISCORD", "SLACK", "EMAIL_SMTP", "GENERIC_WEBHOOK"]


def _validate_subnets(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return None
    validated = []
    for s in v:
        if not isinstance(s, str):
            raise ValueError(f"Subnet filter must be a string, got {type(s).__name__}")
        s_clean = s.strip()
        if not s_clean:
            continue
        try:
            ipaddress.ip_network(s_clean, strict=False)
            validated.append(s_clean)
        except ValueError as err:
            raise ValueError(
                f"Invalid CIDR subnet format: '{s}'. Expected notation like 10.0.0.0/16 or 192.168.1.0/24."
            ) from err
    return validated


class AlertChannelBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    channel_type: ChannelType
    is_enabled: bool = True
    config: Dict[str, Any]
    endpoint_ids: List[UUID] = Field(default_factory=list)
    subnet_filters: List[str] = Field(default_factory=list)
    severity_filters: List[str] = Field(default_factory=lambda: ["DOWN", "RECOVERED"])

    model_config = ConfigDict(from_attributes=True)

    @field_validator("subnet_filters")
    @classmethod
    def validate_subnets(cls, v: List[str]) -> List[str]:
        return _validate_subnets(v) or []


class AlertChannelCreate(AlertChannelBase):
    pass


class AlertChannelUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    channel_type: Optional[ChannelType] = None
    is_enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
    endpoint_ids: Optional[List[UUID]] = None
    subnet_filters: Optional[List[str]] = None
    severity_filters: Optional[List[str]] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("subnet_filters")
    @classmethod
    def validate_subnets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_subnets(v)


class AlertChannelResponse(BaseModel):
    id: UUID
    name: str
    channel_type: str
    is_enabled: bool
    config: Dict[str, Any]
    endpoint_ids: List[UUID]
    subnet_filters: List[str]
    severity_filters: List[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertTestRequest(BaseModel):
    channel_id: Optional[UUID] = None
    channel_type: Optional[ChannelType] = None
    config: Optional[Dict[str, Any]] = None
    name: Optional[str] = None


class AlertDeliveryLogResponse(BaseModel):
    id: UUID
    channel_id: UUID
    channel_name: str
    endpoint_id: Optional[UUID] = None
    endpoint_name: str
    event_type: str
    status: str
    status_code: Optional[int] = None
    retry_count: int = 0
    response_message: Optional[str] = None
    delivered_at: datetime

    model_config = ConfigDict(from_attributes=True)
