from unittest.mock import MagicMock
from app.services.auth_service import get_trusted_client_ip


def test_trusted_proxy_ip_fallback_when_all_entries_trusted():
    # Peer is trusted (127.0.0.5)
    req = MagicMock()
    req.client.host = "127.0.0.5"
    req.headers.get.side_effect = lambda k: {
        "X-Forwarded-For": "127.0.0.1, 127.0.0.2",
    }.get(k)

    # When all XFF entries are trusted, should return peer_ip
    client_ip = get_trusted_client_ip(req)
    assert client_ip == "127.0.0.5"


def test_trusted_proxy_ip_extracts_untrusted_client():
    # Peer is trusted (127.0.0.1)
    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers.get.side_effect = lambda k: {
        "X-Forwarded-For": "203.0.113.50, 10.0.0.1, 127.0.0.2",
    }.get(k)

    # When trusted_cidrs includes 10.0.0.0/8, 10.0.0.1 is trusted and 203.0.113.50 is returned
    client_ip = get_trusted_client_ip(req, trusted_cidrs=["127.0.0.0/8", "10.0.0.0/8"])
    assert client_ip == "203.0.113.50"
