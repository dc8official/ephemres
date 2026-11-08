"""Writes an endpoint's probe cycles, keeping them in memory while the database is unavailable.

The engine used to throw a cycle away when it could not save it, so an endpoint that went down
and recovered during a database outage left no event, no sample and no downtime, and the hole
counted as neither up, down nor unknown in availability figures.

Every cycle now joins its endpoint's backlog and the backlog is written whole: normally that is
one cycle, after an outage all the cycles it missed, in order and with the times they ran. So
history comes out as if the database had never left.

Alerts follow the net change. A transition that is still in effect once the backlog is written
is announced then; one that started and ended during the outage is only recorded, because a
late "host was down ten minutes ago" tells an operator nothing to act on. Transitions are
announced after the commit, so nothing is announced that was not saved.

The backlog is bounded. Past MAX_BACKLOG_CYCLES the oldest cycles are dropped and recorded as a
DATABASE_DISCONNECT monitoring gap, which availability figures leave out.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Deque, Optional, Tuple
from uuid import UUID

from monitoring.gap_handler import record_lost_cycles_gap
from monitoring.ping import PingResult
from monitoring.probe_samples import record_probe_sample
from monitoring.state_machine import EndpointState, StateMachine

from app.core.orm import text
from app.database import AsyncSessionLocal, is_database_unavailable

logger = logging.getLogger(__name__)

# One cycle a minute: four hours of history, about 300 bytes per cycle (72 MB for 1,000 endpoints).
MAX_BACKLOG_CYCLES = 240


@dataclass(frozen=True)
class ProbeCycle:
    sampled_at: datetime
    finished_at: datetime
    result: PingResult


def new_backlog() -> Deque[ProbeCycle]:
    return deque(maxlen=MAX_BACKLOG_CYCLES)


class CycleStore:
    def __init__(
        self,
        state_machine: StateMachine,
        write_limit: asyncio.Semaphore,
        session_factory: Callable[[], Any] = AsyncSessionLocal,
    ) -> None:
        self.state_machine = state_machine
        self.write_limit = write_limit
        self.session_factory = session_factory
        self._outage_logged = False
        self._lost_from: Optional[datetime] = None
        self._lost_to: Optional[datetime] = None

    def add(self, backlog: Deque[ProbeCycle], cycle: ProbeCycle) -> None:
        """Adds a cycle, noting the oldest one when the backlog is full and drops it."""
        if backlog.maxlen is not None and len(backlog) == backlog.maxlen:
            dropped = backlog[0].finished_at
            self._lost_from = min(self._lost_from or dropped, dropped)
            self._lost_to = max(self._lost_to or dropped, backlog[1].finished_at if len(backlog) > 1 else dropped)
        backlog.append(cycle)

    async def write(
        self,
        endpoint_id: UUID,
        state: Optional[EndpointState],
        backlog: Deque[ProbeCycle],
        baseline: Optional[Tuple[float, float]] = None,
    ) -> Optional[EndpointState]:
        """Writes the backlog and empties it. Returns the state after it, or None and keeps the
        backlog when the database is unavailable. Any other error drops the backlog and raises:
        retrying a cycle that cannot be written would block every later one."""
        replaying = len(backlog) > 1
        try:
            async with self.write_limit:
                if replaying:
                    # Fail before the samples: this runs every minute for every endpoint.
                    async with self.session_factory() as db:
                        await db.execute(text("SELECT 1"))
                # Before the events, so a period closed in the backlog finds its samples.
                # Separate transactions: a failed sample write cannot undo state, and rewriting
                # one after a failed attempt is harmless (ON CONFLICT DO NOTHING).
                for cycle in backlog:
                    await record_probe_sample(endpoint_id, cycle.sampled_at, cycle.result)
                current = state
                async with self.session_factory() as db:
                    for cycle in backlog:
                        if current is None:
                            current = await self.state_machine.create_initial_event(
                                endpoint_id, cycle.result, db, baseline=baseline, at=cycle.finished_at
                            )
                        else:
                            current = await self.state_machine.process_cycle(
                                current, cycle.result, db, baseline=baseline, at=cycle.finished_at, announce=False
                            )
                    await db.commit()
                await self._record_lost_cycles()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_database_unavailable(exc):
                if not self._outage_logged:
                    self._outage_logged = True
                    logger.warning(
                        "Database unavailable (%s: %s). Keeping probe results in memory, up to %d cycles "
                        "per endpoint, and writing them when it returns.",
                        type(exc).__name__, exc, MAX_BACKLOG_CYCLES,
                    )
                return None
            backlog.clear()
            raise

        if self._outage_logged:
            self._outage_logged = False
            logger.info("Database back: writing the probe results kept during the outage.")
        latest = backlog[-1].result
        cycles = len(backlog)
        backlog.clear()
        if replaying:
            logger.debug("Wrote %d kept cycles for endpoint %s", cycles, endpoint_id)
        await self._announce_net_change(state, current, latest)
        return current

    async def _record_lost_cycles(self) -> None:
        if self._lost_from is None:
            return
        lost_from, lost_to = self._lost_from, self._lost_to
        async with self.session_factory() as db:
            await record_lost_cycles_gap(db, lost_from, lost_to)
            await db.commit()
        self._lost_from = self._lost_to = None

    async def _announce_net_change(
        self, previous: Optional[EndpointState], current: EndpointState, result: PingResult
    ) -> None:
        first_down = current.announce_first_down
        if not first_down and (previous is None or previous.confirmed_detailed_state == current.confirmed_detailed_state):
            return
        try:
            async with self.session_factory() as db:
                if first_down:
                    await self.state_machine.announce_first_down(current, result, db)
                else:
                    await self.state_machine.announce_transition(previous, current, result, db)
        except Exception as exc:
            # Saved but not announced: the state is right, and failing here would rewrite it.
            logger.error("Could not announce the state change of endpoint %s: %s", current.endpoint_id, exc)
