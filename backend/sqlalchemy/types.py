"""In-tree zero-dependency SQLAlchemy types compatibility shim."""

from __future__ import annotations

from app.core.orm import (
    ARRAY,
    INET,
    JSONB,
    NULLTYPE,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    NullType,
    Numeric,
    SmallInteger,
    SQLType,
    String,
    Text,
    TEXT,
    VARCHAR,
)
from app.core.orm import UUIDType as UUID

__all__ = [
    "NULLTYPE",
    "NullType",
    "SQLType",
    "BigInteger",
    "Boolean",
    "DateTime",
    "Float",
    "Integer",
    "Numeric",
    "SmallInteger",
    "String",
    "VARCHAR",
    "Text",
    "TEXT",
    "UUID",
    "INET",
    "JSONB",
    "ARRAY",
]
