from __future__ import annotations

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from uuid import UUID

from app.core.orm import select
from app.database import AsyncSessionLocal
from app.models.endpoint import Endpoint
from app.services.baseline_service import baseline_cache, start_baseline_refresh_task
from app.services.diagnostics import run_throttled_traceroute, save_diagnostic_trace
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

endpoint_states: dict[str, EndpointState] = {}
states_lock = asyncio.Lock()
db_write_semaphore = asyncio.Semaphore(15)

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


CYCLE_SECONDS = 60.0
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


PING_COUNT, PING_INTERVAL_SECONDS = resolve_ping_plan(
    settings.monitoring.ping_count, settings.monitoring.ping_interval_seconds
)


def _stagger_offset_for_endpoint(endpoint_id: UUID) -> int:
    """Computes deterministic second-slot offset (0-59s) based on endpoint UUID."""
    return int.from_bytes(endpoint_id.bytes[:4], "big") % 60


async def monitor_endpoint(
    endpoint_id: UUID,
    ip_address: str,
    state_machine: StateMachine,
) -> None:
    # PER-01: Deterministic second-slot staggering based on endpoint UUID (0-59s)
    slot_second = _stagger_offset_for_endpoint(endpoint_id)
    now_utc = datetime.now().astimezone()
    now_sec = now_utc.second + (now_utc.microsecond / 1_000_000.0)
    initial_delay = (slot_second - now_sec) % 60.0
    if initial_delay <= 0.05:
        initial_delay += 60.0
    await asyncio.sleep(initial_delay)

    async with AsyncSessionLocal() as db:
        state = await state_machine.initialize_endpoint(endpoint_id, db)
        await db.commit()

    async with states_lock:
        endpoint_states[str(endpoint_id)] = state

    # Fractional First-Minute Handling:
    # When a brand-new endpoint is registered and detected mid-minute (state is None),
    # immediately fire a single baseline validation ping, write it to database,
    # and sleep until the endpoint's deterministic slot boundary.
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

            baseline = baseline_cache.get_baseline(endpoint_id)

            async with db_write_semaphore:
                async with AsyncSessionLocal() as db:
                    current_state = endpoint_states.get(str(endpoint_id))

                    if current_state is None:
                        new_state = await state_machine.create_initial_event(
                            endpoint_id, result, db, baseline=baseline
                        )
                    else:
                        new_state = await state_machine.process_cycle(
                            current_state, result, db, baseline=baseline
                        )

                    # PER-02: Check operational toggles and bound diagnostic trace queue (max 10 in flight).
                    # One trace per endpoint at a time, and only on a state change or once per cooldown.
                    if result.failed_count > 0:
                        toggles = endpoint_registry.get_toggles(endpoint_id)
                        if toggles.get("allow_incident_trace", True):
                            traced_state = new_state.confirmed_detailed_state
                            if not endpoint_registry.incident_trace_due(
                                endpoint_id, traced_state, INCIDENT_TRACE_COOLDOWN_SECONDS
                            ):
                                logger.debug(
                                    "Skipping incident diagnostic trace for %s: in flight or within cooldown",
                                    ip_address,
                                )
                            else:
                                active_diag_count = sum(
                                    len(s) for s in endpoint_registry._diagnostic_tasks.values()
                                )
                                if active_diag_count < 10:
                                    task = safe_create_task(
                                        trigger_incident_diagnostic_trace(
                                            endpoint_id, ip_address
                                        ),
                                        "incident_diagnostic_trace",
                                    )
                                    endpoint_registry.register_diagnostic_task(
                                        endpoint_id, task
                                    )
                                    endpoint_registry.record_incident_trace(endpoint_id, traced_state)
                                else:
                                    logger.warning(
                                        "Skipping incident diagnostic trace for %s: diagnostic queue full (%d active)",
                                        ip_address,
                                        active_diag_count,
                                    )

                    await db.commit()
                # Separate transaction: a failed sample write must not undo the state update.
                await record_probe_sample(endpoint_id, sampled_at, result)

            async with states_lock:
                endpoint_states[str(endpoint_id)] = new_state

        except asyncio.CancelledError:
            logger.info("Monitoring task for endpoint %s cancelled.", endpoint_id)
            break
        except Exception as e:
            logger.error(
                "Error in monitoring cycle for %s: %s: %s",
                ip_address,
                type(e).__name__,
                e,
            )

        # PER-01: Deterministic Slot Loop Alignment (eliminates thundering herds)
        now_utc = datetime.now().astimezone()
        now_sec = now_utc.second + (now_utc.microsecond / 1_000_000.0)
        delay = (slot_second - now_sec) % 60.0
        if delay <= 0.05:
            delay += 60.0

        await asyncio.sleep(delay)


async def _close_startup_gap(gap_id, started_at: datetime) -> None:
    """End the time the engine was not running once every endpoint has run its first cycle."""
    await asyncio.sleep(STARTUP_SETTLE_SECONDS)
    try:
        async with AsyncSessionLocal() as db:
            await close_startup_gap(db, gap_id, started_at)
            await db.commit()
    except Exception as e:
        # The next start closes it instead (resolve_startup_state).
        logger.error("Failed to close the startup monitoring gap: %s: %s", type(e).__name__, e)


async def main() -> None:
    logger.info("Ephemres monitoring engine starting.")
    driver_manager.migrate_sessions = False
    await driver_manager.initialize()
    started_at = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        startup_gap = await resolve_startup_state(db)
        await baseline_cache.refresh_from_db(db)
        await db.commit()
    startup_gap_task = None
    if startup_gap is not None:
        startup_gap_task = asyncio.create_task(_close_startup_gap(startup_gap, started_at), name="close_startup_gap")

    try:
        async with AsyncSessionLocal() as db:
            await sync_event_retention_policy(db, settings.monitoring.retention_days)
    except Exception as e:
        logger.error("Failed to apply monitoring.retention_days to endpoint_events: %s", e)

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

                # PERF-01 / STAB-07: Multi-process engine partitioning
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
