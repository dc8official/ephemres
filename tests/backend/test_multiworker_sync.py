from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.settings_sync import (
    CHANNEL_SETTINGS_SYNC,
    broadcast_settings_reload,
    start_settings_sync_listener,
)


@pytest.mark.anyio
async def test_broadcast_settings_reload():
    """Verifies broadcast_settings_reload publishes to PostgreSQL LISTEN/NOTIFY channel."""
    with patch("app.services.settings_sync.PostgresEventBroker") as MockBroker:
        mock_broker_instance = AsyncMock()
        MockBroker.return_value = mock_broker_instance

        await broadcast_settings_reload(action="DRIVER_RELOAD")

        mock_broker_instance.publish.assert_awaited_once()
        args, _ = mock_broker_instance.publish.call_args
        channel, payload = args
        assert channel == CHANNEL_SETTINGS_SYNC
        assert payload["action"] == "DRIVER_RELOAD"
        assert payload["sender_pid"] == os.getpid()


@pytest.mark.anyio
async def test_settings_sync_listener_reinitializes_on_other_pid():
    """Verifies listener re-initializes driver_manager when receiving reload from another PID."""
    events = [
        {"action": "DRIVER_RELOAD", "sender_pid": 999999},  # Different PID
    ]

    async def mock_subscribe(channel):
        for ev in events:
            yield ev

    mock_broker = MagicMock()
    mock_broker.subscribe = mock_subscribe

    with patch("app.services.settings_sync.PostgresEventBroker", return_value=mock_broker), \
         patch("app.services.driver_manager.driver_manager.initialize", new_callable=AsyncMock) as mock_init:

        task = asyncio.create_task(start_settings_sync_listener())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_init.assert_awaited_once()


@pytest.mark.anyio
async def test_settings_sync_listener_ignores_self_pid():
    """Verifies listener ignores reload events broadcast by its own process PID."""
    events = [
        {"action": "DRIVER_RELOAD", "sender_pid": os.getpid()},  # Self PID
    ]

    async def mock_subscribe(channel):
        for ev in events:
            yield ev

    mock_broker = MagicMock()
    mock_broker.subscribe = mock_subscribe

    with patch("app.services.settings_sync.PostgresEventBroker", return_value=mock_broker), \
         patch("app.services.driver_manager.driver_manager.initialize", new_callable=AsyncMock) as mock_init:

        task = asyncio.create_task(start_settings_sync_listener())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        mock_init.assert_not_awaited()
