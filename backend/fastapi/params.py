"""FastAPI params drop-in compatibility shim for Ephemres."""

from app.core.asgi import (
    Body,
    Cookie,
    Depends,
    Header,
    Path,
    Query,
)

__all__ = [
    "Body",
    "Cookie",
    "Depends",
    "Header",
    "Path",
    "Query",
]
