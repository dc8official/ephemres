"""In-tree zero-dependency SQLAlchemy ORM compatibility shim."""

from __future__ import annotations

from app.core.orm import (
    DeclarativeBase,
    Mapped,
    RelationshipProperty,
    Session,
    joinedload,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)

__all__ = [
    "DeclarativeBase",
    "Mapped",
    "mapped_column",
    "relationship",
    "RelationshipProperty",
    "joinedload",
    "selectinload",
    "sessionmaker",
    "Session",
]
