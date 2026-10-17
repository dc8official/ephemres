from __future__ import annotations

from datetime import datetime
from uuid import UUID

from app.core.orm import DateTime, ForeignKey, Integer, func
from app.core.orm import JSONB, UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class EndpointBaselineRoute(Base):
    """
    Stores single latest online route baseline per endpoint.
    """

    __tablename__ = "endpoint_baseline_routes"

    endpoint_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    total_hops: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    hops: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
    )
