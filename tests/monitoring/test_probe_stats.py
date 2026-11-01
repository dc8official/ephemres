"""Per-cycle RTT statistics recorded for the probe history (v3.4 Phase 2)."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest

from monitoring.ping import PingResult, result_from_rtts, run_system_ping_fallback
from monitoring.probe_samples import sample_params

LINUX_PING_OUTPUT = b"""PING 10.0.0.1 (10.0.0.1) 56(84) bytes of data.
64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=10.2 ms
64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=14.6 ms
64 bytes from 10.0.0.1: icmp_seq=4 ttl=64 time=12.1 ms

--- 10.0.0.1 ping statistics ---
4 packets transmitted, 3 received, 25% packet loss, time 3004ms
rtt min/avg/max/mdev = 10.200/12.300/14.600/1.798 ms
"""


def test_result_from_rtts_spread_and_jitter():
    result = result_from_rtts([10.0, 14.0, 12.0, 20.0], count=5)
    assert (result.success_count, result.failed_count) == (4, 1)
    assert result.avg_rtt_ms == 14.0
    assert (result.min_rtt_ms, result.max_rtt_ms) == (10.0, 20.0)
    # |14-10| + |12-14| + |20-12| = 14 over 3 gaps
    assert result.jitter_ms == pytest.approx(14 / 3, abs=0.001)


def test_result_from_rtts_single_reply_has_no_jitter():
    result = result_from_rtts([7.5], count=5)
    assert (result.avg_rtt_ms, result.min_rtt_ms, result.max_rtt_ms, result.jitter_ms) == (7.5, 7.5, 7.5, None)


def test_result_from_rtts_total_loss():
    result = result_from_rtts([], count=5)
    assert (result.success_count, result.failed_count) == (0, 5)
    assert (result.avg_rtt_ms, result.min_rtt_ms, result.max_rtt_ms, result.jitter_ms) == (None, None, None, None)
    assert result.health_score == 0.0


def test_ping_result_keeps_old_three_field_constructor():
    result = PingResult(success_count=5, failed_count=0, avg_rtt_ms=3.0)
    assert (result.min_rtt_ms, result.max_rtt_ms, result.jitter_ms) == (None, None, None)


@patch("asyncio.create_subprocess_exec")
def test_system_ping_fallback_uses_individual_replies(mock_exec):
    proc = AsyncMock()
    proc.communicate.return_value = (LINUX_PING_OUTPUT, b"")
    mock_exec.return_value = proc

    result = asyncio.run(run_system_ping_fallback("10.0.0.1", 4, 1.0, 1.0))
    assert (result.success_count, result.failed_count) == (3, 1)
    assert (result.min_rtt_ms, result.max_rtt_ms) == (10.2, 14.6)
    assert result.avg_rtt_ms == pytest.approx(12.3, abs=0.001)
    assert result.jitter_ms == pytest.approx((4.4 + 2.5) / 2, abs=0.001)


@patch("asyncio.create_subprocess_exec")
def test_system_ping_fallback_without_reply_lines_keeps_summary_avg(mock_exec):
    proc = AsyncMock()
    proc.communicate.return_value = (
        b"4 packets transmitted, 4 received\nrtt min/avg/max/mdev = 1.0/2.5/4.0/0.5 ms\n",
        b"",
    )
    mock_exec.return_value = proc

    result = asyncio.run(run_system_ping_fallback("10.0.0.1", 4, 1.0, 1.0))
    assert (result.success_count, result.avg_rtt_ms, result.jitter_ms) == (4, 2.5, None)


def test_sample_params():
    ts = datetime(2026, 10, 26, 9, 30, tzinfo=timezone.utc)
    ep = UUID("11111111-0000-0000-0000-000000000001")
    params = sample_params(ep, ts, result_from_rtts([2.0, 4.0], count=5))
    assert params == {
        "endpoint_id": str(ep),
        "sampled_at": ts,
        "sent": 5,
        "received": 2,
        "rtt_min_ms": 2.0,
        "rtt_avg_ms": 3.0,
        "rtt_max_ms": 4.0,
        "jitter_ms": 2.0,
    }


def test_missing_raw_socket_falls_back_instead_of_reporting_total_loss():
    """Without CAP_NET_RAW the raw multiplexer has no socket. The cycle must use the next tier,
    not wait out every probe and report 100 % loss (pre-v3.4 behaviour)."""
    from types import SimpleNamespace

    from monitoring import ping

    fallback = PingResult(success_count=5, failed_count=0, avg_rtt_ms=1.0)
    with patch.object(ping, "_get_raw_multiplexer", return_value=SimpleNamespace(sock=None)), \
         patch.object(ping, "_run_dgram_socket_ping_cycle", AsyncMock(return_value=fallback)) as dgram:
        result = asyncio.run(ping.run_ping_cycle("127.0.0.1", 5, 0.0, 0.1, privileged=True))
    assert result is fallback
    dgram.assert_awaited_once()
