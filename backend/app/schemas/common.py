from __future__ import annotations

from typing import Generic, Literal, Optional, TypeVar
from app.core.schema import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str

    model_config = ConfigDict(from_attributes=True)


class PaginationMeta(BaseModel):
    total: int = Field(..., ge=0)
    page: int = Field(..., ge=1)
    page_size: int = Field(..., ge=1)
    total_pages: int = Field(..., ge=0)

    model_config = ConfigDict(from_attributes=True)


class APIResponse(BaseModel, Generic[T]):
    status: Literal["success", "error"]
    data: Optional[T] = None
    meta: Optional[PaginationMeta] = None
    error: Optional[ErrorDetail] = None

    model_config = ConfigDict(from_attributes=True)

    @property
    def success(self) -> bool:
        return self.status == "success"

    @classmethod
    def success_response(cls, data: T, meta: Optional[PaginationMeta] = None):
        return cls(status="success", data=data, meta=meta)

    @classmethod
    def success(cls, data: T, meta: Optional[PaginationMeta] = None):
        return cls(status="success", data=data, meta=meta)

    @classmethod
    def error(cls, code: str, message: str):
        return cls(
            status="error",
            error=ErrorDetail(code=code, message=message)
        )
