import asyncio
import pytest
from unittest.mock import AsyncMock
from app.core.orm import create_async_engine
import app.core.orm as orm_mod


class FakeStalePool:
    def __init__(self, loop):
        self._loop = loop
        self.close_called = False

    async def close(self):
        self.close_called = True


@pytest.mark.anyio
async def test_orm_pool_close_on_replace(monkeypatch):
    current_loop = asyncio.get_running_loop()
    other_loop = asyncio.new_event_loop()

    old_pool = FakeStalePool(other_loop)
    new_pool = FakeStalePool(current_loop)

    async def fake_create_pool(*args, **kwargs):
        return new_pool

    monkeypatch.setattr(orm_mod.asyncpg, "create_pool", fake_create_pool)

    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost:5432/testdb")
    engine._pool = old_pool

    # When _get_pool is called, it detects old_pool belongs to other_loop,
    # best-effort closes old_pool, and acquires new_pool.
    pool = await engine._get_pool()

    assert old_pool.close_called is True
    assert pool is new_pool
    assert engine._pool is new_pool
