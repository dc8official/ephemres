"""Reply TTL beyond raw IPv4 (build plan 6.5, done 2026-11-14).

Raw IPv4 reads a reply's TTL from the IP header. Raw ICMPv6 and the unprivileged ping socket get
no header, so until v3.3.6 their replies had no TTL and the TTL route trigger (ttl_watch) never ran
for IPv6 endpoints or on servers without CAP_NET_RAW. They now ask the kernel for it as ancillary
data (IP_RECVTTL / IPV6_RECVHOPLIMIT).
"""
import asyncio
import socket
import struct
from unittest.mock import MagicMock

import pytest

from monitoring import ping


def test_ttl_is_read_from_ancillary_data():
    v4 = [(socket.IPPROTO_IP, socket.IP_TTL, struct.pack("i", 57))]
    v6 = [(socket.IPPROTO_IPV6, socket.IPV6_HOPLIMIT, struct.pack("i", 63))]
    assert ping.ttl_from_ancdata(v4) == 57
    assert ping.ttl_from_ancdata(v6) == 63
    # Some kernels hand IP_TTL over as one byte.
    assert ping.ttl_from_ancdata([(socket.IPPROTO_IP, socket.IP_TTL, bytes([120]))]) == 120
    # Anything else is not a TTL.
    assert ping.ttl_from_ancdata([(socket.IPPROTO_IP, socket.IP_TOS, struct.pack("i", 4))]) is None
    assert ping.ttl_from_ancdata([]) is None


def test_asking_for_the_ttl_uses_the_right_option_and_survives_a_refusal():
    sock = MagicMock()
    assert ping.enable_reply_ttl(sock, is_ipv6=True)
    sock.setsockopt.assert_called_with(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
    assert ping.enable_reply_ttl(sock, is_ipv6=False)
    # Python 3.12 doesn't export IP_RECVTTL: its Linux value is used.
    sock.setsockopt.assert_called_with(socket.IPPROTO_IP, getattr(socket, "IP_RECVTTL", 12), 1)
    sock.setsockopt.side_effect = OSError("not supported")
    assert not ping.enable_reply_ttl(sock, is_ipv6=True)


def test_a_raw_ipv6_reply_keeps_the_hop_limit_it_came_with():
    loop = asyncio.new_event_loop()
    try:
        mux = ping._RawICMPMultiplexer.__new__(ping._RawICMPMultiplexer)
        mux.loop, mux.is_ipv6, mux._closed = loop, True, False
        mux._pending, mux._reply_ttls = {}, {}
        fut = loop.create_future()
        mux._pending[(7, 1)] = (fut, "2001:db8::1", 1000)
        reply = ping.build_icmp_echo_packet(7, 1, send_ns=1000, is_ipv6=True)
        reply = bytes([129]) + reply[1:]  # echo reply, no IP header (raw ICMPv6)
        mux._handle_incoming_packet(reply, "2001:db8::1", recv_ns=2_000_000, reply_ttl=58)
        assert fut.done() and mux._reply_ttls[(7, 1)] == 58
    finally:
        loop.close()


def test_the_unprivileged_ping_socket_reports_the_ttl():
    """Live, against loopback; skipped where the kernel doesn't allow ping sockets for this user."""
    for family in (socket.AF_INET, socket.AF_INET6):
        proto = socket.IPPROTO_ICMP if family == socket.AF_INET else socket.IPPROTO_ICMPV6
        try:
            socket.socket(family, socket.SOCK_DGRAM, proto).close()
        except OSError:
            pytest.skip("ping sockets not allowed here (net.ipv4.ping_group_range)")

    async def cycle(ip, is_ipv6):
        return await ping._run_dgram_socket_ping_cycle(ip, 2, 0.05, 1.0, is_ipv6)

    for ip, is_ipv6 in (("127.0.0.1", False), ("::1", True)):
        try:
            result = asyncio.run(cycle(ip, is_ipv6))
        except OSError:
            pytest.skip(f"no {ip} here")
        assert result.avg_rtt_ms is not None, ip
        assert result.reply_ttl == 64, (ip, result.reply_ttl)  # Linux's default TTL, nothing on the way
