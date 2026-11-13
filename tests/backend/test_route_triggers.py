"""Phase 4 triggers without a database (v3.3.6 build plan 6.2): the reply TTL the engine reads
from raw IPv4 replies, the TTL watch that turns a lasting change into a route hint, and the
relay turning a TTL hint into a "ttl" re-trace.

The transit trigger needs real route rows and is tested in tests/integration/test_route_paths.py.
"""
import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from monitoring.ping import _RawICMPMultiplexer, result_from_rtts
from monitoring.ttl_watch import TtlWatch


def _ipv4_reply(ttl, ident, seq, ts):
    # Minimal IPv4 header (20 bytes, IHL 5) with the given TTL, then the echo reply.
    ip_header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 36, 0, 0, ttl, 1, 0, b"\x01\x01\x01\x01", b"\x0a\x00\x00\x01")
    return ip_header + struct.pack("!BBHHH", 0, 0, 0, ident, seq) + struct.pack("!Q", ts)


@pytest.mark.anyio
async def test_raw_reply_ttl_is_read_from_the_ip_header():
    mux = _RawICMPMultiplexer(is_ipv6=False)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    mux._pending[(77, 1)] = (fut, "1.1.1.1", 1000)
    mux._handle_incoming_packet(_ipv4_reply(57, 77, 1, 1000), "1.1.1.1")
    assert fut.done()
    assert mux._reply_ttls[(77, 1)] == 57


def test_cycle_result_keeps_the_most_common_ttl():
    assert result_from_rtts([1.0, 1.1, 1.2], 3, [57, 57, 56]).reply_ttl == 57
    assert result_from_rtts([1.0], 3).reply_ttl is None
    assert result_from_rtts([], 3, [57]).reply_ttl is None


def test_ttl_watch_needs_three_cycles_in_a_row():
    w = TtlWatch()
    assert w.observe("ep", 57) is None  # first value: the usual one
    assert w.observe("ep", 56) is None
    assert w.observe("ep", None) is None  # a cycle without a TTL changes nothing
    assert w.observe("ep", 56) is None
    assert w.observe("ep", 56) == (57, 56)
    assert w.observe("ep", 56) is None  # now the usual one


def test_ttl_watch_ignores_a_blip():
    w = TtlWatch()
    w.observe("ep", 57)
    w.observe("ep", 56)
    w.observe("ep", 56)
    assert w.observe("ep", 57) is None  # back before confirmation: forgotten
    assert w.observe("ep", 56) is None
    assert w.observe("ep", 55) is None  # a different new value starts over
    assert w.observe("ep", 55) is None
    assert w.observe("ep", 55) == (57, 55)


def test_ttl_hint_queues_a_ttl_retrace_with_its_own_cooldown():
    from app.services import baseline_route
    from app.services.telemetry_relay import TelemetryRelay

    relay = TelemetryRelay()
    ep = uuid4()
    session = AsyncMock()
    res = MagicMock()
    res.fetchone.return_value = ("10.20.0.2",)
    session.execute.return_value = res
    factory = MagicMock()
    factory.return_value.__aenter__.return_value = session
    queue = asyncio.Queue()

    async def scenario():
        with patch("app.services.telemetry_relay.AsyncSessionLocal", factory), \
             patch.object(baseline_route, "discovery_route_queue", queue), \
             patch.object(baseline_route, "discovery_in_progress", set()):
            flow = await relay._handle_route_hint({"endpoint_id": str(ep)}, now=1000.0)
            baseline_route.discovery_in_progress.clear()
            ttl = await relay._handle_route_hint({"endpoint_id": str(ep), "reason": "ttl"}, now=1001.0)
            baseline_route.discovery_in_progress.clear()
            again = await relay._handle_route_hint({"endpoint_id": str(ep), "reason": "ttl"}, now=1002.0)
        return flow, ttl, again

    flow, ttl, again = asyncio.run(scenario())
    assert (flow, ttl, again) == (True, True, False)
    assert queue.get_nowait()[2] == "route_hint"
    assert queue.get_nowait()[2] == "ttl"
