"""Alembic compatibility package for Ephemres.

Provides drop-in replacements for alembic.op and alembic.context delegating to
the zero-dependency native migration engine (app.core.migrator).
"""

from . import context, op

__all__ = ["op", "context"]
__version__ = "1.13.0"
