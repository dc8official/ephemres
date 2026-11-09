"""NetFlow v5 decoder. v5 has a fixed layout (no templates): a 24-byte header, then `count`
48-byte records, IPv4 only."""
from __future__ import annotations

import socket
import struct
from typing import Any, List

# Header: version, count, sys_uptime (ms), unix_secs, unix_nsecs, flow_sequence, engine_type,
# engine_id, sampling. Record: src, dst, next hop, in/out ifIndex, packets, bytes, first/last
# (sys_uptime in ms), ports, pad, TCP flags, protocol, ToS, src/dst AS, src/dst mask, pad.
HEADER_STRUCT = struct.Struct("!HHIIIIBBH")
RECORD_STRUCT = struct.Struct("!IIIHHIIIIHHBBBBHHBBH")
HEADER_SIZE = HEADER_STRUCT.size  # 24 bytes
RECORD_SIZE = RECORD_STRUCT.size  # 48 bytes


def parse_v5_packet(data: bytes, exporter_ip: str) -> List[dict[str, Any]]:
    """
    Parses a NetFlow v5 UDP payload into normalized flow dictionaries.
    
    Handles:
    - 24-byte header validation and record extraction.
    - Bitmask sampling (0x3FFF interval mask and multiplier calculation).
    - 32-bit sysUpTime rollover math for precise flow timestamps.
    """
    if len(data) < HEADER_SIZE:
        return []

    (
        version,
        count,
        sys_uptime,
        unix_secs,
        unix_nsecs,
        flow_seq,
        engine_type,
        engine_id,
        raw_sampling,
    ) = HEADER_STRUCT.unpack_from(data, 0)

    if version != 5 or count == 0:
        return []

    # Sampling bitmask: top 2 bits mode, bottom 14 bits interval
    sampling_interval = raw_sampling & 0x3FFF
    multiplier = sampling_interval if sampling_interval > 0 else 1

    expected_len = HEADER_SIZE + (count * RECORD_SIZE)
    # A truncated datagram keeps the records that arrived whole.
    actual_records = min(count, (len(data) - HEADER_SIZE) // RECORD_SIZE)

    flows: List[dict[str, Any]] = []
    offset = HEADER_SIZE

    for _ in range(actual_records):
        (
            src_ip_int,
            dst_ip_int,
            nexthop_int,
            input_if,
            output_if,
            d_pkts,
            d_octets,
            first_ms,
            last_ms,
            src_port,
            dst_port,
            _pad1,
            tcp_flags,
            protocol,
            tos,
            src_as,
            dst_as,
            src_mask,
            dst_mask,
            _pad2,
        ) = RECORD_STRUCT.unpack_from(data, offset)
        offset += RECORD_SIZE

        # Convert IP integers to string
        src_ip = socket.inet_ntoa(struct.pack("!I", src_ip_int))
        dst_ip = socket.inet_ntoa(struct.pack("!I", dst_ip_int))
        # 0.0.0.0: delivered locally or not routed.
        next_hop = socket.inet_ntoa(struct.pack("!I", nexthop_int)) if nexthop_int else None

        # Flow times come as router uptime in ms; the header gives the uptime and the wall-clock
        # time when the packet was sent, so a flow's time is send time minus how long ago it was.
        def calc_flow_time(uptime_ms: int) -> float:
            diff = sys_uptime - uptime_ms
            if diff < 0:
                # The 32-bit uptime counter wrapped (every 2^32 ms, about 49.7 days) in between.
                diff += 0x100000000
            # Over 30 days ago can't be a live flow (a reboot or a bad counter): use the send time.
            if diff > 2592000000:
                return float(unix_secs)
            return float(unix_secs) - (diff / 1000.0)

        start_time = calc_flow_time(first_ms)
        end_time = calc_flow_time(last_ms)

        scaled_bytes = d_octets * multiplier
        scaled_packets = d_pkts * multiplier

        flows.append(
            {
                "version": 5,
                "exporter_ip": exporter_ip,
                "src_ip": src_ip,
                "dst_ip": dst_ip,
                "src_port": src_port,
                "dst_port": dst_port,
                "protocol": protocol,
                "bytes": scaled_bytes,
                "packets": scaled_packets,
                # Header carried a sampling interval and counters are already scaled by it.
                "header_sampled": sampling_interval > 0,
                "tcp_flags": tcp_flags,
                "tos": tos,
                "input_if": input_if,
                "output_if": output_if,
                "next_hop": next_hop,
                "start_time": start_time,
                "end_time": end_time,
            }
        )

    return flows
