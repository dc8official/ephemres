"""Starlette requests drop-in compatibility shim for Ephemres."""

from app.core.asgi import Request

__all__ = ["Request"]
