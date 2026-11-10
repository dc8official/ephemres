"""A full pool gives up after pool_timeout, on a real TimescaleDB (scale and core review S6).

The unit tests (tests/backend/test_connection_limits.py) fake the pool; this one holds the only
connection of a real one-connection pool and checks the next request gives up on time with
PoolTimeoutError, gets a connection once the first is back, and that the startup budget check
reads the real server's limits.
"""
import asyncio
import logging
import time

import pytest

from app.core.orm import PoolTimeoutError, create_async_engine


def test_a_held_pool_times_out_then_recovers(timescale_dsn):
    async def scenario():
        engine = create_async_engine(
            timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=1, max_overflow=0, pool_timeout=0.5,
        )
        try:
            held = await engine.acquire_connection()
            started = time.monotonic()
            with pytest.raises(PoolTimeoutError):
                await engine.acquire_connection()
            waited = time.monotonic() - started
            await engine.release_connection(held)
            again = await engine.acquire_connection()
            value = await again.fetchval("SELECT 1")
            await engine.release_connection(again)
            return waited, value
        finally:
            await engine.dispose()

    waited, value = asyncio.run(scenario())
    assert 0.4 < waited < 2.0  # gave up after about pool_timeout, not the 10 s query timeout
    assert value == 1


def test_the_budget_check_reads_the_real_limits(timescale_dsn, monkeypatch, caplog):
    import app.database as database

    engine = create_async_engine(
        timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=1, max_overflow=0
    )
    monkeypatch.setattr(database, "async_engine", engine)

    async def scenario():
        try:
            await database.check_connection_budget()
        finally:
            await engine.dispose()

    with caplog.at_level(logging.INFO, logger="app.database"):
        asyncio.run(scenario())
    # The container runs PostgreSQL's default max_connections = 100 with 3 reserved.
    assert "Database connection budget: up to" in caplog.text
    assert "of 97 connections" in caplog.text
