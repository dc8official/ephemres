from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import socket
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Below WatchdogSec (45 s), so a stuck worker is gone before systemd could act on it.
LOOP_STALL_LIMIT_SECONDS = 30.0
LOOP_STALL_EXIT_CODE = 70  # uvicorn stops everything on 3 (startup failure); this one it replaces


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
    Respects systemd $WATCHDOG_USEC if provided, sending heartbeats at half the watchdog interval.
    """
    watchdog_usec = os.environ.get("WATCHDOG_USEC")
    if watchdog_usec:
        try:
            usec = int(watchdog_usec)
            if usec > 0:
                interval_seconds = max(1, min(interval_seconds, int(usec / 2_000_000)))
        except (ValueError, TypeError):
            pass

    async def _watchdog_loop():
        # Signal systemd that application startup is complete
        notify_systemd("READY=1")
        # A worker that just started proves the service runs. Waiting a full interval let systemd
        # restart everything when every worker had been replaced at once (start_loop_stall_guard).
        notify_systemd("WATCHDOG=1")
        logger.info(
            "Systemd watchdog heartbeat worker started (ping interval=%ds).",
            interval_seconds,
        )
        while True:
            await asyncio.sleep(interval_seconds)
            notify_systemd("WATCHDOG=1")

    task = asyncio.create_task(_watchdog_loop(), name="systemd_watchdog")
    return task


def _exit_stalled(stalled_seconds: float) -> None:
    """No logging: the stuck thread may hold the logging lock. stderr goes to the journal."""
    os.write(sys.stderr.fileno(), f"Event loop stuck for {stalled_seconds:.0f} s, exiting so the worker is replaced. Stacks:\n".encode())
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    os._exit(LOOP_STALL_EXIT_CODE)


def start_loop_stall_guard(
    limit_seconds: float = LOOP_STALL_LIMIT_SECONDS,
    tick_seconds: float = 1.0,
    on_stall: Callable[[float], None] = _exit_stalled,
) -> Optional[asyncio.Task]:
    """Ends this process if its event loop stops running, so uvicorn starts a replacement.

    Nothing else notices a worker with a stuck loop. uvicorn's health ping is answered by a
    thread, and with several workers another one's heartbeat satisfies the systemd watchdog. If
    the worker holds the alert lock, alerts stop too. A thread can tell: it runs while the loop
    does not. Only under systemd, where a debugger stopping at a breakpoint is not a risk.
    Cancel the returned task to stop the guard.
    """
    if not os.environ.get("NOTIFY_SOCKET"):
        return None

    last_tick = time.monotonic()
    stop = threading.Event()

    def watch() -> None:
        nonlocal last_tick
        last_wake = time.monotonic()
        while not stop.wait(tick_seconds):
            now = time.monotonic()
            if now - last_wake > limit_seconds / 2:
                # This thread did not run either: the whole process was paused, not just the loop.
                last_tick = now
            last_wake = now
            if now - last_tick > limit_seconds:
                on_stall(now - last_tick)
                return

    async def tick() -> None:
        nonlocal last_tick
        try:
            while True:
                last_tick = time.monotonic()
                await asyncio.sleep(tick_seconds)
        finally:
            stop.set()

    threading.Thread(target=watch, name="loop_stall_guard", daemon=True).start()
    return asyncio.create_task(tick(), name="loop_stall_guard")
