"""Starlette BaseHTTPMiddleware drop-in compatibility shim for Ephemres."""

from app.core.asgi import BaseHTTPMiddleware

__all__ = ["BaseHTTPMiddleware"]
