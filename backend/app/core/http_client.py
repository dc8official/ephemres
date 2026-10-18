from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import json as json_mod
import os
import socket
import ssl
import typing
from urllib.parse import urlparse

# Cloud metadata endpoints and known dangerous IPs
BLOCKED_IPS = {
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "fd00:ec2::254",    # AWS IPv6 metadata
}

CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class HttpRequestError(Exception):
    """Base exception for outbound HTTP transport errors."""
    pass


class HttpTimeoutError(HttpRequestError, TimeoutError):
    """Raised when an outbound HTTP connection or read operation times out."""
    pass


MAX_RESPONSE_BYTES = 1 * 1024 * 1024


@dataclass
class HttpResponse:
    """Lightweight HTTP response container matching the httpx.Response interface subset."""
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self) -> typing.Any:
        return json_mod.loads(self.text)


def _resolve_allow_private(allow_private: bool | None) -> bool:
    if allow_private is not None:
        return allow_private
    raw_val = os.environ.get("EPHEMRES_ALLOW_PRIVATE_WEBHOOKS") or os.environ.get(
        "NETMON_ALLOW_PRIVATE_WEBHOOKS", "false"
    )
    return raw_val.strip().lower() in ("true", "1", "yes")


def _validate_resolved_ip(
    ip_str: str,
    hostname: str,
    *,
    allow_private: bool = False,
    allow_loopback: bool = False,
) -> str:
    """
    Validates a single resolved IP address against the SSRF security policy.
    Returns the canonical IP string if valid, or raises ValueError on violation.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as exc:
        raise ValueError(f"SSRF violation: Host '{hostname}' returned invalid IP '{ip_str}'.") from exc

    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped  # type: ignore[assignment]

    str_ip = str(ip)
    if str_ip in BLOCKED_IPS:
        raise ValueError(
            f"SSRF violation: Host '{hostname}' resolves to blocked metadata address {str_ip}."
        )

    if not allow_loopback and ip.is_loopback:
        raise ValueError(
            f"SSRF violation: Host '{hostname}' resolves to loopback address {str_ip}."
        )

    if ip.is_link_local:
        raise ValueError(
            f"SSRF violation: Host '{hostname}' resolves to link-local address {str_ip}."
        )

    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        raise ValueError(
            f"SSRF violation: Host '{hostname}' resolves to invalid/reserved address {str_ip}."
        )

    if not allow_private and (
        not ip.is_global or ip.is_private or (ip.version == 4 and ip in CGNAT_NETWORK)
    ):
        raise ValueError(
            f"SSRF violation: Host '{hostname}' resolves to non-global or private network address {str_ip}."
        )

    return str_ip


class SSRFSafeBackend:
    """
    Zero-Trust socket-level network backend (stdlib asyncio).
    Intercepts and validates resolved IP addresses prior to TCP connection establishment
    to prevent Time-of-Check to Time-of-Use (TOCTOU) DNS rebinding SSRF attacks (CWE-918).
    """

    def __init__(
        self,
        inner: typing.Any = None,
        allow_private: bool | None = None,
    ) -> None:
        self._inner = inner
        self._allow_private = _resolve_allow_private(allow_private)

    async def resolve_and_validate(self, host: str, port: int) -> str:
        try:
            loop = asyncio.get_running_loop()
            addr_info = await loop.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        except socket.gaierror as err:
            raise ValueError(f"DNS resolution failure for host '{host}': {err}") from err

        if not addr_info:
            raise ValueError(f"DNS resolution failure for host '{host}': No address returned.")

        target_ip: str | None = None
        for item in addr_info:
            ip_str = item[4][0]
            validated_ip = _validate_resolved_ip(
                ip_str,
                host,
                allow_private=self._allow_private,
                allow_loopback=False,
            )
            if target_ip is None:
                target_ip = validated_ip

        if not target_ip:
            raise ValueError(f"DNS resolution failure for host '{host}': No valid IP address.")
        return target_ip

    async def connect_tcp(
        self,
        host: str,
        port: int,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        target_ip = await self.resolve_and_validate(host, port)
        if self._inner and hasattr(self._inner, "connect_tcp"):
            return await self._inner.connect_tcp(target_ip, port, *args, **kwargs)
        return await asyncio.open_connection(target_ip, port, *args, **kwargs)

    async def connect_unix_socket(
        self,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> typing.Any:
        raise ValueError("Unix sockets are disabled for outbound webhooks.")


def _decode_chunked_body(raw_body: bytes) -> bytes:
    """Decodes an HTTP/1.1 Transfer-Encoding: chunked payload."""
    out = bytearray()
    idx = 0
    n = len(raw_body)
    while idx < n:
        rn = raw_body.find(b"\r\n", idx)
        if rn == -1:
            break
        size_line = raw_body[idx:rn].split(b";", 1)[0].strip()
        if not size_line:
            break
        try:
            chunk_size = int(size_line, 16)
        except ValueError:
            break
        idx = rn + 2
        if chunk_size <= 0 or idx + chunk_size > n:
            break
        out.extend(raw_body[idx : idx + chunk_size])
        idx += chunk_size + 2
    return bytes(out)


class SSRFSafeHttpClient:
    """
    Zero-dependency asynchronous HTTP/1.1 client built on Python stdlib
    (asyncio.open_connection, ssl, socket, urllib.parse) with CWE-918 DNS-rebinding defense.
    Resolves DNS once via loop.getaddrinfo(), validates every candidate IP against the
    SSRF blocklist, and opens the TCP socket directly to the validated IP with TLS SNI
    pinned to the original hostname. Disallows automatic redirects.
    """

    def __init__(
        self,
        timeout: float = 5.0,
        follow_redirects: bool = False,
        allow_private: bool | None = None,
    ) -> None:
        self.timeout = float(timeout)
        self.follow_redirects = bool(follow_redirects)
        self._allow_private = _resolve_allow_private(allow_private)
        self._backend = SSRFSafeBackend(allow_private=self._allow_private)

    async def __aenter__(self) -> SSRFSafeHttpClient:
        return self

    async def __aexit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        pass

    async def post(
        self,
        url: str,
        *,
        json: typing.Any = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        return await self.request(
            "POST",
            url,
            json=json,
            content=content,
            headers=headers,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        return await self.request(
            "GET",
            url,
            headers=headers,
            follow_redirects=follow_redirects,
            timeout=timeout,
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        json: typing.Any = None,
        content: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        if not url or not isinstance(url, str):
            raise ValueError("URL cannot be empty.")
        if url.startswith("unix://") or url.startswith("http+unix://"):
            raise ValueError("Unix sockets are disabled for outbound webhooks.")

        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"Invalid protocol '{parsed.scheme}'. Only HTTP and HTTPS URLs are permitted."
            )

        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"Invalid URL '{url}': Missing hostname.")

        if hostname in BLOCKED_IPS:
            raise ValueError(f"SSRF violation: Access to cloud metadata IP {hostname} is blocked.")

        is_https = scheme == "https"
        port = parsed.port or (443 if is_https else 80)
        eff_timeout = float(timeout if timeout is not None else self.timeout)

        # Resolve and validate all DNS records; pin connection to validated IP (CWE-918 defense)
        validated_ip = await self._backend.resolve_and_validate(hostname, port)

        body_bytes = b""
        req_headers: dict[str, str] = {}
        if headers:
            for k, v in headers.items():
                req_headers[str(k)] = str(v)

        if json is not None:
            body_bytes = json_mod.dumps(json).encode("utf-8")
            if not any(k.lower() == "content-type" for k in req_headers):
                req_headers["Content-Type"] = "application/json"
        elif content is not None:
            body_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)

        path_and_query = parsed.path or "/"
        if parsed.query:
            path_and_query = f"{path_and_query}?{parsed.query}"

        # Reject CR/LF in request target or hostname
        if "\r" in path_and_query or "\n" in path_and_query or "\r" in hostname or "\n" in hostname:
            raise ValueError("Invalid control characters in URL.")

        host_header = f"[{hostname}]:{port}" if ":" in hostname and parsed.port else (
            f"{hostname}:{port}" if parsed.port else hostname
        )

        wire_headers: dict[str, str] = {
            "Host": host_header,
            "User-Agent": "Ephemres-AlertDispatcher/3.3",
            "Accept": "*/*",
            "Connection": "close",
            "Content-Length": str(len(body_bytes)),
        }
        for k, v in req_headers.items():
            if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
                raise ValueError("Invalid control characters in HTTP header.")
            wire_headers[k] = v

        request_lines = [f"{method.upper()} {path_and_query} HTTP/1.1"]
        for hk, hv in wire_headers.items():
            request_lines.append(f"{hk}: {hv}")
        raw_head = ("\r\n".join(request_lines) + "\r\n\r\n").encode("latin-1")

        ssl_ctx: ssl.SSLContext | None = None
        if is_https:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(eff_timeout):
                reader, writer = await asyncio.open_connection(
                    host=validated_ip,
                    port=port,
                    ssl=ssl_ctx,
                    server_hostname=hostname if is_https else None,
                )

                writer.write(raw_head + body_bytes)
                await writer.drain()

                status_line_bytes = await reader.readline()
                if not status_line_bytes:
                    raise HttpRequestError("Remote server closed connection before sending status line.")

                status_line = status_line_bytes.decode("latin-1", errors="replace").strip()
                parts = status_line.split(" ", 2)
                if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                    raise HttpRequestError(f"Malformed HTTP status line: {status_line!r}")
                try:
                    status_code = int(parts[1])
                except ValueError as exc:
                    raise HttpRequestError(f"Invalid HTTP status code in line: {status_line!r}") from exc

                resp_headers: dict[str, str] = {}
                while True:
                    hdr_line = await reader.readline()
                    if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                        break
                    decoded_hdr = hdr_line.decode("latin-1", errors="replace").strip()
                    if ":" in decoded_hdr:
                        hk, hv = decoded_hdr.split(":", 1)
                        resp_headers[hk.strip().lower()] = hv.strip()

                body_chunks = bytearray()
                cl_hdr = resp_headers.get("content-length")
                is_chunked = "chunked" in resp_headers.get("transfer-encoding", "").lower()

                if is_chunked:
                    while True:
                        line = await reader.readline()
                        if not line:
                            raise HttpRequestError("Connection closed prematurely during chunked read.")
                        size_str = line.split(b";")[0].strip()
                        try:
                            chunk_size = int(size_str, 16)
                        except ValueError as exc:
                            raise HttpRequestError(f"Invalid chunk size: {line!r}") from exc

                        if chunk_size < 0:
                            raise HttpRequestError(f"Negative chunk size: {chunk_size}")

                        if chunk_size == 0:
                            while True:
                                trailer = await reader.readline()
                                if not trailer or trailer in (b"\r\n", b"\n"):
                                    break
                            break

                        if len(body_chunks) + chunk_size > MAX_RESPONSE_BYTES:
                            raise HttpRequestError("Response too large")

                        chunk_data = await reader.readexactly(chunk_size)
                        body_chunks.extend(chunk_data)

                        crlf = await reader.readline()
                        if crlf not in (b"\r\n", b"\n"):
                            raise HttpRequestError("Malformed chunk: missing trailing CRLF")

                elif cl_hdr is not None:
                    try:
                        content_len = int(cl_hdr)
                    except ValueError as exc:
                        raise HttpRequestError(f"Invalid Content-Length: {cl_hdr!r}") from exc

                    if content_len < 0:
                        raise HttpRequestError(f"Negative Content-Length: {content_len}")
                    if content_len > MAX_RESPONSE_BYTES:
                        raise HttpRequestError("Response too large")

                    while len(body_chunks) < content_len:
                        needed = min(65536, content_len - len(body_chunks))
                        chunk = await reader.read(needed)
                        if not chunk:
                            break
                        body_chunks.extend(chunk)

                else:
                    while True:
                        chunk = await reader.read(65536)
                        if not chunk:
                            break
                        if len(body_chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise HttpRequestError("Response too large")
                        body_chunks.extend(chunk)

                raw_resp_body = bytes(body_chunks)
                resp_text = raw_resp_body.decode("utf-8", errors="replace")
                return HttpResponse(
                    status_code=status_code,
                    text=resp_text,
                    headers=resp_headers,
                    content=raw_resp_body,
                )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise HttpTimeoutError(f"HTTP request to '{url}' timed out after {eff_timeout}s.") from exc
        except (ValueError, HttpRequestError):
            raise
        except OSError as exc:
            raise HttpRequestError(f"Network error communicating with '{url}': {exc}") from exc
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass


def create_ssrf_safe_client(
    timeout: float = 5.0,
    follow_redirects: bool = False,
    allow_private: bool | None = None,
) -> SSRFSafeHttpClient:
    """
    Constructs a zero-dependency SSRFSafeHttpClient equipped with Zero-Trust
    socket-level SSRF defense and redirect protection.
    """
    return SSRFSafeHttpClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        allow_private=allow_private,
    )


def validate_outbound_url(
    url: str,
    allow_private: bool | None = None,
    allow_loopback: bool = False,
) -> None:
    """
    Validates that a URL is safe for outbound dispatch (webhook / telemetry push).
    Rejects non-HTTP(S) protocols and destinations that resolve to loopback,
    link-local metadata, or private IP addresses unless allow_private is explicitly enabled.
    """
    if not url:
        raise ValueError("URL cannot be empty.")

    eff_allow_private = _resolve_allow_private(allow_private)

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(
            f"Invalid protocol '{parsed.scheme}'. Only HTTP and HTTPS URLs are permitted."
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL '{url}': Missing hostname.")

    # Check for direct cloud metadata IP in host string
    if hostname in BLOCKED_IPS:
        raise ValueError(f"SSRF violation: Access to cloud metadata IP {hostname} is blocked.")

    # Check if host is direct IP address or requires DNS resolution
    try:
        resolved_ips: list[str] = []
        try:
            direct_ip = ipaddress.ip_address(hostname)
            resolved_ips.append(str(direct_ip))
        except ValueError:
            # Domain name: resolve via DNS
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            addr_info = socket.getaddrinfo(
                hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            for item in addr_info:
                resolved_ips.append(item[4][0])

        if not resolved_ips:
            raise ValueError(f"DNS resolution failure for host '{hostname}'.")

        for ip_str in resolved_ips:
            _validate_resolved_ip(
                ip_str,
                hostname,
                allow_private=eff_allow_private,
                allow_loopback=allow_loopback,
            )

    except socket.gaierror as err:
        raise ValueError(f"Failed to resolve host '{hostname}': {err}") from err
