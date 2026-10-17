from __future__ import annotations

from typing import Optional
from app.core.schema import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    username: str
    password: str

    model_config = ConfigDict(from_attributes=True)


class LoginResponse(BaseModel):
    username: str
    role: str
    must_change_password: bool
    message: str

    model_config = ConfigDict(from_attributes=True)


class ChangePasswordRequest(BaseModel):
    old_password: Optional[str] = None
    new_password: str

    model_config = ConfigDict(from_attributes=True)
