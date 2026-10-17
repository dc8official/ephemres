"""FastAPI drop-in compatibility shim for Ephemres."""

from app.core.asgi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)

__all__ = [
    "APIRouter",
    "BackgroundTasks",
    "Body",
    "Cookie",
    "Depends",
    "FastAPI",
    "Header",
    "HTTPException",
    "Path",
    "Query",
    "Request",
    "Response",
    "status",
]
