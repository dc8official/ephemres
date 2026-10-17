from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

# Add backend directory to sys.path so app modules are discoverable
backend_dir = Path(__file__).resolve().parent.parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.config import settings
from app.database import AsyncSessionLocal, check_database_connection
from app.logging_config import setup_logging
from app.core.watchdog import start_systemd_watchdog
from monitoring.flow.aggregator import FlowAggregator
from monitoring.flow.collector import FlowCollector
from monitoring.flow.correlator import FlowCorrelator

logger = setup_logging(
    service_name="ephemres-flowd",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)


async def run_flow_daemon() -> None:
    logger.info("Starting ephemres-flowd Network Flow Telemetry Daemon (v3.3.2.v)...")

    # Verify database connection
    await check_database_connection()

    # Initialize FastStore client
    store_client = None
    try:
        from app.core.faststore import FastStoreClient

        fs_cfg = getattr(settings, "faststore", getattr(settings, "redis", None))
        r_host = getattr(fs_cfg, "host", "127.0.0.1") if fs_cfg else "127.0.0.1"
        r_port = getattr(fs_cfg, "port", 6379) if fs_cfg else 6379
        r_db = getattr(fs_cfg, "db", 0) if fs_cfg else 0
        client = FastStoreClient(
            host=r_host,
            port=r_port,
            db=r_db,
            socket_connect_timeout=3.0,
            decode_responses=True,
        )
        await client.ping()
        store_client = client
        redis_client = store_client
        logger.info("ephemres-flowd: Connected to FastStore at %s:%d/%d", r_host, r_port, r_db)
    except Exception as e:
        logger.error(
            "ephemres-flowd: FastStore is required for flow ingestion buffer but could not connect: %s",
            e,
        )
        sys.exit(1)

    flow_cfg = getattr(settings, "flow", None)
    netflow_port = getattr(flow_cfg, "netflow_port", 2055) if flow_cfg else 2055
    ipfix_port = getattr(flow_cfg, "ipfix_port", 4739) if flow_cfg else 4739

    correlator = FlowCorrelator(AsyncSessionLocal, store_client=store_client)
    collector = FlowCollector(store_client=store_client, netflow_port=netflow_port, ipfix_port=ipfix_port)
    aggregator = FlowAggregator(store_client=store_client, correlator=correlator, db_session_factory=AsyncSessionLocal)

    await correlator.start()
    await collector.start()
    await aggregator.start()

    watchdog_task = await start_systemd_watchdog(interval_seconds=10)

    logger.info(
        "ephemres-flowd running successfully (NetFlow: UDP %d, IPFIX: UDP %d).",
        netflow_port,
        ipfix_port,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()

    logger.info("ephemres-flowd: Shutdown signal received. Stopping services...")
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    await collector.stop()
    await aggregator.stop()
    await correlator.stop()
    if store_client:
        await store_client.aclose()
    logger.info("ephemres-flowd shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run_flow_daemon())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
