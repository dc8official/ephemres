from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from app.core.orm import DateTime, ForeignKey, Integer, String, Text, func, text
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertDeliveryLog(Base):
    __tablename__ = "alert_delivery_logs"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )
    channel_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("alert_channels.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    endpoint_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        index=True,
    )
    endpoint_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    status_code: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )
    retry_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        nullable=False,
    )
    response_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    channel: Mapped["AlertChannel"] = relationship(
        "AlertChannel",
        back_populates="delivery_logs",
    )
