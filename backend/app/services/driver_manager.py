from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.core.orm import select
from app.config import settings
from app.database import AsyncSessionLocal
from app.models.system_setting import SystemSetting
from app.services.event_broker import (
    EventBroker,
    FastStoreEventBroker,
    PostgresEventBroker,
    RedisEventBroker,
)
from app.services.session_store import (
    FastStoreSessionStore,
    PostgresSessionStore,
    RedisSessionStore,
    SessionStore,
    migrate_sessions_faststore_to_pg,
    migrate_sessions_pg_to_faststore,
    migrate_sessions_pg_to_redis,
    migrate_sessions_redis_to_pg,
)

logger = logging.getLogger(__name__)


class StorageDriverManager:
    """
    Manages dual-driver storage and event pub/sub brokers.
    Dynamically routes between PostgreSQL-Native and FastStore-Accelerated drivers
    with automated health-checking and graceful fallback.
    """

    def __init__(self) -> None:
        self._session_store: Optional[SessionStore] = None
        self._event_broker: Optional[EventBroker] = None
        self._store_client = None
        self._driver_mode: str = "postgres"  # "postgres" | "redis"
        # One future per active subscribe(), resolved when initialize() swaps the event broker so
        # the subscription moves to the new one. Futures, not an Event, as they belong to the loop
        # that created them.
        self._broker_waiters: List[asyncio.Future] = []
        # Login sessions live in the API; the engine and flowd switch drivers without copying them.
        self.migrate_sessions = True
        # Sessions are copied between stores only when a running process switches drivers. On
        # the first initialize() there is no previous driver: copying PostgreSQL's sessions over
        # FastStore's would sign out everyone who logged in while FastStore was on.
        self._initialized = False

    async def initialize(self) -> None:
        """Initializes drivers based on system settings and FastStore connectivity."""
        redis_enabled = False
        redis_host = "127.0.0.1"
        redis_port = 6379
        redis_db = 0

        # Check system_settings if available
        db_performance_mode: Optional[bool] = None
        try:
            async with AsyncSessionLocal() as db:
                stmt = select(SystemSetting).where(
                    SystemSetting.setting_key == "performance_mode"
                )
                res = await db.execute(stmt)
                row = res.scalar_one_or_none()
                if row is not None:
                    db_performance_mode = str(row.setting_value).strip().lower() in (
                        "true",
                        "1",
                        "redis",
                        "yes",
                    )
        except Exception:
            pass

        # Check config settings if performance_mode is configured
        if db_performance_mode is not None:
            redis_enabled = db_performance_mode
        else:
            redis_cfg_enabled = getattr(settings.redis, "enabled", True)
            redis_perf_mode = getattr(settings.redis, "performance_mode", False)
            redis_enabled = bool(redis_cfg_enabled and redis_perf_mode)

        if hasattr(settings, "redis"):
            redis_host = getattr(settings.redis, "host", "127.0.0.1")
            redis_port = getattr(settings.redis, "port", 6379)
            redis_db = getattr(settings.redis, "db", 0)

        previous_mode = self._driver_mode
        previous_store = self._store_client
        switching = self._initialized and self.migrate_sessions
        self._initialized = True

        if redis_enabled:
            try:
                from app.core.faststore import FastStoreClient

                client = FastStoreClient(
                    host=redis_host,
                    port=redis_port,
                    db=redis_db,
                    socket_connect_timeout=2.0,
                )
                await client.ping()

                # If switching from PostgreSQL to FastStore, perform warm session migration
                if previous_mode == "postgres" and switching:
                    try:
                        await migrate_sessions_pg_to_faststore(AsyncSessionLocal, client)
                    except Exception as mig_err:
                        logger.warning("Warm session migration to FastStore encountered error: %s", mig_err)

                # Clean up previous client if it differed
                if previous_store is not None and previous_store != client:
                    try:
                        await previous_store.aclose()
                    except Exception:
                        pass

                self._store_client = client
                self._session_store = FastStoreSessionStore(client)
                self._set_event_broker(FastStoreEventBroker(client))
                self._driver_mode = "redis"
                logger.info(
                    "StorageDriverManager: FastStore-Accelerated driver initialized successfully at %s:%d/%d",
                    redis_host,
                    redis_port,
                    redis_db,
                )
                return
            except Exception as e:
                logger.warning(
                    "StorageDriverManager: FastStore unreachable (%s). Falling back gracefully to PostgreSQL-Native driver.",
                    e,
                )

        # If switching from FastStore to PostgreSQL, perform warm session migration
        if previous_mode == "redis" and previous_store is not None and switching:
            try:
                await migrate_sessions_faststore_to_pg(previous_store, AsyncSessionLocal)
            except Exception as mig_err:
                logger.warning("Warm session migration to PostgreSQL encountered error: %s", mig_err)

        if self._store_client is not None:
            try:
                await self._store_client.aclose()
            except Exception:
                pass
            self._store_client = None

        # Fallback to PostgreSQL-Native driver
        self._session_store = PostgresSessionStore(AsyncSessionLocal)
        self._set_event_broker(PostgresEventBroker(AsyncSessionLocal))
        self._driver_mode = "postgres"
        logger.info("StorageDriverManager: PostgreSQL-Native driver active.")

    def get_session_store(self) -> SessionStore:
        if self._session_store is None:
            self._session_store = PostgresSessionStore(AsyncSessionLocal)
        return self._session_store

    def _set_event_broker(self, broker: EventBroker) -> None:
        self._event_broker = broker
        waiters, self._broker_waiters = self._broker_waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def subscribe(self, channel: str) -> AsyncGenerator[Dict[str, Any], None]:
        """Events on `channel` from the current event broker, following it when it is replaced.

        Switching FastStore on or off in Settings re-initializes every process (settings_sync), and
        a subscription kept on the old broker would wait where nobody publishes any more: route
        hints, state changes and alerts from the other processes would be lost.
        """
        while True:
            waiter = asyncio.get_running_loop().create_future()
            self._broker_waiters.append(waiter)
            events = self.get_event_broker().subscribe(channel)
            pending: Optional[asyncio.Future] = None
            ended = False
            try:
                while True:
                    pending = asyncio.ensure_future(events.__anext__())
                    await asyncio.wait({pending, waiter}, return_when=asyncio.FIRST_COMPLETED)
                    if not pending.done():
                        break  # broker replaced: subscribe again on the new one
                    nxt, pending = pending, None
                    try:
                        event = nxt.result()
                    except StopAsyncIteration:
                        ended = True
                        break
                    yield event
            finally:
                replaced = waiter.done() and not waiter.cancelled()
                waiter.cancel()
                if waiter in self._broker_waiters:
                    self._broker_waiters.remove(waiter)
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await events.aclose()
            if ended and not replaced:
                await asyncio.sleep(1.0)  # the broker closed the subscription: do not spin

    def get_event_broker(self) -> EventBroker:
        if self._event_broker is None:
            self._event_broker = PostgresEventBroker(AsyncSessionLocal)
        return self._event_broker

    @property
    def driver_mode(self) -> str:
        return self._driver_mode

    @property
    def _redis_client(self):
        return self._store_client

    @_redis_client.setter
    def _redis_client(self, val) -> None:
        self._store_client = val

    @_redis_client.deleter
    def _redis_client(self) -> None:
        self._store_client = None

    def get_store_client(self):
        return self._store_client

    def get_redis_client(self):
        return self.get_store_client()


# Global storage driver manager singleton
driver_manager = StorageDriverManager()
