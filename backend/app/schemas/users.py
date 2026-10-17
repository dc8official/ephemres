from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_serializer


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: Optional[str] = None
    role: Literal["ADMIN", "VIEWER"]

    model_config = ConfigDict(from_attributes=True)


class UpdateUserRequest(BaseModel):
    role: Optional[Literal["ADMIN", "VIEWER"]] = None
    is_active: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


class ResetPasswordRequest(BaseModel):
    password: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class UserSummary(BaseModel):
    id: UUID
    username: str
    is_active: bool
    must_change_password: bool
    last_login: Optional[datetime] = None
    created_at: datetime
    role: str

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("last_login")
    def serialize_last_login(self, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()

    @field_serializer("created_at")
    def serialize_created_at(self, v: datetime) -> str:
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        else:
            v = v.astimezone(timezone.utc)
        return v.isoformat()
