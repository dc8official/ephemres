from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app.core.orm import text
from app.database import async_engine

logger = logging.getLogger(__name__)

# Distinct from AlertDispatcher (992817) and the migrator (727274).
SCHEDULER_LOCK_ID = 992818


class SchedulerLeader:
    """
    Elects one API worker to run cluster-wide scheduled jobs (midnight discovery, diagnostic cleanup).

    uvicorn runs several workers, each with its own lifespan; without this every worker fires the
    same schedule. The leader holds a PostgreSQL session advisory lock on a dedicated connection.
    Standby workers retry every `retry_seconds`; the leader pings its lock connection on the same
    cadence and steps down if it fails, since PostgreSQL drops the lock with the connection.
    Jobs check `is_leader` when they fire rather than at startup, so a standby that is promoted
    later picks up the next run. Non-PostgreSQL dialects (tests, dev) always lead.
    """

    def __init__(self, lock_id: int = SCHEDULER_LOCK_ID, retry_seconds: float = 5.0) -> None:
        self._lock_id = lock_id
        self._retry_seconds = retry_seconds
        self._lock_conn: Any = None
        self._is_leader = False
        self._task: Optional[asyncio.Task] = None

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        if self._task is not None:
            return
        if async_engine.dialect.name != "postgresql":
            self._is_leader = True
            return
        await self._try_acquire()
        self._task = asyncio.create_task(self._maintain_loop(), name="scheduler_leader")

    async def _try_acquire(self) -> None:
        conn = None
        try:
            conn = await async_engine.connect()
            res = await conn.execute(text(f"SELECT pg_try_advisory_lock({self._lock_id})"))
            if bool(res.scalar()):
                self._lock_conn = conn
                self._is_leader = True
                logger.info("[SchedulerLeader] This worker runs the scheduled jobs.")
                return
        except Exception as exc:
            logger.warning("[SchedulerLeader] Advisory lock election failed: %s", exc)
        if conn is not None:
            await self._close(conn)

    async def _maintain_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._retry_seconds)
                if self._is_leader:
                    try:
                        await self._lock_conn.execute(text("SELECT 1"))
                    except Exception as exc:
                        logger.warning("[SchedulerLeader] Lost lock connection, stepping down: %s", exc)
                        self._is_leader = False
                        await self._close(self._lock_conn)
                        self._lock_conn = None
                else:
                    await self._try_acquire()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("[SchedulerLeader] Election loop error: %s", exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._lock_conn is not None:
            try:
                await self._lock_conn.execute(text(f"SELECT pg_advisory_unlock({self._lock_id})"))
            except Exception as exc:
                logger.warning("[SchedulerLeader] Failed to release advisory lock: %s", exc)
            await self._close(self._lock_conn)
            self._lock_conn = None
        self._is_leader = False

    @staticmethod
    async def _close(conn: Any) -> None:
        try:
            await conn.close()
        except Exception:
            pass


scheduler_leader = SchedulerLeader()
