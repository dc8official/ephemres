from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
import pytest
from app.core.watchdog import notify_systemd, start_systemd_watchdog


def test_watchdog_notify_socket(tmp_path: Path):
    sock_path = tmp_path / "systemd_notify.sock"
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server_sock.bind(str(sock_path))
    server_sock.setblocking(False)

    old_env = os.environ.get("NOTIFY_SOCKET")
    os.environ["NOTIFY_SOCKET"] = str(sock_path)
    try:
        success = notify_systemd("READY=1")
        assert success is True

        data, _ = server_sock.recvfrom(1024)
        assert data == b"READY=1"
    finally:
        server_sock.close()
        if old_env is None:
            os.environ.pop("NOTIFY_SOCKET", None)
        else:
            os.environ["NOTIFY_SOCKET"] = old_env


@pytest.mark.anyio
async def test_start_systemd_watchdog_interval_usec(tmp_path: Path, monkeypatch):
    sock_path = tmp_path / "systemd_notify2.sock"
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server_sock.bind(str(sock_path))
    server_sock.setblocking(False)

    monkeypatch.setenv("NOTIFY_SOCKET", str(sock_path))
    monkeypatch.setenv("WATCHDOG_USEC", "2000000")  # 2 seconds -> ping at 1 second

    task = await start_systemd_watchdog(interval_seconds=10)
    try:
        # Initial READY=1 notification
        await asyncio.sleep(0.05)
        data, _ = server_sock.recvfrom(1024)
        assert data == b"READY=1"

        # Within ~1.1s, WATCHDOG=1 notification should be received
        await asyncio.sleep(1.1)
        data2, _ = server_sock.recvfrom(1024)
        assert data2 == b"WATCHDOG=1"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        server_sock.close()


def test_deploy_services_watchdog_config():
    root = Path(__file__).resolve().parent.parent.parent
    service_files = [
        root / "deploy" / "ephemres-api.service",
        root / "deploy" / "ephemres-engine.service",
        root / "deploy" / "ephemres-flowd.service",
    ]
    for sf in service_files:
        assert sf.is_file(), f"{sf} does not exist"
        content = sf.read_text(encoding="utf-8")
        assert "WatchdogSec=45" in content, f"{sf.name} missing WatchdogSec=45"
        assert "NotifyAccess=all" in content, f"{sf.name} missing NotifyAccess=all"
        assert "Type=notify" in content, f"{sf.name} missing Type=notify"
