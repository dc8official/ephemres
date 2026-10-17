from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, String, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointDiagnosticTrace(Base):
    """
    SQLAlchemy 2.0 model for the endpoint_diagnostic_traces table.
    """

    __tablename__ = "endpoint_diagnostic_traces"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )
    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    trigger_reason: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    trace_data: Mapped[dict | list] = mapped_column(
        JSONB,
        nullable=False,
    )
