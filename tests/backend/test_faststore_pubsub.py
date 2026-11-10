"""FastStore pub/sub, within one process and between processes (M3 coverage review, 2026-11-10).

Live dashboard updates, alert dispatch, settings sync and the telemetry relay all ride on
FastStoreEventBroker.subscribe -> PubSub, and none of it ran in a test before this file: every
test used a different broker. Two delivery paths are covered: publish() hands a message to
subscribers in its own process at once, and other processes read it from the pubsub_log table
every 50 ms. The second is how the API workers, engine and flowd hear each other, so one test
publishes from a real second process.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap

import pytest

from app.core.faststore import FastStoreClient, _SharedSQLiteStore
from app.services.event_broker import FastStoreEventBroker

BACKEND = os.path.join(os.path.dirname(__file__), "..", "..", "backend")


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    """A private FastStore directory, so these tests never touch a real store. The per-db
    store objects are cached for the whole process, so the ones made here are dropped after."""
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", str(tmp_path))
    before = set(_SharedSQLiteStore._instances)
    yield tmp_path
    for db in set(_SharedSQLiteStore._instances) - before:
        _SharedSQLiteStore._instances.pop(db, None)


async def _next(listener, timeout=2.0):
    return await asyncio.wait_for(listener.__anext__(), timeout)


@pytest.mark.anyio
async def test_a_subscriber_gets_messages_on_its_channels_only(store_dir):
    client = FastStoreClient(db=71, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("alerts")
    listener = pubsub.listen()

    await client.publish("other", "ignored")
    await client.publish("alerts", "a1")

    msg = await _next(listener)
    assert msg == {"type": "message", "pattern": None, "channel": "alerts", "data": "a1"}
    await pubsub.aclose()


@pytest.mark.anyio
async def test_messages_arrive_in_order_and_bytes_mode_returns_bytes(store_dir):
    client = FastStoreClient(db=72)  # decode_responses=False, as redis-py's default
    pubsub = client.pubsub()
    await pubsub.subscribe(b"ch")
    listener = pubsub.listen()
    for i in range(5):
        await client.publish("ch", f"m{i}")

    got = [await _next(listener) for _ in range(5)]
    assert [m["data"] for m in got] == [f"m{i}".encode() for i in range(5)]
    assert {m["channel"] for m in got} == {b"ch"}
    await pubsub.aclose()


@pytest.mark.anyio
async def test_unsubscribe_and_close_stop_delivery(store_dir):
    client = FastStoreClient(db=73, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("a", "b")
    listener = pubsub.listen()

    await pubsub.unsubscribe("a")
    await client.publish("a", "after-unsubscribe")
    await client.publish("b", "still-subscribed")
    assert (await _next(listener))["data"] == "still-subscribed"

    # close() ends the listen() loop instead of leaving it waiting forever.
    await pubsub.aclose()
    with pytest.raises(StopAsyncIteration):
        await _next(listener)


@pytest.mark.anyio
async def test_a_message_from_another_process_arrives_through_the_log(store_dir):
    client = FastStoreClient(db=74, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe("cross")
    listener = pubsub.listen()

    # A child process has its own PID and no in-memory waiters, so the message can only reach
    # us through the pubsub_log poll.
    publish = textwrap.dedent("""
        import asyncio, sys
        from app.core.faststore import FastStoreClient
        asyncio.run(FastStoreClient(db=74).publish("cross", sys.argv[1]))
    """)
    env = dict(os.environ, PYTHONPATH=os.path.abspath(BACKEND))
    subprocess.run([sys.executable, "-c", publish, "from-child"], env=env, check=True, timeout=30)

    msg = await _next(listener, timeout=5.0)
    assert msg["data"] == "from-child" and msg["channel"] == "cross"
    await pubsub.aclose()


@pytest.mark.anyio
async def test_a_new_subscriber_does_not_see_older_messages(store_dir):
    client = FastStoreClient(db=75, decode_responses=True)
    await client.publish("old", "before")

    pubsub = client.pubsub()
    await pubsub.subscribe("old")
    listener = pubsub.listen()
    await client.publish("old", "after")

    assert (await _next(listener))["data"] == "after"
    await pubsub.aclose()


@pytest.mark.anyio
async def test_the_event_broker_round_trips_json_events(store_dir):
    broker = FastStoreEventBroker(store_client=FastStoreClient(db=76, decode_responses=True))
    events = broker.subscribe("telemetry")
    first = asyncio.ensure_future(events.__anext__())
    await asyncio.sleep(0.05)  # let the generator subscribe before publishing

    await broker.publish("telemetry", {"endpoint": "e1", "state": "DOWN"})

    assert await asyncio.wait_for(first, 2.0) == {"endpoint": "e1", "state": "DOWN"}
    await events.aclose()
