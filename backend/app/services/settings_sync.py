from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from app.database import AsyncSessionLocal
from app.services.event_broker import PostgresEventBroker

logger = logging.getLogger(__name__)

CHANNEL_SETTINGS_SYNC = "SYSTEM_SETTINGS_SYNC"


async def broadcast_settings_reload(action: str = "DRIVER_RELOAD") -> None:
    """
    Broadcasts a cluster-wide settings reload event to all running Uvicorn worker processes
    via PostgreSQL LISTEN/NOTIFY.
    """
    try:
        broker = PostgresEventBroker(AsyncSessionLocal)
        payload = {
            "action": action,
            "sender_pid": os.getpid(),
        }
        await broker.publish(CHANNEL_SETTINGS_SYNC, payload)
        logger.info(
            "SettingsSync: Broadcast %s notification across workers (sender_pid=%s)",
            action,
            os.getpid(),
        )
    except Exception as exc:
        logger.warning(
            "SettingsSync: Failed to broadcast reload notification: %s", exc
        )


async def start_settings_sync_listener(session_factory=AsyncSessionLocal) -> None:
    """
    Background worker listening for cluster-wide settings synchronization events.
    When another worker process commits a driver configuration change, this listener
    re-initializes the local StorageDriverManager to prevent worker split-brain states.
    """
    from app.services.driver_manager import driver_manager

    broker = PostgresEventBroker(session_factory)
    my_pid = os.getpid()
    logger.info("SettingsSync: Listener active for worker process PID %s", my_pid)

    try:
        async for event in broker.subscribe(CHANNEL_SETTINGS_SYNC):
            action = event.get("action")
            sender_pid = event.get("sender_pid")

            # Skip self-notifications since the calling worker already re-initialized
            if sender_pid == my_pid:
                logger.debug("SettingsSync: Ignoring self-broadcast from PID %s", my_pid)
                continue

            logger.info(
                "SettingsSync: Worker PID %s received %s from sender PID %s. Synchronizing local drivers...",
                my_pid,
                action,
                sender_pid,
            )

            if action == "RUNTIME_RELOAD":
                from app.services.runtime_settings import refresh_runtime_settings

                await refresh_runtime_settings(session_factory)
                continue

            if action == "DRIVER_RELOAD":
                try:
                    await driver_manager.initialize()
                    logger.info(
                        "SettingsSync: Worker PID %s successfully synchronized storage drivers to mode: %s",
                        my_pid,
                        driver_manager.driver_mode,
                    )
                except Exception as exc:
                    logger.error(
                        "SettingsSync: Worker PID %s failed to re-initialize drivers: %s",
                        my_pid,
                        exc,
                    )
    except asyncio.CancelledError:
        logger.info("SettingsSync: Listener task cancelled cleanly on PID %s", my_pid)
    except Exception as exc:
        logger.error(
            "SettingsSync: Unexpected error in settings sync listener (PID %s): %s",
            my_pid,
            exc,
        )
