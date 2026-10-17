from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.core.orm import DateTime, String, Text, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    """
    SQLAlchemy 2.0 model for the app_settings table.
    """

    __tablename__ = "app_settings"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )
    setting_key: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )
    setting_value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# Alias SystemSetting to AppSetting for backward compatibility / domain clarity
SystemSetting = AppSetting
