"""Starlette status drop-in compatibility shim for Ephemres."""

from app.core.asgi import status

__all__ = ["status"]
