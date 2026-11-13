"""ephemres-snmpd: polls endpoints over SNMP (v3.3.6 build plan 8.3).

Shaped like ephemres-flowd: database check, admin settings refreshed every 30 s, systemd watchdog,
orderly stop on SIGTERM. The work itself is in poller.py. The unit is always running; it does
nothing while SNMP is off in Settings (snmp_enabled), so switching SNMP on needs no sudo rule to
start a service, unlike flowd.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

# Before app.database is imported: it gives this process its small pool (SNMPD_DB_POOL).
os.environ["EPHEMRES_PROCESS"] = "snmpd"

backend_dir = Path(__file__).resolve().parent.parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import settings
from app.core.watchdog import start_systemd_watchdog
from app.database import AsyncSessionLocal, check_database_connection
from app.logging_config import setup_logging
from app.services.runtime_settings import get_runtime_settings, start_runtime_settings_refresh_task
from monitoring.snmp.poller import MAX_CONCURRENT, SnmpPoller, Store

logger = setup_logging(
    service_name="ephemres-snmpd",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)


async def run_snmp_daemon() -> None:
    logger.info("Starting ephemres-snmpd (SNMP poller)...")
    await check_database_connection()
    settings_task = await start_runtime_settings_refresh_task(AsyncSessionLocal, interval_seconds=30)
    snmp_cfg = getattr(settings, "snmp", None)

    # The event broker, as flowd uses it: neighbour changes go to every API worker's map.
    from app.services.driver_manager import driver_manager

    await driver_manager.initialize()

    async def announce(reason: str, endpoint_id: str) -> None:
        broker = driver_manager.get_event_broker()
        if broker is not None:
            await broker.publish("NODE_STATE_CHANGE", {"type": "TOPOLOGY_UPDATED", "reason": reason, "endpoint_id": endpoint_id})

    poller = SnmpPoller(
        Store(AsyncSessionLocal, announce=announce),
        enabled=lambda: get_runtime_settings().snmp_enabled,
        interval=lambda: float(get_runtime_settings().snmp_poll_seconds),
        max_concurrent=int(getattr(snmp_cfg, "max_concurrent", MAX_CONCURRENT) if snmp_cfg else MAX_CONCURRENT),
    )
    watchdog_task = await start_systemd_watchdog(interval_seconds=10)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    rt = get_runtime_settings()
    logger.info("ephemres-snmpd running (SNMP %s, poll every %d s).", "on" if rt.snmp_enabled else "off in Settings",
                rt.snmp_poll_seconds)
    await poller.run(stop)

    logger.info("ephemres-snmpd: stopping.")
    watchdog_task.cancel()
    settings_task.cancel()
    logger.info("ephemres-snmpd stopped.")


def main() -> None:
    asyncio.run(run_snmp_daemon())


if __name__ == "__main__":
    main()
