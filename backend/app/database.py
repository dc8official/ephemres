from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import AsyncGenerator

from app.core.orm import text
from app.core.orm import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async engine – used by all application database operations
# ---------------------------------------------------------------------------
async_engine = create_async_engine(
    settings.db_url,
    echo=(settings.logging.level == "DEBUG"),
    pool_size=getattr(settings.database, "pool_size", 10),
    max_overflow=getattr(settings.database, "max_overflow", 10),
    pool_pre_ping=True,
    pool_recycle=1800,
)

# ---------------------------------------------------------------------------
# Sync engine – previously used for Alembic migrations.
# Deprecated & removed in Vertical Integration Stage 5A (eliminated psycopg2).
# Kept as None for backward compatibility.
# ---------------------------------------------------------------------------
sync_engine = None

# ---------------------------------------------------------------------------
# Async session factory
# ---------------------------------------------------------------------------
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Declarative base class – imported by all ORM model files
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ASGI route session dependency (app.core.asgi.Depends)
# ---------------------------------------------------------------------------
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a managed async database session for use in ASGI route handlers.

    Commits on clean exit if an active transaction remains uncommitted,
    rolls back on exception if a transaction is active, and always closes
    the session in the finally block.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            if session.is_active and session.in_transaction():
                await session.commit()
        except Exception:
            if session.is_active and session.in_transaction():
                await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Startup health check
# ---------------------------------------------------------------------------
async def check_database_connection(max_retries: int = 5, retry_delay: float = 2.0) -> None:
    """Verify database connectivity at application startup with retry resilience.

    Executes a simple ``SELECT 1`` query. Retries up to max_retries with delay.
    Logs an INFO message on success; prints a descriptive error to stderr and exits with code 1
    only after all retries are exhausted.
    """
    for attempt in range(1, max_retries + 1):
        try:
            async with async_engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("Database connection established successfully.")
            return
        except Exception as exc:
            if attempt < max_retries:
                logger.warning(
                    f"Database connection attempt {attempt}/{max_retries} failed: {exc}. Retrying in {retry_delay}s..."
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.error(
                    "Could not connect to the database after %d attempts: %s",
                    max_retries,
                    exc,
                )
                raise RuntimeError(
                    f"Could not connect to the database after {max_retries} attempts: {exc}"
                )

