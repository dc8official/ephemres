"""ephemres-flowd: receives NetFlow v5/v9 and IPFIX from routers and stores per-minute traffic.

The pipeline runs in one process:
  FlowCollector (UDP sockets, parsing) -> FlowAggregator (1-minute sums in memory, written every
  10 s) -> flow_minute_rollups and flow_interface_minute_rollups in TimescaleDB.
FlowCorrelator maps IP addresses to endpoints on the way through, and PathTracker watches which
way the routers forward traffic to monitored endpoints, to spot route changes.
"""
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
from app.services.runtime_settings import get_runtime_settings, start_runtime_settings_refresh_task
from monitoring.flow.aggregator import FlowAggregator
from monitoring.flow.collector import LEGACY_STREAM_KEY, FlowCollector
from monitoring.flow.correlator import FlowCorrelator
from monitoring.flow.path_tracker import PathTracker

logger = setup_logging(
    service_name="ephemres-flowd",
    log_dir=getattr(settings.logging, "log_dir", "/var/log/ephemres"),
    log_level=getattr(settings.logging, "level", "INFO"),
)


STATS_INTERVAL_SECONDS = 60.0


async def _log_flow_stats(collector: FlowCollector, aggregator: FlowAggregator) -> None:
    """Logs once a minute what came in, what was stored and what was lost, so loss is visible."""
    last = (0, 0, 0, collector.udp_drops())
    while True:
        await asyncio.sleep(STATS_INTERVAL_SECONDS)
        try:
            now = (collector.flows_received, aggregator.rows_stored, aggregator.flows_dropped_memory, collector.udp_drops())
        except Exception as e:
            logger.debug("ephemres-flowd: could not read flow counters: %s", e)
            continue
        # The counters only grow, so the difference from last minute is last minute's figure.
        received, stored, dropped_mem, udp = (n - p for n, p in zip(now, last))
        last = now
        if udp or dropped_mem:
            logger.warning(
                "ephemres-flowd: last minute %d flows received, %d rollup rows stored; LOST %d datagrams "
                "(kernel socket buffer full: flowd too slow) and %d flows (flow buffer full: %s)",
                received, stored, udp, dropped_mem,
                aggregator.buffer_full_reason() if dropped_mem else "none dropped",
            )
        elif aggregator.last_flush_seconds > aggregator.flush_interval:
            # Nothing lost yet, but a flush takes longer than the gap between flushes: the
            # buffer is growing and will fill if this goes on.
            logger.warning(
                "ephemres-flowd: last minute %d flows received, %d rollup rows stored; rollup writes are "
                "falling behind (the last flush wrote %d rows in %.1f s, flushes are %.0f s apart)",
                received, stored, aggregator.last_flush_rows, aggregator.last_flush_seconds,
                aggregator.flush_interval,
            )
        elif received:
            logger.info("ephemres-flowd: last minute %d flows received, %d rollup rows stored", received, stored)


async def run_flow_daemon() -> None:
    logger.info("Starting ephemres-flowd Network Flow Telemetry Daemon (v3.3.5)...")

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
        # Earlier versions buffered every flow in a FastStore stream; flows now go from the
        # collector to the aggregator in memory. Drop what is left of the stream.
        try:
            await store_client.delete(LEGACY_STREAM_KEY)
        except Exception as e:
            logger.warning("ephemres-flowd: Could not remove the old flow stream from FastStore: %s", e)
        logger.info("ephemres-flowd: Connected to FastStore at %s:%d/%d", r_host, r_port, r_db)
    except Exception as e:
        logger.error(
            "ephemres-flowd: FastStore is required (exporter discovery, interface lists, events) but could not connect: %s",
            e,
        )
        sys.exit(1)

    # Admin UI settings (ports, sampling multiplier) override config.toml; refreshed periodically.
    runtime_settings_task = await start_runtime_settings_refresh_task(AsyncSessionLocal, interval_seconds=30)
    rt = get_runtime_settings()

    flow_cfg = getattr(settings, "flow", None)
    bind_host = getattr(flow_cfg, "bind_host", "0.0.0.0") if flow_cfg else "0.0.0.0"
    netflow_port = rt.flow_netflow_port
    ipfix_port = rt.flow_ipfix_port

    correlator = FlowCorrelator(AsyncSessionLocal, store_client=store_client)
    # Route changes seen in flows go to the API (targeted traceroute) over the same broker the
    # monitoring engine uses, so initialize it the same way.
    from app.services.driver_manager import driver_manager

    await driver_manager.initialize()
    path_tracker = PathTracker(
        change_minutes=getattr(flow_cfg, "route_change_minutes", 3) if flow_cfg else 3,
        ecmp_share=getattr(flow_cfg, "route_ecmp_share", 0.2) if flow_cfg else 0.2,
    )
    aggregator = FlowAggregator(
        store_client=store_client,
        correlator=correlator,
        db_session_factory=AsyncSessionLocal,
        path_tracker=path_tracker,
        # Resolved per event: the broker changes when FastStore is switched in Settings.
        event_publisher=lambda channel, payload: driver_manager.get_event_broker().publish(channel, payload),
    )
    # Parsed flows go straight to the aggregator: no buffer between them to fill or trim.
    collector = FlowCollector(
        sink=aggregator.ingest,
        bind_host=bind_host,
        netflow_port=netflow_port,
        ipfix_port=ipfix_port,
        exporter_filter=correlator.admit_exporter,
    )

    await correlator.start()
    await aggregator.start()
    await collector.start()
    stats_task = asyncio.create_task(_log_flow_stats(collector, aggregator), name="flow_stats")

    watchdog_task = await start_systemd_watchdog(interval_seconds=10)
    # FastStore switched in Settings: follow the API onto the new event broker (route hints).
    from app.services.settings_sync import start_settings_sync_listener

    settings_sync_task = asyncio.create_task(start_settings_sync_listener(AsyncSessionLocal), name="settings_sync_listener")

    logger.info(
        "ephemres-flowd running successfully (NetFlow: UDP %d, IPFIX: UDP %d).",
        netflow_port,
        ipfix_port,
    )

    # Run until systemd stops the service (SIGTERM) or Ctrl+C, then shut down in order so the
    # aggregator's last flush still has its database and FastStore connections.
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
    stats_task.cancel()
    runtime_settings_task.cancel()
    settings_sync_task.cancel()
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
