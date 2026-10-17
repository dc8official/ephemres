import socket
from unittest.mock import MagicMock, patch
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
