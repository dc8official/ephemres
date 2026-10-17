"""In-tree zero-dependency SQLAlchemy dialects compatibility shim."""

from __future__ import annotations

from . import postgresql as postgresql

__all__ = ["postgresql"]
