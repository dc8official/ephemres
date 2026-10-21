import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from monitoring.flow.collector import FlowCollector


def test_flow_collector_uses_configured_bind_host():
    mock_redis = MagicMock()
    collector = FlowCollector(redis_client=mock_redis, bind_host="127.0.0.1")
    assert collector.bind_host == "127.0.0.1"

    with patch("socket.socket") as mock_socket_cls:
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        created = collector._create_socket(9999)
        mock_sock.bind.assert_called_once_with(("127.0.0.1", 9999))
        assert created == mock_sock


def test_flow_collector_socket_does_not_set_so_reuseaddr():
    """SO_REUSEADDR would let another local process bind the same UDP port and take the flow stream."""
    collector = FlowCollector(redis_client=MagicMock(), bind_host="127.0.0.1")
    with patch("socket.socket") as mock_socket_cls:
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        collector._create_socket(9999)
    for call in mock_sock.setsockopt.call_args_list:
        assert call.args[1] != socket.SO_REUSEADDR


def test_second_listener_cannot_bind_collector_port():
    collector = FlowCollector(redis_client=MagicMock(), bind_host="127.0.0.1")
    sock = collector._create_socket(0)
    try:
        port = sock.getsockname()[1]
        intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        intruder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            with pytest.raises(OSError):
                intruder.bind(("127.0.0.1", port))
        finally:
            intruder.close()
    finally:
        sock.close()


@pytest.mark.anyio
async def test_preflight_reports_port_held_by_another_listener():
    from app.routers import settings as settings_router

    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("127.0.0.1", 0))
    try:
        port = holder.getsockname()[1]
        with patch.object(settings_router, "_flow_daemon_active", AsyncMock(return_value=False)):
            assert await settings_router._udp_port_conflicts("127.0.0.1", (port,)) == [port]
        # The running flow daemon holding its own ports is not a conflict.
        with patch.object(settings_router, "_flow_daemon_active", AsyncMock(return_value=True)):
            assert await settings_router._udp_port_conflicts("127.0.0.1", (port,)) == []
    finally:
        holder.close()


@pytest.mark.anyio
async def test_preflight_reports_free_port_as_available():
    from app.routers import settings as settings_router

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with patch.object(settings_router, "_flow_daemon_active", AsyncMock(return_value=False)) as active:
        assert await settings_router._udp_port_conflicts("127.0.0.1", (port,)) == []
    active.assert_not_called()


@pytest.mark.parametrize("value", ["::", "eth0", "300.1.1.1"])
def test_flow_bind_host_rejects_non_ipv4(value):
    from app.config import ConfigurationError, FlowSettings

    with pytest.raises(ConfigurationError):
        FlowSettings(bind_host=value)


def test_flow_bind_host_blank_defaults_to_all_interfaces():
    from app.config import FlowSettings

    assert FlowSettings(bind_host="  ").bind_host == "0.0.0.0"
    assert FlowSettings(bind_host="10.0.0.5").bind_host == "10.0.0.5"
