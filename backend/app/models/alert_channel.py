from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from app.core.orm import Boolean, DateTime, String, Text, func, text
from app.core.orm import JSONB
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column, relationship

from app.database import Base


class AlertChannel(Base):
    __tablename__ = "alert_channels"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    channel_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=text("true"),
        nullable=False,
        index=True,
    )
    config: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    endpoint_ids: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    subnet_filters: Mapped[list] = mapped_column(
        JSONB,
        default=list,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    severity_filters: Mapped[list] = mapped_column(
        JSONB,
        default=lambda: ["DOWN", "RECOVERED"],
        server_default=text("'[\"DOWN\", \"RECOVERED\"]'::jsonb"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    delivery_logs: Mapped[List["AlertDeliveryLog"]] = relationship(
        "AlertDeliveryLog",
        back_populates="channel",
        cascade="all, delete-orphan",
    )
