"""sFlow v5 (v3.3.6 build plan 8.2): the decoder and its place in the flow collector.

The datagrams are built here by hand from the sFlow v5 layout, so the tests check the decoder
against the specification's field order and not against its own encoder.
"""
import socket
import struct

import pytest

from monitoring.flow.collector import FlowCollector
from monitoring.flow.sflow_parser import SflowError, is_sflow, parse_sflow_packet, sample_count


def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def _tcp_packet(src="10.1.1.10", dst="203.0.113.9", sport=51000, dport=443, flags=0x12, total_length=1500, tos=0xB8):
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, tos, total_length, 1, 0, 64, 6, 0,
                     socket.inet_aton(src), socket.inet_aton(dst))
    tcp = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, flags, 8192, 0, 0)
    return ip + tcp


def _ethernet(payload: bytes, ethertype=0x0800, vlan=None) -> bytes:
    tag = struct.pack("!HH", 0x8100, vlan) if vlan is not None else b""
    return b"\xaa" * 6 + b"\xbb" * 6 + tag + struct.pack("!H", ethertype) + payload


def _raw_header_record(header: bytes, frame_length=1518, header_protocol=1) -> bytes:
    body = struct.pack("!IIII", header_protocol, frame_length, 4, len(header)) + _pad(header)
    return struct.pack("!II", 1, len(body)) + body


def _router_record(next_hop="192.0.2.1") -> bytes:
    body = struct.pack("!I", 1) + socket.inet_aton(next_hop) + struct.pack("!II", 24, 24)
    return struct.pack("!II", 1002, len(body)) + body


def _flow_sample(records, rate=1000, input_if=3, output_if=7) -> bytes:
    body = struct.pack("!8I", 1, 3, rate, 5000, 0, input_if, output_if, len(records)) + b"".join(records)
    return struct.pack("!II", 1, len(body)) + body


def _expanded_sample(records, rate=1000, input_if=3, output_if=7, out_format=0) -> bytes:
    body = struct.pack("!11I", 1, 0, 3, rate, 5000, 0, 0, input_if, out_format, output_if, len(records)) + b"".join(records)
    return struct.pack("!II", 3, len(body)) + body


def _counter_sample() -> bytes:
    body = struct.pack("!III", 1, 3, 0)
    return struct.pack("!II", 2, len(body)) + body


def _datagram(samples, agent="192.0.2.50") -> bytes:
    return (struct.pack("!II", 5, 1) + socket.inet_aton(agent)
            + struct.pack("!IIII", 0, 42, 123456, len(samples)) + b"".join(samples))


def test_a_sampled_tcp_packet_is_scaled_by_its_own_rate():
    data = _datagram([_flow_sample([_raw_header_record(_ethernet(_tcp_packet())), _router_record()], rate=1000)])
    assert is_sflow(data)
    (flow,) = parse_sflow_packet(data, "192.0.2.50", now=1_700_000_000.0)
    assert (flow["src_ip"], flow["dst_ip"]) == ("10.1.1.10", "203.0.113.9")
    assert (flow["src_port"], flow["dst_port"], flow["protocol"]) == (51000, 443, 6)
    assert (flow["tcp_flags"], flow["tos"]) == (0x12, 0xB8)
    # One packet in a thousand: 1500 IP bytes (not the 1518-byte frame) stands for 1.5 MB.
    assert (flow["bytes"], flow["packets"]) == (1_500_000, 1000)
    assert (flow["input_if"], flow["output_if"], flow["next_hop"]) == (3, 7, "192.0.2.1")
    assert flow["header_sampled"] is True
    assert flow["start_time"] == flow["end_time"] == 1_700_000_000.0
    assert flow["exporter_ip"] == "192.0.2.50"


def test_each_sample_keeps_its_own_rate():
    data = _datagram([
        _flow_sample([_raw_header_record(_ethernet(_tcp_packet(total_length=100)))], rate=10),
        _counter_sample(),  # skipped: interface counters come from SNMP
        _expanded_sample([_raw_header_record(_ethernet(_tcp_packet(total_length=200)))], rate=4000),
    ])
    assert sample_count(data) == 3
    a, b = parse_sflow_packet(data, "192.0.2.50")
    assert (a["bytes"], a["packets"]) == (1_000, 10)
    assert (b["bytes"], b["packets"]) == (800_000, 4000)


def test_vlan_tag_ipv6_and_bare_ip_headers_are_read():
    v6 = (struct.pack("!IHBB", 0x6B800000, 20, 17, 64) + socket.inet_pton(socket.AF_INET6, "2001:db8::1")
          + socket.inet_pton(socket.AF_INET6, "2001:db8::2") + struct.pack("!HHHH", 5353, 53, 20, 0))
    data = _datagram([
        _flow_sample([_raw_header_record(_ethernet(_tcp_packet(), vlan=120))]),
        _flow_sample([_raw_header_record(_ethernet(v6, ethertype=0x86DD))]),
        _flow_sample([_raw_header_record(_tcp_packet(dport=22), header_protocol=11)]),
    ])
    tagged, ipv6, bare = parse_sflow_packet(data, "192.0.2.50")
    assert tagged["dst_port"] == 443
    assert (ipv6["src_ip"], ipv6["dst_ip"], ipv6["protocol"]) == ("2001:db8::1", "2001:db8::2", 17)
    assert (ipv6["src_port"], ipv6["dst_port"], ipv6["tos"]) == (5353, 53, 0xB8)
    assert ipv6["bytes"] == 60 * 1000  # payload 20 + the 40-byte IPv6 header
    assert bare["dst_port"] == 22


def test_pre_decoded_ipv4_record():
    body = (struct.pack("!II", 576, 17) + socket.inet_aton("10.0.0.5") + socket.inet_aton("10.0.0.9")
            + struct.pack("!IIII", 40000, 161, 0, 0))
    data = _datagram([_flow_sample([struct.pack("!II", 3, len(body)) + body], rate=256)])
    (flow,) = parse_sflow_packet(data, "192.0.2.50")
    assert (flow["src_ip"], flow["dst_port"], flow["protocol"], flow["bytes"]) == ("10.0.0.5", 161, 17, 576 * 256)


def test_what_is_not_a_readable_ip_packet_is_left_out():
    arp = _ethernet(b"\x00" * 28, ethertype=0x0806)
    fragment = bytearray(_tcp_packet())
    fragment[6:8] = struct.pack("!H", 185)  # a later fragment: no transport header in it
    data = _datagram([
        _flow_sample([_raw_header_record(arp)]),
        _flow_sample([_raw_header_record(_ethernet(bytes(fragment)))]),
        # Dropped packet (format 1) and "unknown" input: neither is an interface.
        _flow_sample([_raw_header_record(_ethernet(_tcp_packet()))], input_if=0x3FFFFFFF, output_if=(1 << 30) | 258),
        _flow_sample([_raw_header_record(_ethernet(_tcp_packet()))], rate=0),
    ])
    frag, dropped, unrated = parse_sflow_packet(data, "192.0.2.50")
    assert (frag["src_port"], frag["dst_port"], frag["tcp_flags"]) == (0, 0, 0)
    assert (dropped["input_if"], dropped["output_if"]) == (0, 0)
    # No rate given: counted as one packet, and left for the admin's multiplier to scale.
    assert (unrated["packets"], unrated["header_sampled"]) == (1, False)


def test_truncated_and_forged_datagrams_do_not_raise_or_spin():
    good = _flow_sample([_raw_header_record(_ethernet(_tcp_packet()))])
    data = _datagram([good, good])
    # Cut in the second sample: the first is kept.
    assert len(parse_sflow_packet(data[:-20], "192.0.2.50")) == 1
    # A header promising four billion samples over an empty body.
    forged = struct.pack("!II", 5, 1) + socket.inet_aton("192.0.2.50") + struct.pack("!IIII", 0, 1, 1, 0xFFFFFFFF)
    assert parse_sflow_packet(forged, "192.0.2.50") == []
    assert sample_count(forged) == 256
    # A record length that runs past its sample.
    bad_record = struct.pack("!8I", 1, 3, 100, 0, 0, 1, 2, 1) + struct.pack("!II", 1, 9999)
    assert parse_sflow_packet(_datagram([struct.pack("!II", 1, len(bad_record)) + bad_record]), "192.0.2.50") == []
    with pytest.raises(SflowError):
        parse_sflow_packet(struct.pack("!HH", 5, 1) + b"\x00" * 40, "192.0.2.50")  # NetFlow v5


def test_netflow_and_ipfix_are_never_taken_for_sflow():
    for version in (5, 9, 10):
        assert not is_sflow(struct.pack("!HH", version, 1) + b"\x00" * 60)


def _collector(admitted=("192.0.2.50",)):
    got, asked = [], []

    def admit(ip, records):
        asked.append((ip, records))
        return ip in admitted

    return FlowCollector(sink=got.extend, exporter_filter=admit), got, asked


def test_collector_reads_sflow_on_any_port_and_marks_the_exporter_sampled(monkeypatch):
    # The admin multiplier must not be applied on top of the sample's own rate.
    monkeypatch.setattr("monitoring.flow.collector.get_runtime_settings",
                        lambda: type("S", (), {"flow_sampling_multiplier": 50})())
    collector, got, asked = _collector()
    data = _datagram([_flow_sample([_raw_header_record(_ethernet(_tcp_packet()))], rate=1000)])
    collector.handle_packet(data, ("192.0.2.50", 40000), 2055)  # sent to the NetFlow port
    assert asked == [("192.0.2.50", 1)]
    assert [f["bytes"] for f in got] == [1_500_000]
    assert list(collector.sampled_exporters) == ["192.0.2.50"]


def test_collector_drops_sflow_from_a_sender_that_is_not_enrolled():
    collector, got, asked = _collector(admitted=())
    data = _datagram([_flow_sample([_raw_header_record(_ethernet(_tcp_packet()))])] * 2)
    collector.handle_packet(data, ("198.51.100.66", 40000), 6343)
    assert got == [] and asked == [("198.51.100.66", 2)]
    assert collector.sampled_exporters == {}
