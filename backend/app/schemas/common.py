"""Ephemres Network Monitoring Platform — Common API Envelope Schemas.

This module defines standard API response envelopes, error structures, and
pagination metadata contracts inherited across all REST endpoints.
"""

from __future__ import annotations

from typing import Generic, Literal, Optional, TypeVar
from app.core.schema import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    """Standardized error payload details."""

    code: str
    message: str

    model_config = ConfigDict(from_attributes=True)


class PaginationMeta(BaseModel):
    """Standardized keyset and offset pagination metadata."""

    total: int = Field(..., ge=0, description="Total number of matching records in collection")
    page: int = Field(..., ge=1, description="Current page index (1-based)")
    page_size: int = Field(..., ge=1, description="Number of records per page")
    total_pages: int = Field(..., ge=0, description="Total number of available pages")

    model_config = ConfigDict(from_attributes=True)


class APIResponse(BaseModel, Generic[T]):
    """Unified API response envelope wrapping data payloads, pagination metadata,

    and error details.
    """

    status: Literal["success", "error"]
    data: Optional[T] = None
    meta: Optional[PaginationMeta] = None
    error: Optional[ErrorDetail] = None

    model_config = ConfigDict(from_attributes=True)

    @property
    def success(self) -> bool:
        """Helper property indicating whether response is successful."""
        return self.status == "success"

    @classmethod
    def success_response(cls, data: T, meta: Optional[PaginationMeta] = None):
        """Construct a successful API response envelope."""
        return cls(status="success", data=data, meta=meta)

    @classmethod
    def success(cls, data: T, meta: Optional[PaginationMeta] = None):
        """Alias for success_response."""
        return cls(status="success", data=data, meta=meta)

    @classmethod
    def error(cls, code: str, message: str):
        """Construct an error API response envelope."""
        return cls(
            status="error",
            error=ErrorDetail(code=code, message=message)
        )
