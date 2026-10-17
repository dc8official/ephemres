"""Starlette CORS middleware drop-in compatibility shim for Ephemres."""

from app.core.asgi import CORSMiddleware

__all__ = ["CORSMiddleware"]
