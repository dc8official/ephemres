"""The active monitoring engine: pings every monitored endpoint once a minute and records its state.

Each endpoint gets its own asyncio task (monitor_endpoint) that wakes at a fixed second of the
minute, taken from its UUID, so a large fleet's probes are spread over the minute instead of all
firing at :00. A cycle sends PING_COUNT echo requests, hands the result to the state machine
through the cycle store (which keeps cycles in memory while the database is down), and may start
a traceroute when probes were lost.

main() starts the shared background services, then every 30 s reloads the active endpoints from
the database, starting tasks for new ones and stopping the tasks of removed or disabled ones.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from app.core.orm import select
from app.database import AsyncSessionLocal, is_database_unavailable
from app.models.endpoint import Endpoint
from app.services.baseline_service import baseline_cache, start_baseline_refresh_task
from app.services.diagnostics import run_throttled_traceroute, save_diagnostic_trace
from monitoring.cycle_store import CycleStore, ProbeCycle, new_backlog
from monitoring.gap_handler import STARTUP_SETTLE_SECONDS, close_startup_gap, resolve_startup_state
from monitoring.ping import run_ping_cycle
from monitoring.probe_samples import record_probe_sample
from monitoring.registry import endpoint_registry, MonitoredEndpoint
from monitoring.state_machine import EndpointState, StateMachine

from app.logging_config import setup_logging
from app.config import settings
from app.services.driver_manager import driver_manager
from app.services.retention_policy import sync_event_retention_policy
from app.services.runtime_settings import start_runtime_settings_refresh_task
from app.core.watchdog import start_systemd_watchdog

logger = setup_logging(
    service_name="ephemres-engine",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)

# Latest confirmed state of each endpoint this process monitors, keyed by str(endpoint UUID).
endpoint_states: dict[str, EndpointState] = {}
states_lock = asyncio.Lock()
# At most 15 endpoints write at once. The pool opens up to 20 connections by default
# (database.pool_size 10 + max_overflow 10), so this leaves room for the background tasks.
db_write_semaphore = asyncio.Semaphore(15)

# asyncio only keeps weak references to tasks, so a task nothing else holds can be garbage
# collected before it finishes. Holding them here keeps them alive until they are done.
_active_background_tasks: set[asyncio.Task] = set()


def safe_create_task(coro, task_name: str = "background_task") -> asyncio.Task:
    """Safely spawn a background asyncio task with error logging and lifetime tracking."""
    task = asyncio.create_task(coro)
    _active_background_tasks.add(task)

    def _on_complete(t: asyncio.Task) -> None:
        _active_background_tasks.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(
                    "Background task '%s' failed with exception: %s",
                    task_name,
                    exc,
                    exc_info=exc,
                )

    task.add_done_callback(_on_complete)
    return task


async def trigger_incident_diagnostic_trace(endpoint_id: UUID, ip_address: str) -> None:
    """Fires a background diagnostic traceroute upon detecting a failed ping sub-cycle."""
    try:
        trace_data = await run_throttled_traceroute(ip_address)
        async with db_write_semaphore:
            async with AsyncSessionLocal() as db:
                await save_diagnostic_trace(
                    db, endpoint_id, "FAILED_PING_SUBCYCLE", trace_data
                )
                await db.commit()
                logger.info(
                    "Incident diagnostic trace saved for endpoint %s (%s)",
                    endpoint_id,
                    ip_address,
                )
    except Exception as e:
        logger.error(
            "Failed to execute incident diagnostic trace for %s: %s",
            ip_address,
            e,
        )


# Every endpoint is probed once per cycle. The cycle is fixed: monitoring.interval_seconds in the
# config is accepted but not read here.
CYCLE_SECONDS = 60.0
# How long each echo request waits for its reply before it counts as lost.
PING_TIMEOUT_SECONDS = 2.0
# Worst-case sweep must leave this much of the cycle free for the DB write and slot realignment.
MIN_CYCLE_HEADROOM_SECONDS = 5.0
# Re-trace a still-failing endpoint at most this often unless its confirmed state changes.
INCIDENT_TRACE_COOLDOWN_SECONDS = 900.0
DEFAULT_PING_COUNT = 5
DEFAULT_PING_INTERVAL_SECONDS = 8.0


def worst_case_sweep_seconds(count: int, interval: float, timeout: float = PING_TIMEOUT_SECONDS) -> float:
    """Every probe waits up to `timeout` for its reply, with `interval` sleeps between probes."""
    return count * timeout + max(count - 1, 0) * interval


def resolve_ping_plan(count: int, interval: float) -> tuple[int, float]:
    """Returns the configured (ping_count, ping_interval) or the defaults if they overrun the cycle."""
    budget = CYCLE_SECONDS - MIN_CYCLE_HEADROOM_SECONDS
    if worst_case_sweep_seconds(count, interval) > budget:
        logger.error(
            "monitoring.ping_count=%d with ping_interval_seconds=%s takes up to %.0fs, exceeding the %.0fs "
            "budget of the %.0fs cycle. Falling back to %d pings @ %.1fs.",
            count,
            interval,
            worst_case_sweep_seconds(count, interval),
            budget,
            CYCLE_SECONDS,
            DEFAULT_PING_COUNT,
            DEFAULT_PING_INTERVAL_SECONDS,
        )
        return DEFAULT_PING_COUNT, DEFAULT_PING_INTERVAL_SECONDS
    return count, float(interval)


# Read once at import: a change to ping_count or ping_interval_seconds takes an engine restart.
PING_COUNT, PING_INTERVAL_SECONDS = resolve_ping_plan(
    settings.monitoring.ping_count, settings.monitoring.ping_interval_seconds
)


def _stagger_offset_for_endpoint(endpoint_id: UUID) -> int:
    """The second of the minute (0 to 59) at which this endpoint is probed. Taken from its UUID,
    so it stays the same across restarts, and random UUIDs spread a fleet evenly over the minute."""
    return int.from_bytes(endpoint_id.bytes[:4], "big") % 60


_store: Optional[CycleStore] = None


def _cycle_store(state_machine: StateMachine) -> CycleStore:
    """One per engine: an outage is logged, and a gap recorded, once rather than per endpoint."""
    global _store
    if _store is None:
        _store = CycleStore(state_machine, db_write_semaphore)
    return _store


def _maybe_trace_incident(endpoint_id: UUID, ip_address: str, result: Any, new_state: EndpointState) -> None:
    """Start a traceroute towards an endpoint whose cycle lost probes, to show where the path breaks.

    Skipped when the endpoint has incident traces turned off, when one is already running for it,
    or when it was traced in the same confirmed state less than INCIDENT_TRACE_COOLDOWN_SECONDS
    ago. At most 10 traces are queued across the engine (diagnostics runs 3 of them at a time),
    so a site-wide outage cannot start a traceroute for every endpoint at once."""
    if result.failed_count <= 0:
        return
    if not endpoint_registry.get_toggles(endpoint_id).get("allow_incident_trace", True):
        return
    traced_state = new_state.confirmed_detailed_state
    if not endpoint_registry.incident_trace_due(endpoint_id, traced_state, INCIDENT_TRACE_COOLDOWN_SECONDS):
        logger.debug("Skipping incident diagnostic trace for %s: in flight or within cooldown", ip_address)
        return
    active_diag_count = sum(len(s) for s in endpoint_registry._diagnostic_tasks.values())
    if active_diag_count >= 10:
        logger.warning(
            "Skipping incident diagnostic trace for %s: diagnostic queue full (%d active)",
            ip_address,
            active_diag_count,
        )
        return
    task = safe_create_task(trigger_incident_diagnostic_trace(endpoint_id, ip_address), "incident_diagnostic_trace")
    endpoint_registry.register_diagnostic_task(endpoint_id, task)
    endpoint_registry.record_incident_trace(endpoint_id, traced_state)


async def monitor_endpoint(
    endpoint_id: UUID,
    ip_address: str,
    state_machine: StateMachine,
) -> None:
    """Probe one endpoint once a minute, at its own second, until the task is cancelled
    (the endpoint was removed or disabled)."""
    store = _cycle_store(state_machine)
    # Wait for this endpoint's second of the minute before anything else.
    slot_second = _stagger_offset_for_endpoint(endpoint_id)
    now_utc = datetime.now().astimezone()
    now_sec = now_utc.second + (now_utc.microsecond / 1_000_000.0)
    initial_delay = (slot_second - now_sec) % 60.0
    # Already at the slot (within 50 ms): take the next one rather than probing twice in one slot.
    if initial_delay <= 0.05:
        initial_delay += 60.0
    await asyncio.sleep(initial_delay)

    # A task started during a database outage would otherwise die here and never probe again.
    while True:
        try:
            async with AsyncSessionLocal() as db:
                state = await state_machine.initialize_endpoint(endpoint_id, db)
                await db.commit()
            break
        except Exception as exc:
            if not is_database_unavailable(exc):
                raise
            await asyncio.sleep(CYCLE_SECONDS)

    async with states_lock:
        endpoint_states[str(endpoint_id)] = state

    # A new endpoint (no event in the database yet) gets one ping straight away, so it has a state
    # on the dashboard within seconds instead of after its first full cycle. A lost ping here
    # alerts nobody: a first-probe DOWN is only announced once the usual number of cycles has
    # confirmed it (StateMachine.announce_first_down). If this write fails, the first full cycle
    # creates the event instead (the cycle store is given state None).
    if state is None:
        logger.info(
            "Endpoint %s is brand-new. Firing baseline validation ping.",
            str(endpoint_id),
        )
        try:
            sampled_at = datetime.now(timezone.utc)
            result = await run_ping_cycle(
                ip_address=ip_address,
                count=1,
                interval=PING_INTERVAL_SECONDS,
                timeout=PING_TIMEOUT_SECONDS,
                privileged=True,
            )

            baseline = baseline_cache.get_baseline(endpoint_id)
            async with AsyncSessionLocal() as db:
                new_state = await state_machine.create_initial_event(
                    endpoint_id, result, db, baseline=baseline
                )
                await db.commit()
            await record_probe_sample(endpoint_id, sampled_at, result)

            async with states_lock:
                endpoint_states[str(endpoint_id)] = new_state
                state = new_state

        except Exception as e:
            logger.error(
                "Error in baseline validation ping for %s: %s: %s",
                ip_address,
                type(e).__name__,
                e,
            )

        # Sleep until the endpoint's deterministic slot boundary
        now_utc = datetime.now().astimezone()
        now_sec = now_utc.second + (now_utc.microsecond / 1_000_000.0)
        delay = (slot_second - now_sec) % 60.0
        if delay <= 0.05:
            delay += 60.0
        await asyncio.sleep(delay)

    backlog = new_backlog()
    while True:
        try:
            # Default 5 pings @ 8.0s: ~32s typical, 42s worst case, inside the 60s slot cycle
            sampled_at = datetime.now(timezone.utc)
            result = await run_ping_cycle(
                ip_address=ip_address,
                count=PING_COUNT,
                interval=PING_INTERVAL_SECONDS,
                timeout=PING_TIMEOUT_SECONDS,
                privileged=True,
            )
            # sampled_at (probes started) stamps the probe sample; the end time stamps the event.
            store.add(backlog, ProbeCycle(sampled_at, datetime.now(timezone.utc), result))

            # None while the database is unavailable: the cycle stays in the backlog for later.
            new_state = await store.write(
                endpoint_id, endpoint_states.get(str(endpoint_id)), backlog, baseline_cache.get_baseline(endpoint_id)
            )
            if new_state is not None:
                async with states_lock:
                    endpoint_states[str(endpoint_id)] = new_state
                _maybe_trace_incident(endpoint_id, ip_address, result, new_state)

        except asyncio.CancelledError:
            logger.info("Monitoring task for endpoint %s cancelled.", endpoint_id)
            break
        except Exception as e:
            # This cycle is lost, but the task carries on with the next one.
            logger.error(
                "Error in monitoring cycle for %s: %s: %s",
                ip_address,
                type(e).__name__,
                e,
            )

        # Sleep until this endpoint's second in the next minute. Aligning to the clock, rather than
        # sleeping a fixed 60 s, stops the cycle drifting by however long the probes and the write
        # took, and keeps the fleet spread over the minute instead of slowly bunching up.
        now_utc = datetime.now().astimezone()
        now_sec = now_utc.second + (now_utc.microsecond / 1_000_000.0)
        delay = (slot_second - now_sec) % 60.0
        if delay <= 0.05:
            delay += 60.0

        await asyncio.sleep(delay)


async def _close_startup_gap(gap_id, started_at: datetime) -> None:
    """End the time the engine was not running once every endpoint has run its first cycle."""
    await asyncio.sleep(STARTUP_SETTLE_SECONDS)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await close_startup_gap(db, gap_id, started_at)
                await db.commit()
            return
        except Exception as e:
            # Left open, availability would leave out everything until the next start.
            if is_database_unavailable(e):
                await asyncio.sleep(CYCLE_SECONDS)
                continue
            # The next start closes it instead (resolve_startup_state).
            logger.error("Failed to close the startup monitoring gap: %s: %s", type(e).__name__, e)
            return


async def main() -> None:
    """Run the engine until it is stopped: tidy up after the last run, start the shared background
    tasks, then keep the set of monitoring tasks in step with the endpoints in the database."""
    logger.info("Ephemres monitoring engine starting.")
    await driver_manager.initialize()
    started_at = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # Close the periods the last run left open and record the time it was not running.
        startup_gap = await resolve_startup_state(db)
        await baseline_cache.refresh_from_db(db)
        await db.commit()
    startup_gap_task = None
    if startup_gap is not None:
        startup_gap_task = asyncio.create_task(_close_startup_gap(startup_gap, started_at), name="close_startup_gap")

    # Keep endpoint_events' retention in step with the config. Monitoring works without it, so a
    # failure only logs.
    try:
        async with AsyncSessionLocal() as db:
            await sync_event_retention_policy(db, settings.monitoring.retention_days)
    except Exception as e:
        logger.error("Failed to apply monitoring.retention_days to endpoint_events: %s", e)

    # Each endpoint's usual RTT (mean and spread) for each hour of the week, which
    # classify_ping_result uses to mark a slow but answering endpoint UP-UNSTABLE. Refreshed
    # hourly. Without history (first 7 days, or an empty hour) RTT is not judged at all.
    await start_baseline_refresh_task(AsyncSessionLocal, interval_seconds=3600)
    # Admin UI settings used by the engine (Layer-2 auto-bypass for incident traceroutes).
    runtime_settings_task = await start_runtime_settings_refresh_task(AsyncSessionLocal, interval_seconds=30)
    watchdog_task = await start_systemd_watchdog(interval_seconds=10)
    # FastStore switched in Settings: follow the API onto the new event broker, or the state
    # changes published here would go where the API no longer listens.
    from app.services.settings_sync import start_settings_sync_listener

    settings_sync_task = asyncio.create_task(start_settings_sync_listener(AsyncSessionLocal), name="settings_sync_listener")

    state_machine = StateMachine(
        confirmation_threshold=settings.monitoring.state_confirmation_cycles
    )

    try:
        while True:
            try:
                async with AsyncSessionLocal() as db:
                    stmt = select(Endpoint).where(
                        Endpoint.endpoint_status == "ACTIVE",
                        Endpoint.monitoring_enabled == True,  # noqa: E712
                    )
                    result = await db.execute(stmt)
                    active_endpoints = result.scalars().all()

                # Optional split of the fleet over several engine processes: worker N of M takes the
                # endpoints whose UUID maps to N. The shipped systemd unit sets neither variable,
                # so a standard install runs one worker. The NETMON_ names are the spellings from
                # before the rename (v3.2.0 and v3.3.0 installs).
                worker_id = int(os.environ.get("EPHEMRES_ENGINE_WORKER_ID") or os.environ.get("NETMON_ENGINE_WORKER_ID", "0"))
                num_workers = int(os.environ.get("EPHEMRES_ENGINE_NUM_WORKERS") or os.environ.get("NETMON_ENGINE_NUM_WORKERS", "1"))
                if num_workers > 1:
                    assigned_endpoints = [
                        ep
                        for ep in active_endpoints
                        if (int.from_bytes(ep.id.bytes[:4], "big") % num_workers) == worker_id
                    ]
                else:
                    assigned_endpoints = list(active_endpoints)

                db_active_map = {ep.id: ep for ep in assigned_endpoints}

                # Sync in-memory endpoint registry
                for ep in assigned_endpoints:
                    def _spawn(target: MonitoredEndpoint):
                        return monitor_endpoint(
                            target.id,
                            target.ip_address,
                            state_machine,
                        )

                    await endpoint_registry.add_endpoint(ep, spawn_coro_fn=_spawn)

                # Evict removed or deactivated endpoints
                current_registered = endpoint_registry.list_active_endpoints()
                for reg_ep in current_registered:
                    if reg_ep.id not in db_active_map:
                        logger.info(
                            "Deactivating monitoring for removed endpoint %s",
                            reg_ep.id,
                        )
                        await endpoint_registry.remove_endpoint(reg_ep.id)
                        async with states_lock:
                            endpoint_states.pop(str(reg_ep.id), None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "Error in master orchestration loop: %s: %s",
                    type(e).__name__,
                    e,
                )

            # How soon an endpoint added, removed or disabled in the UI is picked up.
            await asyncio.sleep(30)
    finally:
        if startup_gap_task is not None:
            startup_gap_task.cancel()
        runtime_settings_task.cancel()
        settings_sync_task.cancel()
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitoring engine stopped.")
