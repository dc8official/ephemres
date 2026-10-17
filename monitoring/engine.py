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
from monitoring.gap_handler import resolve_startup_state
from monitoring.ping import run_ping_cycle
from monitoring.registry import endpoint_registry, MonitoredEndpoint
from monitoring.state_machine import EndpointState, StateMachine

from app.logging_config import setup_logging
from app.config import settings
from app.services.driver_manager import driver_manager

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
            result = await run_ping_cycle(
                ip_address=ip_address,
                count=1,
                interval=8.0,
                timeout=2.0,
                privileged=True,
            )

            baseline = baseline_cache.get_baseline(endpoint_id)
            async with AsyncSessionLocal() as db:
                new_state = await state_machine.create_initial_event(
                    endpoint_id, result, db, baseline=baseline
                )
                await db.commit()

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
            # 5 pings @ 8.0s = ~32.0s duration, guaranteeing ~28.0s headroom window before next boundary
            result = await run_ping_cycle(
                ip_address=ip_address,
                count=5,
                interval=8.0,
                timeout=2.0,
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

                    # PER-02: Check operational toggles and bound diagnostic trace queue (max 10 in flight)
                    if result.failed_count > 0:
                        toggles = endpoint_registry.get_toggles(endpoint_id)
                        if toggles.get("allow_incident_trace", True):
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
                            else:
                                logger.warning(
                                    "Skipping incident diagnostic trace for %s: diagnostic queue full (%d active)",
                                    ip_address,
                                    active_diag_count,
                                )

                    await db.commit()

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


async def main() -> None:
    logger.info("Ephemres monitoring engine starting.")
    await driver_manager.initialize()
    async with AsyncSessionLocal() as db:
        await resolve_startup_state(db)
        await baseline_cache.refresh_from_db(db)
        await db.commit()

    await start_baseline_refresh_task(AsyncSessionLocal, interval_seconds=3600)

    state_machine = StateMachine(confirmation_threshold=3)

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


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Monitoring engine stopped.")
