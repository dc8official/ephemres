"""In-tree zero-dependency PostgreSQL dialect compatibility shim."""

from __future__ import annotations

from app.core.orm import (
    ARRAY,
    INET,
    JSONB,
    Insert,
    PostgreSQLDDLCompiler,
    PostgreSQLDialect,
    insert,
    postgresql_dialect,
)
from app.core.orm import UUIDType as UUID


def dialect() -> PostgreSQLDialect:
    return postgresql_dialect


__all__ = [
    "UUID",
    "INET",
    "JSONB",
    "ARRAY",
    "insert",
    "Insert",
    "dialect",
    "PostgreSQLDialect",
    "PostgreSQLDDLCompiler",
]
