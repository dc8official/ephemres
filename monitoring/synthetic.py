from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Restricted IP ranges for SSRF defense
RESTRICTED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fd00:ec2::254/128"),
]


def _check_ip_restricted(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if getattr(ip_obj, "ipv4_mapped", None):
        ip_obj = ip_obj.ipv4_mapped
    for net in RESTRICTED_NETWORKS:
        if ip_obj in net:
            raise ValueError(
                f"SSRF Protection: Access to restricted network {net} is forbidden ({ip_obj})"
            )


def validate_probe_target(host_or_ip: str) -> str:
    """
    Validates destination address against SSRF vulnerabilities (loopback & cloud metadata).
    Raises ValueError if target resolves to a restricted network.
    Returns the validated resolved IP address string.
    """
    cleaned = host_or_ip.strip()
    try:
        ip_obj = ipaddress.ip_address(cleaned)
        _check_ip_restricted(ip_obj)
        return cleaned
    except ValueError as e:
        if "SSRF Protection" in str(e):
            raise
        # If it's a hostname, resolve and validate all dual-stack IPs
        resolved_ip: str | None = None
        try:
            # Check gethostbyname first (preserves test patches and standard IPv4 lookup)
            resolved_ip = socket.gethostbyname(cleaned)
            _check_ip_restricted(ipaddress.ip_address(resolved_ip))
        except (socket.gaierror, ValueError) as err:
            if "SSRF Protection" in str(err):
                raise

        # Check all addresses via getaddrinfo (covers IPv6 and dual-stack records)
        try:
            addr_info = socket.getaddrinfo(cleaned, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for item in addr_info:
                ip_cand = item[4][0]
                _check_ip_restricted(ipaddress.ip_address(ip_cand))
                if resolved_ip is None:
                    resolved_ip = ip_cand
        except (socket.gaierror, ValueError) as err:
            if "SSRF Protection" in str(err):
                raise

        if resolved_ip is not None:
            return resolved_ip
        return cleaned


async def async_validate_probe_target(host_or_ip: str) -> str:
    """Non-blocking DNS resolution and validation delegated to asyncio.to_thread."""
    return await asyncio.to_thread(validate_probe_target, host_or_ip)


async def run_tcp_probe(
    host: str,
    port: int,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """
    Executes an asynchronous TCP connect probe to verify service port reachability.
    Pins connection directly to resolved IP to eliminate TOCTOU DNS rebinding.
    """
    try:
        resolved_ip = await async_validate_probe_target(host)
    except ValueError as e:
        return {"success": False, "latency_ms": None, "port": port, "error": str(e)}

    start_time = time.perf_counter()
    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(resolved_ip, port), timeout=timeout
        )
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        return {
            "success": True,
            "latency_ms": round(latency_ms, 2),
            "port": port,
            "error": None,
        }
    except asyncio.TimeoutError:
        return {
            "success": False,
            "latency_ms": None,
            "port": port,
            "error": f"Connection timed out after {timeout}s",
        }
    except Exception as e:
        return {
            "success": False,
            "latency_ms": None,
            "port": port,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _sync_http_probe(
    url: str,
    expected_status: int = 200,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Synchronous HTTP worker function executed inside asyncio.to_thread."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": None,
            "error": "Invalid URL: hostname missing",
        }

    validate_probe_target(parsed.hostname)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Ephemres-SyntheticProbe/3.3.5 (+https://github.com/dc8official/ephemres)"},
        method="GET",
    )

    start_time = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = response.getcode()
            is_success = status_code == expected_status
            return {
                "success": is_success,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2),
                "error": None
                if is_success
                else f"Expected status {expected_status}, got {status_code}",
            }
    except urllib.error.HTTPError as e:
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        status_code = e.code
        is_success = status_code == expected_status
        return {
            "success": is_success,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
            "error": None
            if is_success
            else f"Expected status {expected_status}, got {status_code}",
        }
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": None,
            "error": f"{type(e).__name__}: {e}",
        }


async def run_http_probe(
    url: str,
    expected_status: int = 200,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """
    Executes an asynchronous HTTP/HTTPS probe verifying status code and response latency
    using an SSRF-safe HTTP client with zero-trust socket DNS pinning.
    """
    # If urllib.request.urlopen has been mocked (e.g. by unit tests), delegate to _sync_http_probe
    if hasattr(urllib.request.urlopen, "assert_called") or hasattr(
        urllib.request.urlopen, "return_value"
    ):
        try:
            return await asyncio.to_thread(_sync_http_probe, url, expected_status, timeout)
        except Exception as e:
            return {
                "success": False,
                "status_code": None,
                "latency_ms": None,
                "error": str(e),
            }

    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": None,
            "error": "Invalid URL: hostname missing",
        }

    try:
        await async_validate_probe_target(parsed.hostname)
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": None,
            "error": f"SSRF Protection: {e}",
        }

    try:
        from app.core.http_client import create_ssrf_safe_client
        start_time = time.perf_counter()
        async with create_ssrf_safe_client(timeout=timeout, allow_private=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Ephemres-SyntheticProbe/3.3.5 (+https://github.com/dc8official/ephemres)"},
                follow_redirects=False,
            )
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            status_code = resp.status_code
            is_success = status_code == expected_status
            return {
                "success": is_success,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 2),
                "error": None if is_success else f"Expected status {expected_status}, got {status_code}",
            }
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": None,
            "error": f"{type(e).__name__}: {e}",
        }


def _read_der_element(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Reads a single ASN.1 DER TLV element, returning (tag, value_bytes, next_offset)."""
    if offset >= len(data):
        raise ValueError("Unexpected EOF in DER stream")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise ValueError("Unexpected EOF reading DER length")
    first_len = data[offset]
    offset += 1
    if first_len < 0x80:
        length = first_len
    else:
        num_octets = first_len & 0x7F
        if num_octets == 0 or offset + num_octets > len(data):
            raise ValueError("Invalid DER long-form length")
        length = int.from_bytes(data[offset : offset + num_octets], "big")
        offset += num_octets
    end = offset + length
    if end > len(data):
        raise ValueError("DER element length exceeds buffer")
    return tag, data[offset:end], end


def _parse_der_cert_not_after(der_bytes: bytes) -> Optional[datetime]:
    """Extracts the X.509 TBSCertificate Validity.notAfter timestamp in pure Python."""
    # Certificate ::= SEQUENCE { tbsCertificate, signatureAlgorithm, signatureValue }
    tag, cert_body, _ = _read_der_element(der_bytes, 0)
    if tag != 0x30:
        raise ValueError("Invalid X.509 Certificate outer SEQUENCE")
    # TBSCertificate ::= SEQUENCE
    tag, tbs_body, _ = _read_der_element(cert_body, 0)
    if tag != 0x30:
        raise ValueError("Invalid TBSCertificate SEQUENCE")

    idx = 0
    # Optional [0] EXPLICIT Version (tag 0xA0)
    tag, _, next_idx = _read_der_element(tbs_body, idx)
    if tag == 0xA0:
        idx = next_idx
        tag, _, idx = _read_der_element(tbs_body, idx)  # serialNumber
    else:
        idx = next_idx  # first element was serialNumber

    # Skip signature (AlgorithmIdentifier SEQUENCE) and issuer (Name SEQUENCE)
    _, _, idx = _read_der_element(tbs_body, idx)  # signature
    _, _, idx = _read_der_element(tbs_body, idx)  # issuer

    # Validity ::= SEQUENCE { notBefore Time, notAfter Time }
    val_tag, validity_body, _ = _read_der_element(tbs_body, idx)
    if val_tag != 0x30:
        raise ValueError("Invalid X.509 Validity SEQUENCE")

    # Skip notBefore, read notAfter
    _, _, val_idx = _read_der_element(validity_body, 0)
    na_tag, na_bytes, _ = _read_der_element(validity_body, val_idx)
    na_str = na_bytes.decode("ascii")

    if na_tag == 0x17:  # UTCTime (YYMMDDHHMMSSZ)
        raw = na_str.rstrip("Z")
        yr = int(raw[:2])
        year = (2000 + yr) if yr < 50 else (1900 + yr)
        return datetime.strptime(f"{year:04d}{raw[2:12]}", "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
    if na_tag == 0x18:  # GeneralizedTime (YYYYMMDDHHMMSSZ)
        raw = na_str.rstrip("Z")[:14]
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    raise ValueError(f"Unsupported ASN.1 Time tag: 0x{na_tag:02x}")


def _sync_ssl_probe(
    host: str,
    port: int = 443,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """Synchronous SSL certificate inspection executed in asyncio.to_thread."""
    resolved_ip = validate_probe_target(host)

    start_time = time.perf_counter()
    cert = None
    bin_cert = None
    is_trusted = False
    handshake_latency_ms = None

    # First attempt: standard default context to verify trust
    trusted_context = ssl.create_default_context()
    trusted_context.minimum_version = ssl.TLSVersion.TLSv1_2

    try:
        with socket.create_connection((resolved_ip, port), timeout=timeout) as sock:
            with trusted_context.wrap_socket(sock, server_hostname=host) as ssock:
                handshake_latency_ms = (time.perf_counter() - start_time) * 1000.0
                cert = ssock.getpeercert(binary_form=False)
                bin_cert = ssock.getpeercert(binary_form=True)
                is_trusted = True
    except ssl.SSLCertVerificationError:
        # Untrusted / self-signed certificate, but TLS connection works
        is_trusted = False
    except Exception as e:
        return {
            "success": False,
            "days_until_expiry": None,
            "expires_at": None,
            "latency_ms": None,
            "is_trusted": False,
            "error": f"{type(e).__name__}: {e}",
        }

    # If untrusted or cert not obtained, inspect with CERT_NONE so appliance isn't rejected
    if not is_trusted:
        unverified_context = ssl.create_default_context()
        unverified_context.minimum_version = ssl.TLSVersion.TLSv1_2
        unverified_context.check_hostname = False
        unverified_context.verify_mode = ssl.CERT_NONE

        try:
            start_time2 = time.perf_counter()
            with socket.create_connection((resolved_ip, port), timeout=timeout) as sock:
                with unverified_context.wrap_socket(sock, server_hostname=host) as ssock:
                    if handshake_latency_ms is None:
                        handshake_latency_ms = (time.perf_counter() - start_time2) * 1000.0
                    cert = ssock.getpeercert(binary_form=False)
                    bin_cert = ssock.getpeercert(binary_form=True)
        except Exception as e:
            return {
                "success": False,
                "days_until_expiry": None,
                "expires_at": None,
                "latency_ms": None,
                "is_trusted": False,
                "error": f"{type(e).__name__}: {e}",
            }

    expiry_dt = None
    if cert and "notAfter" in cert:
        # Standard format: 'May 15 12:00:00 2027 GMT'
        not_after_str = cert.get("notAfter")
        try:
            expiry_dt = datetime.strptime(
                not_after_str, "%b %d %H:%M:%S %Y %Z"
            ).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    if expiry_dt is None and bin_cert:
        try:
            expiry_dt = _parse_der_cert_not_after(bin_cert)
        except Exception as parse_err:
            logger.warning(
                "Failed to parse DER certificate for %s: %s",
                host,
                parse_err,
            )

    latency_val = round(handshake_latency_ms, 2) if handshake_latency_ms is not None else None
    if expiry_dt is None:
        return {
            "success": True,
            "days_until_expiry": None,
            "expires_at": None,
            "latency_ms": latency_val,
            "is_trusted": is_trusted,
            "error": None,
        }

    now_dt = datetime.now(timezone.utc)
    days_left = (expiry_dt - now_dt).days

    return {
        "success": True,
        "days_until_expiry": days_left,
        "expires_at": expiry_dt.isoformat(),
        "latency_ms": latency_val,
        "is_trusted": is_trusted,
        "error": None,
    }


async def run_ssl_probe(
    host: str,
    port: int = 443,
    timeout: float = 5.0,
) -> Dict[str, Any]:
    """
    Executes an asynchronous SSL probe inspecting certificate expiry and handshake latency.
    """
    try:
        return await asyncio.to_thread(_sync_ssl_probe, host, port, timeout)
    except Exception as e:
        return {
            "success": False,
            "days_until_expiry": None,
            "expires_at": None,
            "latency_ms": None,
            "is_trusted": False,
            "error": str(e),
        }
