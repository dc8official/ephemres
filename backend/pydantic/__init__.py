"""Pydantic v2 drop-in compatibility shim for Ephemres."""

from app.core.schema import (
    BaseModel,
    ConfigDict,
    Field,
    _FieldInfo as FieldInfo,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
    to_camel,
)

__all__ = [
    "BaseModel",
    "ConfigDict",
    "Field",
    "FieldInfo",
    "ValidationError",
    "field_serializer",
    "field_validator",
    "model_validator",
    "to_camel",
]
