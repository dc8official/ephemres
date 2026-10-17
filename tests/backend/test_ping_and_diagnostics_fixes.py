import asyncio
import struct
import socket
import pytest
from monitoring.ping import _RawICMPMultiplexer, _pack_icmp_echo
from app.services.diagnostics import _parse_trace_output
from app.core.netlink import get_local_interfaces_and_subnets, get_local_interface_networks


@pytest.mark.anyio
async def test_raw_icmp_multiplexer_ignores_wrong_source_ip():
    mux = _RawICMPMultiplexer(is_ipv6=False)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    mux._pending[(1234, 1)] = (fut, "1.1.1.1", 1000)

    # Craft an Echo Reply (type 0) with matching ident=1234, seq=1, ts=1000 from a spoofed IP 9.9.9.9
    reply_pkt = struct.pack("!BBHHH", 0, 0, 0, 1234, 1) + struct.pack("!Q", 1000)
    # Simulate _handle_incoming_packet(reply_pkt, "9.9.9.9")
    mux._handle_incoming_packet(reply_pkt, "9.9.9.9")
    assert not fut.done()

    # Now from expected IP 1.1.1.1
    mux._handle_incoming_packet(reply_pkt, "1.1.1.1")
    assert fut.done()


def test_parse_trace_output_supports_ipv6():
    sample = " 1  2001:db8::1 (2001:db8::1)  1.234 ms  1.100 ms  1.050 ms\n"
    hops = _parse_trace_output(sample, "2001:db8::2")
    assert len(hops) == 1
    assert hops[0].ip == "2001:db8::1"


def test_netlink_caching_and_alias():
    res1 = get_local_interfaces_and_subnets()
    res2 = get_local_interface_networks()
    assert res1 == res2
    assert len(res1) >= 1
