import pytest
from unittest.mock import AsyncMock, MagicMock
from app.core.orm import AsyncEngine, create_async_engine


class FakeAsyncpgPool:
    def __init__(self):
        self.acquire_count = 0
        self.release_count = 0

    async def acquire(self):
        self.acquire_count += 1
        return AsyncMock()

    async def release(self, conn):
        self.release_count += 1

    async def close(self):
        pass


@pytest.mark.anyio
async def test_async_engine_dialect_and_awaitable_connect(monkeypatch):
    fake_pool = FakeAsyncpgPool()

    async def fake_create_pool(*args, **kwargs):
        return fake_pool

    import app.core.orm as orm_mod
    monkeypatch.setattr(orm_mod.asyncpg, "create_pool", fake_create_pool)

    engine = create_async_engine("postgresql+asyncpg://user:pass@localhost:5432/testdb")

    # 1. Assert dialect attribute
    assert hasattr(engine, "dialect")
    assert engine.dialect.name == "postgresql"

    # 2. Assert awaitable connect and close() lifecycle
    conn = await engine.connect()
    assert fake_pool.acquire_count == 1
    assert fake_pool.release_count == 0

    await conn.close()
    assert fake_pool.acquire_count == 1
    assert fake_pool.release_count == 1

    # 3. Calling close() twice releases once (idempotent)
    await conn.close()
    assert fake_pool.acquire_count == 1
    assert fake_pool.release_count == 1

    # 4. Context manager 'async with engine.connect()' still works and releases
    async with engine.connect() as ctx_conn:
        assert fake_pool.acquire_count == 2
        assert fake_pool.release_count == 1

    assert fake_pool.acquire_count == 2
    assert fake_pool.release_count == 2

    # 5. Calling close() inside context manager does not double-release on exit
    async with engine.connect() as ctx_conn2:
        assert fake_pool.acquire_count == 3
        assert fake_pool.release_count == 2
        await ctx_conn2.close()
        assert fake_pool.release_count == 3

    assert fake_pool.acquire_count == 3
    assert fake_pool.release_count == 3

    # 6. AsyncConnection can be used directly as an async context manager
    conn3 = await engine.connect()
    assert fake_pool.acquire_count == 4
    async with conn3:
        pass
    assert fake_pool.release_count == 4

    # 7. await engine.begin() starts a transaction and rolls back/commits properly
    tx_mock = AsyncMock()
    fake_pg_conn = MagicMock()
    fake_pg_conn.transaction.return_value = tx_mock
    fake_pool.acquire = AsyncMock(return_value=fake_pg_conn)

    conn4 = await engine.begin()
    assert conn4._in_tx is True
    assert conn4._tx is not None
    tx_mock.start.assert_awaited_once()

    async with conn4:
        pass
    tx_mock.commit.assert_awaited_once()
    assert fake_pool.release_count == 5

