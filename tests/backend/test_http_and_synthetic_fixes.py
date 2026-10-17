import pytest
from app.core.http_client import _decode_chunked_body, SSRFSafeHttpClient
from monitoring.synthetic import validate_probe_target


def test_decode_chunked_body_negative_chunk_size_does_not_hang():
    malformed = b"-2\r\nXX\r\n0\r\n\r\n"
    res = _decode_chunked_body(malformed)
    assert res == b""


def test_validate_probe_target_blocks_ipv4_mapped_ipv6_and_metadata():
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("::ffff:169.254.169.254")
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("::ffff:127.0.0.1")
    with pytest.raises(ValueError, match="SSRF Protection"):
        validate_probe_target("fd00:ec2::254")


@pytest.mark.anyio
async def test_ssrf_http_client_get_accepts_follow_redirects():
    client = SSRFSafeHttpClient(timeout=2.0)
    # Verify signature accepts follow_redirects and timeout without TypeError
    import inspect
    sig = inspect.signature(client.get)
    assert "follow_redirects" in sig.parameters
    assert "timeout" in sig.parameters

    sig_req = inspect.signature(client.request)
    assert "follow_redirects" in sig_req.parameters
    assert "timeout" in sig_req.parameters
