"""Ephemres Network Monitoring Platform — Dynamic System Setting Model.

This module defines the persistent runtime setting model. Values saved here are
cached in-memory and synchronized across multi-worker Uvicorn clusters via
PostgreSQL LISTEN / NOTIFY channels ('system_settings_sync').
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.core.orm import DateTime, String, Text, func
from app.core.orm import UUID as PG_UUID
from app.core.orm import Mapped, mapped_column

from app.database import Base


class AppSetting(Base):
    """Native Declarative ORM Model (app.core.orm.DeclarativeBase) representing

    dynamic key-value system settings in the app_settings table.
    """

    __tablename__ = "app_settings"

    # Unique setting record identifier (UUIDv4)
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
    )

    # Unique configuration parameter key (e.g. 'flow_ingestion_enabled', 'storage_driver')
    setting_key: Mapped[str] = mapped_column(
        String(100),
        unique=True,
        nullable=False,
    )

    # Serialized configuration value (JSON or plaintext string)
    setting_value: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Modification timestamp (UTC)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# Alias SystemSetting to AppSetting for domain clarity and backward compatibility
SystemSetting = AppSetting
