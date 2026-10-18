import socket
import pytest
from app.core.http_client import (
    SSRFSafeHttpClient,
    create_ssrf_safe_client,
    _validate_resolved_ip,
)


def test_forbidden_headers_rejected():
    client = SSRFSafeHttpClient()
    for forbidden in ["Host", "host", "Content-Length", "content-length", "TRANSFER-ENCODING", "Connection"]:
        with pytest.raises(ValueError, match="cannot be overridden"):
            # We don't need to actually send the request, validation happens before connect
            import asyncio
            asyncio.run(client.request("GET", "https://example.com", headers={forbidden: "bad"}))


def test_invalid_http_method_rejected():
    client = SSRFSafeHttpClient()
    for invalid_method in ["get", "post", "GET ", "GET\r\n", "HEAD/1.1", "123"]:
        with pytest.raises(ValueError, match="Invalid HTTP method"):
            import asyncio
            asyncio.run(client.request(invalid_method, "https://example.com"))


def test_follow_redirects_rejected():
    with pytest.raises(ValueError, match="Redirects are not supported"):
        SSRFSafeHttpClient(follow_redirects=True)

    with pytest.raises(ValueError, match="Redirects are not supported"):
        create_ssrf_safe_client(follow_redirects=True)

    client = SSRFSafeHttpClient(follow_redirects=False)
    with pytest.raises(ValueError, match="Redirects are not supported"):
        import asyncio
        asyncio.run(client.request("GET", "https://example.com", follow_redirects=True))


@pytest.mark.anyio
async def test_ipv6_host_header_bracketing(monkeypatch):
    sent_request = b""

    class FakeStream:
        def __init__(self, reader, writer):
            self.reader = reader
            self.writer = writer

    class FakeWriter:
        def write(self, data):
            nonlocal sent_request
            sent_request += data

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class FakeReader:
        def __init__(self):
            self.lines = [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Length: 0\r\n",
                b"\r\n",
            ]

        async def readline(self):
            if self.lines:
                return self.lines.pop(0)
            return b""

        async def read(self, n=-1):
            return b""

    async def fake_open_connection(*args, **kwargs):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open_connection)
    monkeypatch.setattr("app.core.http_client._validate_resolved_ip", lambda ip, *a, **k: ip)
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 80))])

    client = SSRFSafeHttpClient(allow_private=True)
    # Test with port
    await client.request("GET", "http://[2001:db8::1]:8080/path")
    assert b"Host: [2001:db8::1]:8080\r\n" in sent_request

    sent_request = b""
    # Test without port
    await client.request("GET", "http://[2001:db8::1]/path")
    assert b"Host: [2001:db8::1]\r\n" in sent_request


def test_nat64_and_6to4_unwrapping():
    # 64:ff9b::127.0.0.1 (NAT64 embedded loopback)
    with pytest.raises(ValueError, match="resolves to loopback address 127.0.0.1"):
        _validate_resolved_ip("64:ff9b::127.0.0.1", "bad.host", allow_loopback=False)

    # 64:ff9b::10.0.0.1 (NAT64 embedded private)
    with pytest.raises(ValueError, match="resolves to non-global or private network address 10.0.0.1"):
        _validate_resolved_ip("64:ff9b::10.0.0.1", "bad.host", allow_private=False)

    # 2002:7f00:0001:: (6to4 embedded 127.0.0.1)
    with pytest.raises(ValueError, match="resolves to loopback address 127.0.0.1"):
        _validate_resolved_ip("2002:7f00:0001::", "bad.host", allow_loopback=False)

    # 2002:0a00:0001:: (6to4 embedded 10.0.0.1)
    with pytest.raises(ValueError, match="resolves to non-global or private network address 10.0.0.1"):
        _validate_resolved_ip("2002:0a00:0001::", "bad.host", allow_private=False)
