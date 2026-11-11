from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_serializer


# Session fields: the bounds here are only the outer limits. Which idle timeouts a role may
# use (admin 15/30/60, viewer 30/60/120/240 minutes) is checked in routers/users.py against
# services/session_policy.py, because it depends on the role in the same request.
class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: Optional[str] = None
    role: Literal["ADMIN", "VIEWER"]
    # Omitted: the role's default (admin 30, viewer 120 minutes).
    session_idle_minutes: Optional[int] = Field(default=None, ge=15, le=240)
    # Omitted: security.max_active_sessions_per_user from the config file.
    max_sessions: Optional[int] = Field(default=None, ge=1, le=10)

    model_config = ConfigDict(from_attributes=True)


class UpdateUserRequest(BaseModel):
    role: Optional[Literal["ADMIN", "VIEWER"]] = None
    is_active: Optional[bool] = None
    session_idle_minutes: Optional[int] = Field(default=None, ge=15, le=240)
    max_sessions: Optional[int] = Field(default=None, ge=1, le=10)

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
    session_idle_minutes: int = 30
    max_sessions: int = 2

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
