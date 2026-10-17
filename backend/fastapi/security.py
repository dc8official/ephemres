"""FastAPI security drop-in compatibility shim for Ephemres."""

from app.core.asgi import (
    HTTPAuthorizationCredentials,
    HTTPBearer,
    OAuth2PasswordBearer,
)

__all__ = [
    "HTTPAuthorizationCredentials",
    "HTTPBearer",
    "OAuth2PasswordBearer",
]
