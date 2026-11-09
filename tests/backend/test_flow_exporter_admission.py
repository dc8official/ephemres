"""flowd keeps flows only from enrolled exporters.

The collector used to parse and buffer packets from any sender, and the aggregator stored the
flows of unknown exporters under the placeholder exporter id. Anyone who could reach UDP
2055/4739 could fill the flow buffer and the database, and spoofed v9/IPFIX senders could fill
the template caches. Packets from unknown senders are now dropped before they are parsed;
the sender is still recorded as an unmatched exporter candidate for the enrollment banner.
"""
from __future__ import annotations

import inspect
import socket
import struct
import time
from uuid import uuid4

from monitoring.flow.collector import FlowCollector
from monitoring.flow.correlator import FlowCorrelator

HDR = struct.Struct("!HHIIIIBBH")
REC = struct.Struct("!IIIHHIIIIHHBBBBHHBBH")


def _v5_packet(records: int) -> bytes:
    src = struct.unpack("!I", socket.inet_aton("10.0.0.1"))[0]
    dst = struct.unpack("!I", socket.inet_aton("10.0.0.2"))[0]
    body = b"".join(
        REC.pack(src, dst, 0, 1, 2, 10, 1500, 1000, 2000, 40000, 443, 0, 0x18, 6, 0, 0, 0, 24, 24, 0)
        for _ in range(records)
    )
    return HDR.pack(5, records, 3000, int(time.time()), 0, 0, 0, 0, 0) + body


def _correlator(*exporters: str) -> FlowCorrelator:
    corr = FlowCorrelator(db_session_factory=None, store_client=None)
    corr._exporter_map = {ip: uuid4() for ip in exporters}
    return corr


def test_packets_from_an_unknown_sender_are_dropped_before_parsing():
    corr = _correlator("192.0.2.1")
    kept = []
    collector = FlowCollector(sink=kept.extend, exporter_filter=corr.admit_exporter)

    collector.handle_packet(_v5_packet(30), ("198.51.100.66", 2055), 2055)
    assert kept == []

    collector.handle_packet(_v5_packet(30), ("192.0.2.1", 2055), 2055)
    assert len(kept) == 30


def test_unknown_sender_is_still_recorded_for_the_enrollment_banner():
    corr = _correlator()
    corr._unmatched_local["198.51.100.66"] = time.time()  # rate limit: count, don't write yet
    collector = FlowCollector(exporter_filter=corr.admit_exporter)

    collector.handle_packet(_v5_packet(30), ("198.51.100.66", 2055), 2055)
    collector.handle_packet(_v5_packet(12), ("198.51.100.66", 2055), 2055)

    # Counted in flows from the v5 header, so the banner's flow rate stays a flow rate.
    assert corr._unmatched_counts["198.51.100.66"] == 42


def test_filter_runs_before_the_v9_and_ipfix_template_caches():
    corr = _correlator()
    corr._unmatched_local["198.51.100.66"] = time.time()
    collector = FlowCollector(exporter_filter=corr.admit_exporter)
    seen = []
    collector.v9_parser.parse_packet = lambda *a: seen.append("v9") or []
    collector.ipfix_parser.parse_packet = lambda *a: seen.append("ipfix") or []

    collector.handle_packet(struct.pack("!HH", 9, 1) + b"\x00" * 16, ("198.51.100.66", 2055), 2055)
    collector.handle_packet(struct.pack("!HH", 10, 20) + b"\x00" * 16, ("198.51.100.66", 4739), 4739)
    assert seen == []
    assert corr._unmatched_counts["198.51.100.66"] == 2


def test_exporter_aliases_are_admitted():
    corr = _correlator("192.0.2.1", "192.0.2.254")
    assert corr.admit_exporter("192.0.2.254")
    assert corr.admit_exporter("192.0.2.1/32")
    assert not corr.admit_exporter("192.0.2.2")


def test_flowd_passes_the_correlator_as_the_filter():
    from monitoring.flow import daemon

    assert "exporter_filter=correlator.admit_exporter" in inspect.getsource(daemon.run_flow_daemon)
