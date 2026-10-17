"""Ephemres Network Monitoring Platform — Authentication Schemas.

This module defines request and response validation models for user login,
session creation, and forced password change flows.
"""

from __future__ import annotations

from typing import Optional
from app.core.schema import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """User login credential payload."""

    username: str = Field(..., description="Login username")
    password: str = Field(..., description="Plaintext login password")

    model_config = ConfigDict(from_attributes=True)


class LoginResponse(BaseModel):
    """Authentication response payload containing user profile and security state."""

    username: str
    role: str
    must_change_password: bool
    message: str

    model_config = ConfigDict(from_attributes=True)


class ChangePasswordRequest(BaseModel):
    """Password mutation request payload."""

    old_password: Optional[str] = Field(None, description="Current password (optional during initial setup reset)")
    new_password: str = Field(..., min_length=8, description="New replacement password")

    model_config = ConfigDict(from_attributes=True)
