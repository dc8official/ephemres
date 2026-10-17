from __future__ import annotations

import asyncio
import logging
import os
import socket

logger = logging.getLogger(__name__)


def notify_systemd(state: str) -> bool:
    """
    Sends a status/watchdog notification string to systemd via $NOTIFY_SOCKET.
    Implements pure-Python UNIX datagram socket communication (zero external dependencies).
    Handles standard UNIX paths and Linux abstract namespace sockets (prefixed with '@').
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False

    # Handle Linux abstract socket namespace (prefixed with @)
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(state.encode("utf-8"), sock_path)
        return True
    except Exception as e:
        logger.debug("Failed to send systemd notification: %s", e)
        return False


async def start_systemd_watchdog(interval_seconds: int = 10) -> asyncio.Task:
    """
    Spawns an asynchronous background worker that notifies systemd of readiness (READY=1)
    and periodically sends watchdog heartbeats (WATCHDOG=1) to prevent WatchdogSec timeouts.
    """
    async def _watchdog_loop():
        # Signal systemd that application startup is complete
        notify_systemd("READY=1")
        logger.info(
            "Systemd watchdog heartbeat worker started (ping interval=%ds).",
            interval_seconds,
        )
        while True:
            await asyncio.sleep(interval_seconds)
            notify_systemd("WATCHDOG=1")

    task = asyncio.create_task(_watchdog_loop(), name="systemd_watchdog")
    return task
