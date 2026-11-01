"""Subscriptions follow the event broker when FastStore is switched on or off at runtime.

Found under real systemd: after FastStore was turned on in Settings, the API kept listening on
PostgreSQL while ephemres-flowd published route hints on FastStore, and after an API restart the
engine's state changes were lost the other way round.
"""
import asyncio
from pathlib import Path

import pytest

from app.services.driver_manager import StorageDriverManager
from app.services.event_broker import EventBroker

ROOT = Path(__file__).resolve().parents[2]


class QueueBroker(EventBroker):
    def __init__(self, name):
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue()
        self.open_subscriptions = 0

    async def publish(self, channel, event_data):
        await self.queue.put(event_data)

    async def subscribe(self, channel):
        self.open_subscriptions += 1
        try:
            while True:
                yield await self.queue.get()
        finally:
            self.open_subscriptions -= 1


@pytest.mark.anyio
async def test_subscription_moves_to_the_new_broker():
    manager = StorageDriverManager()
    postgres, faststore = QueueBroker("postgres"), QueueBroker("faststore")
    manager._set_event_broker(postgres)

    received: asyncio.Queue = asyncio.Queue()

    async def listen():
        async for event in manager.subscribe("ROUTE_HINT"):
            await received.put(event)

    task = asyncio.create_task(listen())
    try:
        await postgres.publish("ROUTE_HINT", {"n": 1})
        assert await asyncio.wait_for(received.get(), 1) == {"n": 1}

        manager._set_event_broker(faststore)  # FastStore switched on in Settings
        await asyncio.sleep(0.05)
        assert postgres.open_subscriptions == 0 and faststore.open_subscriptions == 1

        await faststore.publish("ROUTE_HINT", {"n": 2})
        assert await asyncio.wait_for(received.get(), 1) == {"n": 2}
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert faststore.open_subscriptions == 0


@pytest.mark.anyio
async def test_events_already_queued_are_delivered_before_the_switch():
    manager = StorageDriverManager()
    old = QueueBroker("old")
    manager._set_event_broker(old)
    stream = manager.subscribe("STATE_TRANSITION")
    await old.publish("STATE_TRANSITION", {"n": 1})
    assert await asyncio.wait_for(stream.__anext__(), 1) == {"n": 1}
    await stream.aclose()
    assert old.open_subscriptions == 0


def test_every_long_lived_subscriber_follows_the_broker():
    for path in ("backend/app/services/telemetry_relay.py", "backend/app/services/alert_dispatcher.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "async for event in driver_manager.subscribe(channel):" in text, path
        assert "get_event_broker().subscribe(" not in text and "broker.subscribe(channel)" not in text, path


def test_engine_and_flowd_follow_settings_changes():
    for path in ("monitoring/engine.py", "monitoring/flow/daemon.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "start_settings_sync_listener(AsyncSessionLocal)" in text, path
        assert "driver_manager.migrate_sessions = False" in text, path
    daemon = (ROOT / "monitoring/flow/daemon.py").read_text(encoding="utf-8")
    # Resolved per event, not bound to the broker of startup.
    assert "event_publisher=driver_manager.get_event_broker().publish" not in daemon
