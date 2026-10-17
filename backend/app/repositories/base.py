"""Ephemres Network Monitoring Platform — Base Async Repository.

This module provides the generic asynchronous BaseRepository abstraction over
app.core.orm.AsyncSession, offering typed CRUD primitives and parameterized queries.
"""

from __future__ import annotations

from typing import Any, Generic, Optional, Sequence, Type, TypeVar
from app.core.orm import select, update as sa_update, delete as sa_delete
from app.core.orm import AsyncSession
from app.database import Base

T = TypeVar("T", bound=Base)


class BaseRepository(Generic[T]):
    """Abstract generic async base repository providing standard CRUD operations."""

    def __init__(self, model: Type[T], session: AsyncSession) -> None:
        self.model = model
        self.session = session

    async def get_by_id(self, id_val: Any) -> Optional[T]:
        """Fetch a single record by its primary key ID."""
        result = await self.session.get(self.model, id_val)
        return result

    async def list(
        self,
        skip: int = 0,
        limit: int = 100,
        **filters: Any,
    ) -> Sequence[T]:
        """Fetch a list of records matching given filters with pagination."""
        stmt = select(self.model)
        for field, value in filters.items():
            if hasattr(self.model, field):
                stmt = stmt.where(getattr(self.model, field) == value)
        stmt = stmt.offset(skip).limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def create(self, instance: T) -> T:
        """Add and persist a new model instance."""
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def update(self, id_val: Any, **attributes: Any) -> Optional[T]:
        """Dynamically update a model instance by ID."""
        stmt = (
            sa_update(self.model)
            .where(getattr(self.model, "id") == id_val)
            .values(**attributes)
            .returning(self.model)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete(self, id_val: Any) -> bool:
        """Permanently delete a record by ID."""
        stmt = sa_delete(self.model).where(getattr(self.model, "id") == id_val)
        result = await self.session.execute(stmt)
        return result.rowcount > 0
