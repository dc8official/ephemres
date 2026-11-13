from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import AsyncGenerator

from app.core.orm import text
from app.core.orm import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from app.core.orm import DatabaseUnavailableError, DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Async engine – used by all application database operations
# ---------------------------------------------------------------------------
# echo, pool_pre_ping and pool_recycle used to be passed here and were silently ignored. asyncpg
# closes a connection idle for 300 s on its own, and a connection that died with the server
# fails on its next use (the API answers 503 and the next request gets a fresh one).
#
# Each API worker gets a pool of EPHEMRES_API_DB_POOL connections when its systemd unit sets it
# (10: plenty for about 300 requests/s, scale and core review S6), so the workers don't take the
# engine's and flowd's share of PostgreSQL's connections. Other processes use database.pool_size
# + database.max_overflow (20 by default).
API_DB_POOL_ENV = "EPHEMRES_API_DB_POOL"
# ephemres-snmpd (v3.3.6) only writes short rows between SNMP requests, so it gets a small pool of
# its own instead of the 20 the engine and flowd hold. It names itself in EPHEMRES_PROCESS before
# importing this module (monitoring/snmp/daemon.py); the API's connection budget counts it.
PROCESS_ENV = "EPHEMRES_PROCESS"
SNMPD_DB_POOL = 5


def _is_api_process() -> bool:
    """True in uvicorn and its workers (which inherit uvicorn's argv), false in the engine,
    flowd and scripts. They all load /etc/ephemres/ephemres.env, so an API setting placed there
    by mistake must not shrink the engine's pool.

    Both ways of starting uvicorn count: the venv's `uvicorn` command, and `python -m uvicorn`,
    where argv[0] is .../uvicorn/__main__.py. The second was missed at first, so an API started
    that way took the full pool of 20 per worker and said nothing (branch review, 2026-11-10)."""
    argv0 = sys.argv[0] if sys.argv else ""
    name = os.path.basename(argv0)
    return name == "uvicorn" or (
        name == "__main__.py" and os.path.basename(os.path.dirname(argv0)) == "uvicorn"
    )


def _pool_sizes_for_api(raw: str) -> tuple[int, int] | None:
    """(pool_size, max_overflow) that EPHEMRES_API_DB_POOL=`raw` asks for, or None when it is
    not a positive whole number."""
    return (int(raw), 0) if raw.isdigit() and int(raw) > 0 else None


def _pool_sizes() -> tuple[int, int]:
    """(pool_size, max_overflow) for this process."""
    api_pool = _pool_sizes_for_api(os.environ.get(API_DB_POOL_ENV, "").strip())
    if _is_api_process() and api_pool is not None:
        return api_pool
    if os.environ.get(PROCESS_ENV) == "snmpd":
        return SNMPD_DB_POOL, 0
    return settings.database.pool_size, settings.database.max_overflow


_POOL_SIZE, _MAX_OVERFLOW = _pool_sizes()
async_engine = create_async_engine(
    settings.db_url,
    pool_size=_POOL_SIZE,
    max_overflow=_MAX_OVERFLOW,
    pool_timeout=settings.database.pool_timeout,
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
def is_database_unavailable(exc: BaseException) -> bool:
    """True when an error means the database can't be reached right now, so the same work can
    succeed later, as opposed to the work itself being wrong."""
    import asyncpg

    return isinstance(
        exc,
        (
            DatabaseUnavailableError,
            OSError,  # reset mid-query, and TimeoutError (the command timeout)
            asyncpg.PostgresConnectionError,
            asyncpg.InterfaceError,  # "connection is closed"
            asyncpg.exceptions.OperatorInterventionError,  # shutting down, starting up
        ),
    )


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



# ---------------------------------------------------------------------------
# Readiness probe and connection budget (scale and core review S6)
# ---------------------------------------------------------------------------
async def ping_database(timeout: float = 2.0) -> None:
    """SELECT 1 on a pooled connection, waiting at most `timeout` s for one to come free.

    For the readiness probe. Through a session it waited for a connection with no limit, then
    answered 200 "ready" after 10 s while users were getting errors; now a full pool raises
    PoolTimeoutError within `timeout`, so a health checker sees this worker as busy in time.
    Uses the pool rather than a connection of its own, so probing every few seconds never adds
    to the connection count.

    The query has the same limit as the wait. Left to the pool's 10 s statement timeout, a
    server that held the connection but had stopped answering kept the probe for 12 s, longer
    than a health checker waits; it now answers in 4 s (both measured with the database
    container frozen; branch review, 2026-11-10). The worst case is 3 x `timeout`: the wait,
    the query, and asyncpg handing back a connection whose query it had to cancel."""
    conn = await async_engine.acquire_connection(timeout=timeout)
    try:
        await conn.fetchval("SELECT 1", timeout=timeout)
    finally:
        await async_engine.release_connection(conn)


API_WORKERS_ENV = "EPHEMRES_API_WORKERS"


def connection_budget(
    max_connections: int,
    reserved: int,
    api_workers: int,
    api_pool: int,
    other_pool: int,
    api_listeners: int = 1,
    leader_listeners: int = 0,
    snmpd_pool: int = 0,
) -> tuple[int, int]:
    """(connections Ephemres may open at its peak, connections PostgreSQL lets it open).

    Each API worker holds its pool plus `api_listeners` LISTEN connections outside it, and the
    one worker that sends alerts `leader_listeners` more. The engine and flowd each hold their
    pool plus one (the engine measured 17 to 21 against a pool of 20). `reserved` is what only
    superusers may use (superuser_reserved_connections, plus reserved_connections on
    PostgreSQL 16 and later). ephemres-snmpd (v3.3.6) holds `snmpd_pool` plus one, like the
    others but with its own small pool."""
    need = api_workers * (api_pool + api_listeners) + leader_listeners + 2 * (other_pool + 1)
    if snmpd_pool:
        need += snmpd_pool + 1
    return need, max_connections - reserved


async def check_connection_budget(api_listeners: int = 1, leader_listeners: int = 0) -> None:
    """Log a warning at API startup when every Ephemres pool filled at once would need more
    connections than PostgreSQL allows; the last ones to connect would then fail with "too many
    clients". It used to be found only under load: with the old pool of 20 a third API worker
    didn't fit the default max_connections = 100 (docs/troubleshooting.md). Never stops startup.

    The caller passes the LISTEN connections each worker opens outside its pool, and the extra
    ones on the alert leader (main.py knows which listeners it starts). The first version
    counted one per worker, but without Performance Mode a worker opens five and the leader two
    more: two idle workers held 20 connections, and four workers were reported as 86 of 97
    when they can open 104 (branch review, 2026-11-10)."""
    try:
        async with async_engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT current_setting('max_connections')::int, "
                "current_setting('superuser_reserved_connections')::int, "
                "COALESCE(current_setting('reserved_connections', true), '0')::int"
            ))).fetchone()
    except Exception as exc:
        logger.warning("Could not read PostgreSQL connection limits: %s", exc)
        return
    max_conn, su_reserved, reserved = row[0], row[1], row[2]
    # Say so when the unit's pool setting is not the pool in use. Only the API calls this
    # function, so here an unused setting is a mistake: a launcher _is_api_process() doesn't
    # know, or a value that isn't a positive whole number. It used to be dropped in silence.
    api_pool_raw = os.environ.get(API_DB_POOL_ENV, "").strip()
    if api_pool_raw and (_POOL_SIZE, _MAX_OVERFLOW) != _pool_sizes_for_api(api_pool_raw):
        logger.warning(
            "%s=%s is set but not used: %s. This worker's database pool is %d connections.",
            API_DB_POOL_ENV, api_pool_raw,
            "it must be a positive whole number" if _pool_sizes_for_api(api_pool_raw) is None
            else f"the API was not started by uvicorn ({sys.argv[0] if sys.argv else 'no argv'})",
            _POOL_SIZE + _MAX_OVERFLOW,
        )
    workers_raw = os.environ.get(API_WORKERS_ENV, "").strip()
    workers = int(workers_raw) if workers_raw.isdigit() and int(workers_raw) > 0 else 1
    other_pool = settings.database.pool_size + settings.database.max_overflow
    need, available = connection_budget(
        max_conn, su_reserved + reserved, workers, _POOL_SIZE + _MAX_OVERFLOW, other_pool,
        api_listeners=api_listeners, leader_listeners=leader_listeners, snmpd_pool=SNMPD_DB_POOL,
    )
    if need > available:
        logger.warning(
            "Database connection budget exceeded: %d API worker(s) x (%d + %d for live events), "
            "%d more on the worker that sends alerts, the engine and flowd at %d + 1 each, and snmpd at "
            f"{SNMPD_DB_POOL} + 1 can "
            "open %d connections, but PostgreSQL allows %d (max_connections %d, %d reserved). "
            "Raise max_connections or lower the pools; see docs/troubleshooting.md.",
            workers, _POOL_SIZE + _MAX_OVERFLOW, api_listeners, leader_listeners, other_pool,
            need, available, max_conn, su_reserved + reserved,
        )
    else:
        # The pool size is named so a worker running with a pool other than the unit's is seen
        # in the log of a normal start.
        logger.info(
            "Database connection budget: up to %d of %d connections (this worker's pool: %d).",
            need, available, _POOL_SIZE + _MAX_OVERFLOW,
        )
