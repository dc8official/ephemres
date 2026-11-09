"""ADR-004 fixes: custom plans on pooled connections (S1), 4 MB receive buffers for the
ICMP and flow sockets (S2), and live-event streams that end with their session (M1)."""
import asyncio
import json
import logging
import re
import socket
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import orm
from app.routers import events
from monitoring import sockbuf

ROOT = Path(__file__).resolve().parents[2]


# --- S1 -------------------------------------------------------------------------------

def test_pool_forces_a_custom_plan_on_every_connection():
    created = {}

    async def fake_create_pool(url, **kwargs):
        created.update(kwargs)
        return MagicMock()

    engine = orm.AsyncEngine("postgresql+asyncpg://u:p@db/x")
    with patch.object(orm.asyncpg, "create_pool", new=fake_create_pool):
        asyncio.run(engine._get_pool())
    assert created["server_settings"] == {"plan_cache_mode": "force_custom_plan"}


# --- S2 -------------------------------------------------------------------------------

def test_receive_buffer_is_requested_and_the_grant_returned():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        granted = sockbuf.set_receive_buffer(sock, "test socket", size=65536)
        assert granted == sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        assert granted >= 65536
    finally:
        sock.close()


def test_capped_receive_buffer_is_logged_as_a_warning(caplog):
    sock = MagicMock()
    sock.getsockopt.return_value = 425984  # stock Ubuntu rmem_max, doubled by the kernel
    with caplog.at_level(logging.WARNING, logger="monitoring.sockbuf"):
        granted = sockbuf.set_receive_buffer(sock, "ICMP socket")
    sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    assert granted == 425984
    assert "net.core.rmem_max" in caplog.text and "ICMP socket" in caplog.text


def test_full_receive_buffer_is_not_a_warning(caplog):
    sock = MagicMock()
    sock.getsockopt.return_value = 8 * 1024 * 1024
    with caplog.at_level(logging.INFO, logger="monitoring.sockbuf"):
        sockbuf.set_receive_buffer(sock, "flow socket UDP 2055")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_icmp_multiplexer_asks_for_the_large_buffer():
    from monitoring import ping

    fake = MagicMock()
    fake.fileno.return_value = 99
    loop = MagicMock()
    with patch.object(ping.socket, "socket", return_value=fake), \
         patch.object(ping, "set_receive_buffer") as set_buf:
        mux = ping._RawICMPMultiplexer(loop=loop)
    assert mux.sock is fake
    set_buf.assert_called_once_with(fake, "ICMP socket")


def test_flow_collector_asks_for_the_large_buffer():
    from monitoring.flow import collector

    fake = MagicMock()
    with patch.object(collector.socket, "socket", return_value=fake), \
         patch.object(collector, "set_receive_buffer") as set_buf:
        coll = collector.FlowCollector.__new__(collector.FlowCollector)
        coll.bind_host = "0.0.0.0"
        coll._sockets = []
        coll._create_socket(2055)
    set_buf.assert_called_once_with(fake, "flow socket UDP 2055")


def test_installed_rmem_max_fits_the_full_request():
    conf = (ROOT / "deploy" / "sysctl-ephemres.conf").read_text()
    value = int(re.search(r"^net\.core\.rmem_max\s*=\s*(\d+)", conf, re.M).group(1))
    assert value >= 2 * sockbuf.RECEIVE_BUFFER_BYTES
    for script in ("install.sh", "upgrade.sh"):
        text = (ROOT / "deploy" / script).read_text()
        assert "sysctl-ephemres.conf" in text and "/etc/sysctl.d/90-ephemres.conf" in text
    assert "/etc/sysctl.d/90-ephemres.conf" in (ROOT / "deploy" / "uninstall.sh").read_text()


# --- M1 -------------------------------------------------------------------------------

def _request():
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)
    return req


async def _drain(gen, limit=50):
    out = []
    try:
        for _ in range(limit):
            out.append(await asyncio.wait_for(gen.asend(None), 2))
    except StopAsyncIteration:
        return out, True
    return out, False


def test_stream_ends_when_the_session_is_signed_out(monkeypatch):
    monkeypatch.setattr(events, "SESSION_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.05)
    valid = {"ok": True}

    async def check(_req):
        return valid["ok"]

    async def scenario():
        gen = events.sse_event_generator(_request(), session_check=check)
        assert json.loads((await gen.asend(None))[6:])["type"] == "CONNECTED"
        await gen.asend(None)  # heartbeat or event while still signed in
        valid["ok"] = False
        return await _drain(gen)

    out, ended = asyncio.run(scenario())
    assert ended
    assert len(events._active_sse_queues) == 0


def test_session_is_rechecked_while_events_keep_arriving(monkeypatch):
    """A busy stream never reaches a heartbeat, so the check runs on a clock, not on idle."""
    monkeypatch.setattr(events, "SESSION_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 30.0)
    calls = []

    async def check(_req):
        calls.append(1)
        return len(calls) < 2

    async def scenario():
        gen = events.sse_event_generator(_request(), session_check=check)
        await gen.asend(None)

        async def flood():
            while True:
                await events.broadcast_sse_event("STATE_TRANSITION", {"n": 1})
                await asyncio.sleep(0.005)

        task = asyncio.create_task(flood())
        try:
            return await _drain(gen, limit=10000)
        finally:
            task.cancel()

    out, ended = asyncio.run(scenario())
    assert ended and len(calls) == 2
    assert any("STATE_TRANSITION" in line for line in out)


def test_session_check_fails_closed_on_errors(monkeypatch):
    async def boom(_req):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(events, "get_current_user_sse", boom)
    assert asyncio.run(events._session_still_valid(_request())) is False


def test_session_check_uses_the_same_rules_as_connect(monkeypatch):
    from app.core.asgi import HTTPException

    async def signed_out(_req):
        raise HTTPException(status_code=401, detail="Session expired or invalid.")

    async def signed_in(_req):
        return {"sub": "u"}

    monkeypatch.setattr(events, "get_current_user_sse", signed_out)
    assert asyncio.run(events._session_still_valid(_request())) is False
    monkeypatch.setattr(events, "get_current_user_sse", signed_in)
    assert asyncio.run(events._session_still_valid(_request())) is True


def test_slow_client_is_disconnected_instead_of_queueing_forever(monkeypatch):
    monkeypatch.setattr(events, "CLIENT_QUEUE_LIMIT", 3)

    async def ok(_req):
        return True

    async def scenario():
        gen = events.sse_event_generator(_request(), session_check=ok)
        await gen.asend(None)
        for i in range(10):
            await events.broadcast_sse_event("STATE_TRANSITION", {"n": i})
        assert len(events._active_sse_queues) == 0  # dropped from the broadcast set at once
        return await _drain(gen)

    out, ended = asyncio.run(scenario())
    assert ended and len(out) <= 3
