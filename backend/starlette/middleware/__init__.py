"""Starlette middleware drop-in compatibility shim for Ephemres."""

from app.core.asgi import BaseHTTPMiddleware, CORSMiddleware

__all__ = ["BaseHTTPMiddleware", "CORSMiddleware"]
