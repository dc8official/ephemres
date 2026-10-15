from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import typing
from urllib.parse import urlparse

import httpcore
import httpx

# Cloud metadata endpoints and known dangerous IPs
BLOCKED_IPS = {
    "169.254.169.254",  # AWS/GCP/Azure link-local metadata
    "fd00:ec2::254",    # AWS IPv6 metadata
}


CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class SSRFSafeBackend(httpcore.AsyncNetworkBackend):
    """
    Zero-Trust socket-level network backend wrapping httpcore.AsyncNetworkBackend.
    Intercepts and validates resolved IP addresses during the TCP handshake to prevent
    Time-of-Check to Time-of-Use (TOCTOU) DNS rebinding SSRF attacks (CWE-918).
    """

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend | None = None,
        allow_private: bool | None = None,
    ):
        self._inner = inner if inner is not None else httpcore.AsyncNetworkBackend()
        if allow_private is None:
            raw_val = os.environ.get("EPHEMRES_ALLOW_PRIVATE_WEBHOOKS") or os.environ.get("NETMON_ALLOW_PRIVATE_WEBHOOKS", "false")
            self._allow_private = raw_val.lower() in (
                "true",
                "1",
                "yes",
            )
        else:
            self._allow_private = allow_private

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addr_info = await asyncio.to_thread(
                socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
        except socket.gaierror as err:
            raise ValueError(f"DNS resolution failure for host '{host}': {err}") from err

        if not addr_info:
            raise ValueError(f"DNS resolution failure for host '{host}': No address returned.")

        target_ip: str | None = None
        for item in addr_info:
            ip_str = item[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise ValueError(f"SSRF violation: Host '{host}' returned invalid IP '{ip_str}'.")

            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped

            str_ip = str(ip)
            if str_ip in BLOCKED_IPS:
                raise ValueError(
                    f"SSRF violation: Host '{host}' resolves to forbidden address {str_ip}."
                )

            if not self._allow_private:
                if not ip.is_global or (ip.version == 4 and ip in CGNAT_NETWORK):
                    raise ValueError(
                        f"SSRF violation: Host '{host}' resolves to forbidden address {str_ip}."
                    )
            else:
                if (
                    ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or ip.is_reserved
                    or ip.is_unspecified
                ):
                    raise ValueError(
                        f"SSRF violation: Host '{host}' resolves to forbidden address {str_ip}."
                    )

            if target_ip is None:
                target_ip = str_ip

        return await self._inner.connect_tcp(
            target_ip or host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> httpcore.AsyncNetworkStream:
        raise ValueError("Unix sockets are disabled for outbound webhooks.")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def create_ssrf_safe_client(
    timeout: float = 5.0,
    allow_private: bool | None = None,
) -> httpx.AsyncClient:
    """
    Constructs an httpx.AsyncClient equipped with Zero-Trust socket-level SSRF defense
    and redirect protection (follow_redirects=False).
    """
    transport = httpx.AsyncHTTPTransport(verify=True)
    transport._pool._network_backend = SSRFSafeBackend(
        transport._pool._network_backend,
        allow_private=allow_private,
    )
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=False,
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

    if allow_private is None:
        raw_val = os.environ.get("EPHEMRES_ALLOW_PRIVATE_WEBHOOKS") or os.environ.get("NETMON_ALLOW_PRIVATE_WEBHOOKS", "false")
        allow_private = raw_val.lower() in (
            "true",
            "1",
            "yes",
        )

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
        resolved_ips = []
        try:
            direct_ip = ipaddress.ip_address(hostname)
            resolved_ips.append(direct_ip)
        except ValueError:
            # Domain name: resolve via DNS
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
            addr_info = socket.getaddrinfo(
                hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            for item in addr_info:
                ip_str = item[4][0]
                resolved_ips.append(ipaddress.ip_address(ip_str))

        if not resolved_ips:
            raise ValueError(f"DNS resolution failure for host '{hostname}'.")

        for ip in resolved_ips:
            if getattr(ip, "ipv4_mapped", None):
                ip = ip.ipv4_mapped

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

            if not allow_private and (not ip.is_global or ip.is_private or (ip.version == 4 and ip in CGNAT_NETWORK)):
                raise ValueError(
                    f"SSRF violation: Host '{hostname}' resolves to non-global or private network address {str_ip}."
                )

    except socket.gaierror as err:
        raise ValueError(f"Failed to resolve host '{hostname}': {err}") from err
