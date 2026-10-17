"""In-tree zero-dependency SQLAlchemy exceptions compatibility shim."""

from __future__ import annotations

from app.core.orm import (
    DBAPIError,
    IntegrityError,
    MultipleResultsFound,
    NoResultFound,
    OperationalError,
    SQLAlchemyError,
)

__all__ = [
    "SQLAlchemyError",
    "DBAPIError",
    "OperationalError",
    "IntegrityError",
    "NoResultFound",
    "MultipleResultsFound",
]
