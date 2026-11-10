"""What the webhook HTTP client sends and how it reads what comes back (M3 coverage review,
2026-11-10).

The client talks to servers Ephemres doesn't control, so every malformed or oversized answer is
a case a hostile server can choose. Before these tests about a quarter of SSRFSafeHttpClient.request
never ran: the request it writes, the status-line errors, and most of the body-size limits.
Each test runs a real socket server on 127.0.0.1 that sends a canned answer; the SSRF address
check is switched off only so loopback is reachable (test_ssrf_validator.py covers that check).
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.http_client import (
    MAX_HEADER_BYTES,
    MAX_HEADER_LINES,
    MAX_RESPONSE_BYTES,
    HttpRequestError,
    HttpTimeoutError,
    SSRFSafeHttpClient,
)


@pytest.fixture(autouse=True)
def allow_loopback(monkeypatch):
    monkeypatch.setattr("app.core.http_client._validate_resolved_ip", lambda ip_str, *a, **kw: ip_str)


class Server:
    """Answers every connection with `reply` (bytes) and records the request it received."""

    def __init__(self, reply: bytes, close_after: bool = True) -> None:
        self.reply = reply
        self.close_after = close_after
        self.requests: list[bytes] = []
        self._open: list[asyncio.StreamWriter] = []

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        self.requests.append(head + (await reader.readexactly(length) if length else b""))
        writer.write(self.reply)
        await writer.drain()
        if self.close_after:
            writer.close()
        else:
            self._open.append(writer)

    async def __aenter__(self) -> "Server":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc) -> None:
        # Since Python 3.12 wait_closed() also waits for open connections, so close the ones
        # kept open on purpose first or the test hangs.
        for writer in self._open:
            writer.close()
        self._server.close()
        await self._server.wait_closed()


def _client(timeout: float = 2.0) -> SSRFSafeHttpClient:
    return SSRFSafeHttpClient(timeout=timeout, allow_private=True)


OK_EMPTY = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"


# --- the request that goes out --------------------------------------------------------------

@pytest.mark.anyio
async def test_json_body_is_sent_with_its_length_and_content_type():
    async with Server(OK_EMPTY) as srv:
        await _client().post(f"{srv.url}/hook?x=1", json={"a": 1})
    head, body = srv.requests[0].split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    assert lines[0] == "POST /hook?x=1 HTTP/1.1"
    assert body == b'{"a": 1}'
    assert f"Content-Length: {len(body)}" in lines
    assert "Content-Type: application/json" in lines
    # The port was in the URL, so it is in the Host header too.
    assert f"Host: {srv.url.removeprefix('http://')}" in lines


@pytest.mark.anyio
async def test_a_callers_content_type_is_kept_and_a_text_body_is_utf8():
    async with Server(OK_EMPTY) as srv:
        await _client().post(srv.url, content="héllo", headers={"Content-Type": "text/plain"})
    head, body = srv.requests[0].split(b"\r\n\r\n", 1)
    assert body == "héllo".encode()
    assert head.count(b"Content-Type:") == 1 and b"Content-Type: text/plain" in head


@pytest.mark.anyio
async def test_cr_lf_in_the_url_cannot_add_a_header():
    # urlparse drops CR, LF and tab (WHATWG URL rules) before the client sees the path, so the
    # injected text ends up inside the path and never starts a header line of its own.
    async with Server(OK_EMPTY) as srv:
        await _client().get(f"{srv.url}/a\r\nX-Injected: 1")
    lines = srv.requests[0].decode().split("\r\n")
    assert lines[0] == "GET /aX-Injected: 1 HTTP/1.1"
    assert not any(line.startswith("X-Injected") for line in lines[1:])


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{"X-A": "1\r\nX-B: 2"}, {"X-A\nX-B": "1"}])
async def test_cr_lf_in_a_header_is_refused_before_connecting(headers):
    with pytest.raises(ValueError, match="control characters"):
        await _client().post("http://127.0.0.1:9/", headers=headers)


@pytest.mark.anyio
@pytest.mark.parametrize("url, message", [
    ("", "cannot be empty"),
    ("unix:///run/docker.sock", "Unix sockets"),
    ("ftp://example.com/", "Only HTTP and HTTPS"),
    ("http:///nohost", "Missing hostname"),
    ("http://169.254.169.254/latest/meta-data/", "metadata"),
])
async def test_urls_that_are_refused_before_any_lookup(url, message):
    with pytest.raises(ValueError, match=message):
        await _client().get(url)


@pytest.mark.anyio
async def test_a_refused_connection_is_an_http_request_error():
    async with Server(OK_EMPTY) as srv:
        url = srv.url
    # The server is closed now, so the port refuses connections.
    with pytest.raises(HttpRequestError, match="Network error"):
        await _client().get(url)


# --- the status line ------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("reply, message", [
    (b"", "closed connection before sending status line"),
    (b"SMTP ready\r\n\r\n", "Malformed HTTP status line"),
    (b"HTTP/1.1\r\n\r\n", "Malformed HTTP status line"),
    (b"HTTP/1.1 OK fine\r\n\r\n", "Invalid HTTP status code"),
])
async def test_a_broken_status_line_is_an_error(reply, message):
    async with Server(reply) as srv:
        with pytest.raises(HttpRequestError, match=message):
            await _client().get(srv.url)


# --- Content-Length bodies --------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("value, message", [("ten", "Invalid Content-Length"), ("-5", "Negative Content-Length")])
async def test_a_bad_content_length_is_an_error(value, message):
    async with Server(f"HTTP/1.1 200 OK\r\nContent-Length: {value}\r\n\r\n".encode()) as srv:
        with pytest.raises(HttpRequestError, match=message):
            await _client().get(srv.url)


@pytest.mark.anyio
async def test_a_body_of_exactly_the_limit_is_read_whole():
    body = b"x" * MAX_RESPONSE_BYTES
    async with Server(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body) as srv:
        resp = await _client().get(srv.url)
    assert resp.status_code == 200 and resp.content == body


# --- bodies without a length (read until the server closes) ---------------------------------

@pytest.mark.anyio
async def test_a_body_without_length_is_read_until_close():
    async with Server(b"HTTP/1.1 200 OK\r\nX-Thing: a b\r\n\r\n{\"ok\": true}") as srv:
        resp = await _client().get(srv.url)
    assert resp.json() == {"ok": True}
    assert resp.headers["x-thing"] == "a b"  # names are lower-cased, values stripped


@pytest.mark.anyio
async def test_a_body_without_length_over_the_limit_is_refused():
    async with Server(b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * (MAX_RESPONSE_BYTES + 1)) as srv:
        with pytest.raises(HttpRequestError, match="Response too large"):
            await _client().get(srv.url)


@pytest.mark.anyio
async def test_a_server_that_never_finishes_times_out():
    async with Server(b"HTTP/1.1 200 OK\r\n\r\npartial", close_after=False) as srv:
        with pytest.raises(HttpTimeoutError):
            await _client(timeout=0.3).get(srv.url)


# --- chunked bodies ---------------------------------------------------------------------------

CHUNKED = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"


@pytest.mark.anyio
async def test_chunks_with_extensions_and_trailers_are_joined():
    reply = CHUNKED + b"5;name=v\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: t\r\n\r\n"
    async with Server(reply) as srv:
        resp = await _client().get(srv.url)
    assert resp.content == b"hello world"


@pytest.mark.anyio
@pytest.mark.parametrize("chunks, message", [
    (b"zz\r\nhello\r\n", "Invalid chunk size"),
    (b"5\r\nhelloXX0\r\n\r\n", "missing trailing CRLF"),
    (b"5\r\nhello\r\n", "closed prematurely"),
])
async def test_a_malformed_chunked_body_is_an_error(chunks, message):
    async with Server(CHUNKED + chunks) as srv:
        with pytest.raises(HttpRequestError, match=message):
            await _client().get(srv.url)


@pytest.mark.anyio
async def test_chunks_adding_up_past_the_limit_are_refused():
    # Each chunk is allowed on its own; their sum is not. The check runs before a chunk is
    # read, so the oversize chunk's data is never taken in.
    half = MAX_RESPONSE_BYTES // 2 + 1
    chunk = b"%x\r\n" % half + b"x" * half + b"\r\n"
    async with Server(CHUNKED + chunk + chunk + b"0\r\n\r\n") as srv:
        with pytest.raises(HttpRequestError, match="Response too large"):
            await _client().get(srv.url)


# --- header limits ----------------------------------------------------------------------------
# Headers don't count toward MAX_RESPONSE_BYTES, so they have limits of their own. Without them a
# server that sent headers without end made the client hold about 56 MB in 3 s.

def _headers(n: int, value: bytes = b"v") -> bytes:
    return b"".join(b"X-%d: %s\r\n" % (i, value) for i in range(n))


@pytest.mark.anyio
async def test_the_header_line_limit_is_exact():
    reply = b"HTTP/1.1 200 OK\r\n" + _headers(MAX_HEADER_LINES) + b"Content-Length: 0\r\n\r\n"
    async with Server(reply) as srv:
        # MAX_HEADER_LINES headers plus Content-Length is one over: refused.
        with pytest.raises(HttpRequestError, match="headers too large"):
            await _client().get(srv.url)
    reply = b"HTTP/1.1 200 OK\r\n" + _headers(MAX_HEADER_LINES - 1) + b"Content-Length: 0\r\n\r\n"
    async with Server(reply) as srv:
        assert (await _client().get(srv.url)).status_code == 200


@pytest.mark.anyio
async def test_too_many_header_bytes_are_refused():
    # 20 headers of 4 KiB each: few lines, but over 64 KiB in all.
    value = b"a" * 4096
    assert 20 * len(value) > MAX_HEADER_BYTES
    async with Server(b"HTTP/1.1 200 OK\r\n" + _headers(20, value) + b"\r\n") as srv:
        with pytest.raises(HttpRequestError, match="headers too large"):
            await _client().get(srv.url)


@pytest.mark.anyio
async def test_a_server_sending_headers_without_end_is_stopped_quickly():
    async def flood(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\n")
        try:
            i = 0
            while True:
                writer.write(_headers(1000, b"a" * 100).replace(b"X-", b"X-%d-" % i))
                i += 1
                await writer.drain()
        except (ConnectionError, OSError):
            pass

    server = await asyncio.start_server(flood, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(HttpRequestError, match="headers too large"):
            await _client(timeout=5.0).get(url)
        assert loop.time() - started < 1.0  # stopped by the limit, not by the 5 s timeout
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
async def test_chunked_trailers_count_toward_the_header_limit():
    trailers = _headers(MAX_HEADER_LINES + 1)
    async with Server(CHUNKED + b"2\r\nhi\r\n0\r\n" + trailers + b"\r\n") as srv:
        with pytest.raises(HttpRequestError, match="headers too large"):
            await _client().get(srv.url)


@pytest.mark.anyio
@pytest.mark.parametrize("reply", [
    b"HTTP/1.1 200 OK\r\nX-Big: " + b"a" * 100_000 + b"\r\nContent-Length: 0\r\n\r\n",
    b"HTTP/1.1 200 " + b"a" * 100_000 + b"\r\nContent-Length: 0\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;" + b"a" * 100_000 + b"\r\nx\r\n0\r\n\r\n",
], ids=["header", "status line", "chunk size"])
async def test_one_line_over_64_kib_is_an_http_request_error(reply):
    # asyncio refuses the line with a bare ValueError, which skipped the alert dispatcher's
    # retries and reached the delivery log as "Separator is found, but chunk is longer than
    # limit" (branch review, 2026-11-10).
    async with Server(reply) as srv:
        with pytest.raises(HttpRequestError, match="line too long"):
            await _client().get(srv.url)
