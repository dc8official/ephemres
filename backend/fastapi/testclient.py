"""FastAPI TestClient drop-in compatibility shim for Ephemres."""

from app.core.asgi import TestClient

__all__ = ["TestClient"]
