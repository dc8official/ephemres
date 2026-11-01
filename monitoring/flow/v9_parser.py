from __future__ import annotations

import collections
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

HEADER_STRUCT = struct.Struct("!HHIIII")
FLOWSET_HEADER_STRUCT = struct.Struct("!HH")

# Standard Field Types (RFC 3954)
FT_IN_BYTES = 1
FT_IN_PKTS = 2
FT_FLOWS = 3
FT_PROTOCOL = 4
FT_SRC_TOS = 5
FT_TCP_FLAGS = 6
FT_L4_SRC_PORT = 7
FT_IPV4_SRC_ADDR = 8
FT_INPUT_SNMP = 10
FT_L4_DST_PORT = 11
FT_IPV4_DST_ADDR = 12
FT_OUTPUT_SNMP = 14
FT_IPV4_NEXT_HOP = 15
FT_LAST_SWITCHED = 21
FT_FIRST_SWITCHED = 22
FT_OUT_BYTES = 23
FT_OUT_PKTS = 24
FT_IPV6_SRC_ADDR = 27
FT_IPV6_DST_ADDR = 28
FT_IPV6_NEXT_HOP = 62


def _routed_next_hop(family: int, raw: bytes) -> Optional[str]:
    """Next-hop address, or None for the all-zero address (delivered locally or not routed)."""
    return socket.inet_ntop(family, raw) if any(raw) else None


class V9Template:
    __slots__ = ("template_id", "fields", "total_length", "created_at")

    def __init__(self, template_id: int, fields: List[Tuple[int, int]]):
        self.template_id = template_id
        self.fields = fields  # [(field_type, field_len), ...]
        self.total_length = sum(length for _, length in fields)
        self.created_at = time.time()


class V9Parser:
    """
    Stateful NetFlow v9 template cache and binary decoder.
    Tracks templates per (exporter_ip, source_id) with 1800s TTL.
    Maintains an orphan Data FlowSet ring buffer (100 packets, 10s TTL).
    """

    def __init__(self, template_ttl: float = 1800.0, orphan_ttl: float = 10.0):
        self.template_ttl = template_ttl
        self.orphan_ttl = orphan_ttl
        # (exporter_ip, source_id, template_id) -> V9Template
        self._templates: Dict[Tuple[str, int, int], V9Template] = {}
        # (exporter_ip, source_id) -> deque of (timestamp, template_id, data_bytes, sys_uptime, unix_secs)
        self._orphan_buffer: Dict[Tuple[str, int], collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=100)
        )

    def _cleanup_expired(self, now: float) -> None:
        expired_templates = [
            k for k, v in self._templates.items() if now - v.created_at > self.template_ttl
        ]
        for k in expired_templates:
            self._templates.pop(k, None)

    def parse_packet(self, data: bytes, exporter_ip: str) -> List[dict[str, Any]]:
        now = time.time()
        if len(data) < HEADER_STRUCT.size:
            return []

        (
            version,
            count,
            sys_uptime,
            unix_secs,
            package_seq,
            source_id,
        ) = HEADER_STRUCT.unpack_from(data, 0)

        if version != 9 or count == 0:
            return []

        self._cleanup_expired(now)
        offset = HEADER_STRUCT.size
        flows: List[dict[str, Any]] = []

        while offset + FLOWSET_HEADER_STRUCT.size <= len(data):
            flowset_id, flowset_len = FLOWSET_HEADER_STRUCT.unpack_from(data, offset)
            if flowset_len < FLOWSET_HEADER_STRUCT.size:
                break
            flowset_end = offset + flowset_len
            if flowset_end > len(data):
                break

            flowset_payload = data[offset + FLOWSET_HEADER_STRUCT.size : flowset_end]

            if flowset_id == 0:
                # Template FlowSet
                self._parse_template_flowset(
                    flowset_payload, exporter_ip, source_id, sys_uptime, unix_secs, flows
                )
            elif flowset_id == 1:
                # Options Template FlowSet (skip)
                pass
            elif flowset_id >= 256:
                # Data FlowSet
                parsed = self._parse_data_flowset(
                    flowset_id, flowset_payload, exporter_ip, source_id, sys_uptime, unix_secs
                )
                if parsed is not None:
                    flows.extend(parsed)
                else:
                    # Buffer as orphan awaiting template
                    q = self._orphan_buffer[(exporter_ip, source_id)]
                    q.append((now, flowset_id, flowset_payload, sys_uptime, unix_secs))

            offset = flowset_end

        return flows

    def _parse_template_flowset(
        self,
        payload: bytes,
        exporter_ip: str,
        source_id: int,
        sys_uptime: int,
        unix_secs: int,
        out_flows: List[dict[str, Any]],
    ) -> None:
        idx = 0
        while idx + 4 <= len(payload):
            template_id, field_count = struct.unpack_from("!HH", payload, idx)
            idx += 4
            if template_id < 256:
                break
            fields: List[Tuple[int, int]] = []
            for _ in range(field_count):
                if idx + 4 > len(payload):
                    break
                ft, fl = struct.unpack_from("!HH", payload, idx)
                fields.append((ft, fl))
                idx += 4

            if len(fields) != field_count:
                continue

            tmpl = V9Template(template_id, fields)
            cache_key = (exporter_ip, source_id, template_id)
            self._templates[cache_key] = tmpl

            # Drain any orphaned data awaiting this template
            self._drain_orphans(exporter_ip, source_id, template_id, out_flows)

    def _drain_orphans(
        self,
        exporter_ip: str,
        source_id: int,
        template_id: int,
        out_flows: List[dict[str, Any]],
    ) -> None:
        now = time.time()
        buf = self._orphan_buffer.get((exporter_ip, source_id))
        if not buf:
            return

        remaining: collections.deque = collections.deque(maxlen=100)
        while buf:
            item = buf.popleft()
            t_stamp, o_tmpl_id, o_payload, o_sys_uptime, o_unix_secs = item
            if now - t_stamp > self.orphan_ttl:
                continue
            if o_tmpl_id == template_id:
                parsed = self._parse_data_flowset(
                    o_tmpl_id, o_payload, exporter_ip, source_id, o_sys_uptime, o_unix_secs
                )
                if parsed is not None:
                    out_flows.extend(parsed)
            else:
                remaining.append(item)

        self._orphan_buffer[(exporter_ip, source_id)] = remaining

    def _parse_data_flowset(
        self,
        template_id: int,
        payload: bytes,
        exporter_ip: str,
        source_id: int,
        sys_uptime: int,
        unix_secs: int,
    ) -> Optional[List[dict[str, Any]]]:
        cache_key = (exporter_ip, source_id, template_id)
        tmpl = self._templates.get(cache_key)
        if not tmpl or tmpl.total_length == 0:
            return None

        flows: List[dict[str, Any]] = []
        offset = 0

        while offset + tmpl.total_length <= len(payload):
            src_ip: Optional[str] = None
            dst_ip: Optional[str] = None
            src_port = 0
            dst_port = 0
            protocol = 0
            octets = 0
            packets = 0
            tcp_flags = 0
            tos = 0
            input_if = 0
            output_if = 0
            next_hop: Optional[str] = None
            first_ms: Optional[int] = None
            last_ms: Optional[int] = None

            rec_offset = offset
            for f_type, f_len in tmpl.fields:
                f_bytes = payload[rec_offset : rec_offset + f_len]
                rec_offset += f_len

                if f_type == FT_IPV4_SRC_ADDR and f_len == 4:
                    src_ip = socket.inet_ntop(socket.AF_INET, f_bytes)
                elif f_type == FT_IPV4_DST_ADDR and f_len == 4:
                    dst_ip = socket.inet_ntop(socket.AF_INET, f_bytes)
                elif f_type == FT_IPV6_SRC_ADDR and f_len == 16:
                    src_ip = socket.inet_ntop(socket.AF_INET6, f_bytes)
                elif f_type == FT_IPV6_DST_ADDR and f_len == 16:
                    dst_ip = socket.inet_ntop(socket.AF_INET6, f_bytes)
                elif f_type == FT_IPV4_NEXT_HOP and f_len == 4:
                    next_hop = _routed_next_hop(socket.AF_INET, f_bytes)
                elif f_type == FT_IPV6_NEXT_HOP and f_len == 16:
                    next_hop = _routed_next_hop(socket.AF_INET6, f_bytes)
                elif f_type == FT_L4_SRC_PORT:
                    src_port = int.from_bytes(f_bytes, "big")
                elif f_type == FT_L4_DST_PORT:
                    dst_port = int.from_bytes(f_bytes, "big")
                elif f_type == FT_PROTOCOL:
                    protocol = int.from_bytes(f_bytes, "big")
                elif f_type == FT_SRC_TOS:
                    tos = int.from_bytes(f_bytes, "big")
                elif f_type == FT_IN_BYTES:
                    octets = int.from_bytes(f_bytes, "big")
                elif f_type == FT_OUT_BYTES and octets == 0:
                    octets = int.from_bytes(f_bytes, "big")
                elif f_type == FT_IN_PKTS:
                    packets = int.from_bytes(f_bytes, "big")
                elif f_type == FT_OUT_PKTS and packets == 0:
                    packets = int.from_bytes(f_bytes, "big")
                elif f_type == FT_TCP_FLAGS:
                    tcp_flags = int.from_bytes(f_bytes, "big")
                elif f_type == FT_INPUT_SNMP:
                    input_if = int.from_bytes(f_bytes, "big")
                elif f_type == FT_OUTPUT_SNMP:
                    output_if = int.from_bytes(f_bytes, "big")
                elif f_type == FT_FIRST_SWITCHED:
                    first_ms = int.from_bytes(f_bytes, "big")
                elif f_type == FT_LAST_SWITCHED:
                    last_ms = int.from_bytes(f_bytes, "big")

            offset += tmpl.total_length

            if src_ip and dst_ip:
                def calc_v9_time(uptime_ms: Optional[int]) -> float:
                    if uptime_ms is None:
                        return float(unix_secs)
                    diff = sys_uptime - uptime_ms
                    if diff < 0:
                        diff += 0x100000000
                    if diff > 2592000000:
                        return float(unix_secs)
                    return float(unix_secs) - (diff / 1000.0)

                start_time = calc_v9_time(first_ms)
                end_time = calc_v9_time(last_ms)

                flows.append(
                    {
                        "version": 9,
                        "exporter_ip": exporter_ip,
                        "src_ip": src_ip,
                        "dst_ip": dst_ip,
                        "src_port": src_port,
                        "dst_port": dst_port,
                        "protocol": protocol,
                        "bytes": octets,
                        "packets": packets,
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
