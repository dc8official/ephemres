import asyncio
import time
import pytest
from app.core.http_client import (
    SSRFSafeHttpClient,
    HttpTimeoutError,
    HttpRequestError,
    MAX_RESPONSE_BYTES,
)


@pytest.fixture(autouse=True)
def allow_loopback_in_tests(monkeypatch):
    monkeypatch.setattr("app.core.http_client._validate_resolved_ip", lambda ip_str, *a, **kw: ip_str)


@pytest.mark.anyio
async def test_http_client_slow_drip_timeout():
    # Start a server that sends status and then hangs
    async def handle_slow(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(2.0)
        writer.write(b"slow data")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_slow, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = SSRFSafeHttpClient(timeout=0.3, allow_private=True)
    t0 = time.monotonic()
    try:
        with pytest.raises(HttpTimeoutError):
            await client.get(f"http://127.0.0.1:{port}/slow")
        elapsed = time.monotonic() - t0
        assert elapsed < 0.8  # Must time out promptly within timeout + 0.5s
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_http_client_huge_content_length_rejected():
    # Server claims Content-Length larger than MAX_RESPONSE_BYTES
    async def handle_huge(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {MAX_RESPONSE_BYTES + 1024}\r\n\r\n".encode("latin-1"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_huge, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = SSRFSafeHttpClient(timeout=2.0, allow_private=True)
    try:
        with pytest.raises(HttpRequestError, match="Response too large"):
            await client.get(f"http://127.0.0.1:{port}/huge")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_http_client_chunked_with_embedded_zero_delimiter():
    # Chunk containing literal 0\r\n\r\n within data
    chunk_data = b"hello\r\n0\r\n\r\nworld"
    chunk_size_hex = f"{len(chunk_data):x}".encode("ascii")

    async def handle_chunked(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        raw_resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            + chunk_size_hex + b"\r\n"
            + chunk_data + b"\r\n"
            + b"0\r\n\r\n"
        )
        writer.write(raw_resp)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_chunked, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = SSRFSafeHttpClient(timeout=2.0, allow_private=True)
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/chunked")
        assert resp.status_code == 200
        assert resp.content == chunk_data
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_http_client_both_chunked_and_content_length():
    # Server sends both Transfer-Encoding: chunked and Content-Length
    async def handle_both(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await reader.readuntil(b"\r\n\r\n")
        raw_resp = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 100\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"\r\n"
            b"4\r\ntest\r\n"
            b"0\r\n\r\n"
        )
        writer.write(raw_resp)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_both, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    client = SSRFSafeHttpClient(timeout=2.0, allow_private=True)
    try:
        resp = await client.get(f"http://127.0.0.1:{port}/both")
        assert resp.status_code == 200
        assert resp.text == "test"
    finally:
        server.close()
        await server.wait_closed()
