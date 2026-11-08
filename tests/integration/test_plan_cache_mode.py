"""Pooled connections plan every run of a prepared statement (real TimescaleDB).

asyncpg prepares each statement and reuses it on the same connection. After five runs
PostgreSQL may switch it to one generic plan that ignores the parameter values; for the
endpoint list at 5,000 endpoints that plan took over 10 s, so the sixth call on every
connection timed out. The pool sets plan_cache_mode=force_custom_plan, so no statement
ever gets a generic plan.
"""
import asyncio
import datetime

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.repositories.endpoint_repo import EndpointRepository


def test_endpoint_list_never_uses_a_generic_plan(timescale_dsn, run_sql):
    async def scenario():
        engine = create_async_engine(
            timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1),
            pool_size=1, max_overflow=0,
        )
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            for _ in range(8):
                async with factory() as session:
                    await EndpointRepository(session).list_with_stats(
                        since_utc=now - datetime.timedelta(hours=24), now_utc=now, page=1, page_size=50,
                    )
            # One connection in the pool, so this is the one that ran all eight calls.
            async with engine.connect() as conn:
                mode = await conn._conn.fetchval("SHOW plan_cache_mode")
                plans = await conn._conn.fetch(
                    "SELECT statement, generic_plans, custom_plans FROM pg_prepared_statements"
                    " WHERE cardinality(parameter_types) > 0"
                )
            return mode, plans
        finally:
            await engine.dispose()

    mode, plans = asyncio.run(scenario())
    assert mode == "force_custom_plan"
    repeated = [p for p in plans if p["custom_plans"] >= 8]
    assert repeated, "the endpoint list was not run 8 times as one prepared statement"
    # A statement without parameters has only one plan, which PostgreSQL counts as generic.
    assert all(p["generic_plans"] == 0 for p in plans), [dict(p) for p in plans if p["generic_plans"]]
