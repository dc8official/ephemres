import socket
import pytest
from app.core.http_client import validate_outbound_url_async, validate_outbound_url


@pytest.mark.anyio
async def test_validate_outbound_url_async_public_ip():
    await validate_outbound_url_async("https://8.8.8.8/webhook", allow_private=False)
    await validate_outbound_url_async("https://1.1.1.1/api/v1", allow_private=False)


@pytest.mark.anyio
async def test_validate_outbound_url_async_invalid_inputs():
    with pytest.raises(ValueError, match="URL cannot be empty"):
        await validate_outbound_url_async("")

    with pytest.raises(ValueError, match="Invalid protocol"):
        await validate_outbound_url_async("ftp://example.com/test")

    with pytest.raises(ValueError, match="Invalid protocol"):
        await validate_outbound_url_async("file:///etc/passwd")

    with pytest.raises(ValueError, match="Missing hostname"):
        await validate_outbound_url_async("http:///path")


@pytest.mark.anyio
async def test_validate_outbound_url_async_blocked_and_private():
    # Cloud metadata
    with pytest.raises(ValueError, match="SSRF violation"):
        await validate_outbound_url_async("http://169.254.169.254/latest/meta-data")

    # Loopback
    with pytest.raises(ValueError, match="resolves to loopback address"):
        await validate_outbound_url_async("http://127.0.0.1:8000/api")

    # Loopback allowed
    await validate_outbound_url_async("http://127.0.0.1:8000/api", allow_loopback=True, allow_private=True)

    # Private IP
    with pytest.raises(ValueError, match="resolves to non-global or private"):
        await validate_outbound_url_async("http://10.0.0.1/webhook", allow_private=False)

    # Private IP allowed
    await validate_outbound_url_async("http://10.0.0.1/webhook", allow_private=True)


@pytest.mark.anyio
async def test_validate_outbound_url_async_dns_resolution(monkeypatch):
    called = False

    async def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        nonlocal called
        called = True
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    import asyncio
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    await validate_outbound_url_async("https://example.com/webhook", allow_private=False)
    assert called is True


def test_sync_validate_outbound_url_still_works():
    # Ensure sync validate_outbound_url is retained for sync callers
    validate_outbound_url("https://8.8.8.8/webhook", allow_private=False)
    with pytest.raises(ValueError, match="resolves to loopback address"):
        validate_outbound_url("http://127.0.0.1:8000/api")
