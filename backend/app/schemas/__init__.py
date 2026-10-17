"""Ephemres Network Monitoring Platform — Schemas Package.

This package exports all typed request validation and response serialization models
inheriting from app.core.schema.SchemaModel (BaseModel).
"""

from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
)
from app.schemas.common import (
    APIResponse,
    ErrorDetail,
    PaginationMeta,
)
from app.schemas.endpoints import (
    EndpointDetail,
    EndpointSummary,
)
from app.schemas.events import EventRecord
from app.schemas.monitoring import MonitoringStatus
from app.schemas.reports import (
    FleetEndpointSummary,
    FleetSummaryResponse,
    IncidentRecord,
    UptimeReport,
)
from app.schemas.users import (
    CreateUserRequest,
    ResetPasswordRequest,
    UpdateUserRequest,
    UserSummary,
)

__all__ = [
    "APIResponse",
    "ErrorDetail",
    "PaginationMeta",
    "EndpointSummary",
    "EndpointDetail",
    "EventRecord",
    "UptimeReport",
    "IncidentRecord",
    "FleetEndpointSummary",
    "FleetSummaryResponse",
    "MonitoringStatus",
    "LoginRequest",
    "LoginResponse",
    "ChangePasswordRequest",
    "CreateUserRequest",
    "UpdateUserRequest",
    "ResetPasswordRequest",
    "UserSummary",
]
