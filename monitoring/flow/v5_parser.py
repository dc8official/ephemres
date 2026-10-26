from __future__ import annotations

import socket
import struct
from typing import Any, List

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

        # 32-bit sysUpTime rollover compensation
        # sys_uptime is the current uptime in ms when packet was sent
        # first_ms / last_ms are uptimes in ms when flow started / ended
        def calc_flow_time(uptime_ms: int) -> float:
            diff = sys_uptime - uptime_ms
            if diff < 0:
                diff += 0x100000000  # 2^32 ms rollover
            # If device rebooted or diff is unreasonably large (> 30 days), fallback to unix_secs
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
