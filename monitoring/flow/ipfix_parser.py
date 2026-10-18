from __future__ import annotations

import collections
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

HEADER_STRUCT = struct.Struct("!HHIII")  # 16 bytes: version, length, export_time, seq, obs_domain_id
SET_HEADER_STRUCT = struct.Struct("!HH")  # 4 bytes: set_id, length

# Standard IANA Information Elements (RFC 7012)
IE_OCTET_DELTA_COUNT = 1
IE_PACKET_DELTA_COUNT = 2
IE_PROTOCOL_IDENTIFIER = 4
IE_IP_CLASS_OF_SERVICE = 5
IE_TCP_CONTROL_BITS = 6
IE_SOURCE_TRANSPORT_PORT = 7
IE_SOURCE_IPV4_ADDRESS = 8
IE_INGRESS_INTERFACE = 10
IE_DESTINATION_TRANSPORT_PORT = 11
IE_DESTINATION_IPV4_ADDRESS = 12
IE_EGRESS_INTERFACE = 14
IE_FLOW_END_SYS_UP_TIME = 21
IE_FLOW_START_SYS_UP_TIME = 22
IE_POST_OCTET_DELTA_COUNT = 23
IE_POST_PACKET_DELTA_COUNT = 24
IE_SOURCE_IPV6_ADDRESS = 27
IE_DESTINATION_IPV6_ADDRESS = 28
IE_OCTET_TOTAL_COUNT = 85
IE_PACKET_TOTAL_COUNT = 86
IE_FLOW_START_SECONDS = 150
IE_FLOW_END_SECONDS = 151
IE_FLOW_START_MILLISECONDS = 152
IE_FLOW_END_MILLISECONDS = 153
IE_FLOW_START_MICROSECONDS = 154
IE_FLOW_END_MICROSECONDS = 155
IE_FLOW_START_NANOSECONDS = 156
IE_FLOW_END_NANOSECONDS = 157
IE_FLOW_START_DELTA_MICROSECONDS = 158
IE_FLOW_END_DELTA_MICROSECONDS = 159


class IPFIXFieldSpec:
    __slots__ = ("ie_id", "length", "enterprise_number")

    def __init__(self, ie_id: int, length: int, enterprise_number: Optional[int] = None):
        self.ie_id = ie_id
        self.length = length
        self.enterprise_number = enterprise_number


class IPFIXTemplate:
    __slots__ = (
        "template_id",
        "fields",
        "total_length",
        "has_variable_length",
        "min_length",
        "created_at",
    )

    def __init__(self, template_id: int, fields: List[IPFIXFieldSpec]):
        self.template_id = template_id
        self.fields = fields
        self.total_length = sum(f.length for f in fields)
        self.has_variable_length = any(f.length == 65535 for f in fields)
        self.min_length = sum(1 if f.length == 65535 else f.length for f in fields)
        self.created_at = time.time()


class IPFIXParser:
    """
    RFC 7011/7012 compliant IPFIX decoder with Enterprise bit (0x8000) support
    and dual-stack IPv4/IPv6 extraction.
    """

    def __init__(self, template_ttl: float = 1800.0, orphan_ttl: float = 10.0):
        self.template_ttl = template_ttl
        self.orphan_ttl = orphan_ttl
        # (exporter_ip, obs_domain_id, template_id) -> IPFIXTemplate
        self._templates: Dict[Tuple[str, int, int], IPFIXTemplate] = {}
        # (exporter_ip, obs_domain_id) -> deque of (timestamp, set_id, payload, export_time)
        self._orphan_buffer: Dict[Tuple[str, int], collections.deque] = collections.defaultdict(
            lambda: collections.deque(maxlen=100)
        )

    def _cleanup_expired(self, now: float) -> None:
        expired = [
            k for k, v in self._templates.items() if now - v.created_at > self.template_ttl
        ]
        for k in expired:
            self._templates.pop(k, None)

    def parse_packet(self, data: bytes, exporter_ip: str) -> List[dict[str, Any]]:
        now = time.time()
        if len(data) < HEADER_STRUCT.size:
            return []

        (
            version,
            total_len,
            export_time,
            seq_number,
            obs_domain_id,
        ) = HEADER_STRUCT.unpack_from(data, 0)

        if version != 10 or total_len > len(data):
            return []

        self._cleanup_expired(now)
        offset = HEADER_STRUCT.size
        flows: List[dict[str, Any]] = []

        while offset + SET_HEADER_STRUCT.size <= total_len:
            set_id, set_len = SET_HEADER_STRUCT.unpack_from(data, offset)
            if set_len < SET_HEADER_STRUCT.size:
                break
            set_end = offset + set_len
            if set_end > total_len:
                break

            set_payload = data[offset + SET_HEADER_STRUCT.size : set_end]

            if set_id == 2:
                # Template Set
                self._parse_template_set(
                    set_payload, exporter_ip, obs_domain_id, export_time, flows
                )
            elif set_id == 3:
                # Options Template Set (skip)
                pass
            elif set_id >= 256:
                # Data Set
                parsed = self._parse_data_set(
                    set_id, set_payload, exporter_ip, obs_domain_id, export_time
                )
                if parsed is not None:
                    flows.extend(parsed)
                else:
                    q = self._orphan_buffer[(exporter_ip, obs_domain_id)]
                    q.append((now, set_id, set_payload, export_time))

            offset = set_end

        return flows

    def _parse_template_set(
        self,
        payload: bytes,
        exporter_ip: str,
        obs_domain_id: int,
        export_time: int,
        out_flows: List[dict[str, Any]],
    ) -> None:
        idx = 0
        while idx + 4 <= len(payload):
            template_id, field_count = struct.unpack_from("!HH", payload, idx)
            idx += 4
            if template_id < 256:
                break
            fields: List[IPFIXFieldSpec] = []
            for _ in range(field_count):
                if idx + 4 > len(payload):
                    break
                raw_ie_id, field_len = struct.unpack_from("!HH", payload, idx)
                idx += 4
                enterprise_bit = bool(raw_ie_id & 0x8000)
                ie_id = raw_ie_id & 0x7FFF
                enterprise_number: Optional[int] = None
                if enterprise_bit:
                    if idx + 4 > len(payload):
                        break
                    enterprise_number = struct.unpack_from("!I", payload, idx)[0]
                    idx += 4
                fields.append(IPFIXFieldSpec(ie_id, field_len, enterprise_number))

            if len(fields) != field_count:
                continue

            tmpl = IPFIXTemplate(template_id, fields)
            cache_key = (exporter_ip, obs_domain_id, template_id)
            self._templates[cache_key] = tmpl

            self._drain_orphans(exporter_ip, obs_domain_id, template_id, out_flows)

    def _drain_orphans(
        self,
        exporter_ip: str,
        obs_domain_id: int,
        template_id: int,
        out_flows: List[dict[str, Any]],
    ) -> None:
        now = time.time()
        buf = self._orphan_buffer.get((exporter_ip, obs_domain_id))
        if not buf:
            return

        remaining: collections.deque = collections.deque(maxlen=100)
        while buf:
            item = buf.popleft()
            t_stamp, o_set_id, o_payload, o_export_time = item
            if now - t_stamp > self.orphan_ttl:
                continue
            if o_set_id == template_id:
                parsed = self._parse_data_set(
                    o_set_id, o_payload, exporter_ip, obs_domain_id, o_export_time
                )
                if parsed is not None:
                    out_flows.extend(parsed)
            else:
                remaining.append(item)

        self._orphan_buffer[(exporter_ip, obs_domain_id)] = remaining

    def _parse_data_set(
        self,
        template_id: int,
        payload: bytes,
        exporter_ip: str,
        obs_domain_id: int,
        export_time: int,
    ) -> Optional[List[dict[str, Any]]]:
        cache_key = (exporter_ip, obs_domain_id, template_id)
        tmpl = self._templates.get(cache_key)
        if not tmpl or (tmpl.min_length == 0 and tmpl.total_length == 0):
            return None

        flows: List[dict[str, Any]] = []
        offset = 0

        while offset + tmpl.min_length <= len(payload):
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
            start_time: Optional[float] = None
            end_time: Optional[float] = None
            first_up_ms: Optional[int] = None
            last_up_ms: Optional[int] = None

            rec_offset = offset
            record_valid = True
            for f in tmpl.fields:
                if f.length == 65535:
                    if rec_offset >= len(payload):
                        record_valid = False
                        break
                    first_byte = payload[rec_offset]
                    rec_offset += 1
                    if first_byte < 255:
                        actual_len = first_byte
                    else:
                        if rec_offset + 2 > len(payload):
                            record_valid = False
                            break
                        actual_len = int.from_bytes(payload[rec_offset : rec_offset + 2], "big")
                        rec_offset += 2
                    if rec_offset + actual_len > len(payload):
                        record_valid = False
                        break
                    f_bytes = payload[rec_offset : rec_offset + actual_len]
                    rec_offset += actual_len
                else:
                    if rec_offset + f.length > len(payload):
                        record_valid = False
                        break
                    f_bytes = payload[rec_offset : rec_offset + f.length]
                    rec_offset += f.length

                if f.enterprise_number is not None:
                    continue  # Skip enterprise fields for standard metrics

                if f.ie_id == IE_SOURCE_IPV4_ADDRESS and len(f_bytes) == 4:
                    src_ip = socket.inet_ntop(socket.AF_INET, f_bytes)
                elif f.ie_id == IE_DESTINATION_IPV4_ADDRESS and len(f_bytes) == 4:
                    dst_ip = socket.inet_ntop(socket.AF_INET, f_bytes)
                elif f.ie_id == IE_SOURCE_IPV6_ADDRESS and len(f_bytes) == 16:
                    src_ip = socket.inet_ntop(socket.AF_INET6, f_bytes)
                elif f.ie_id == IE_DESTINATION_IPV6_ADDRESS and len(f_bytes) == 16:
                    dst_ip = socket.inet_ntop(socket.AF_INET6, f_bytes)
                elif f.ie_id == IE_SOURCE_TRANSPORT_PORT:
                    src_port = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_DESTINATION_TRANSPORT_PORT:
                    dst_port = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_PROTOCOL_IDENTIFIER:
                    protocol = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_IP_CLASS_OF_SERVICE:
                    tos = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_OCTET_DELTA_COUNT:
                    octets = int.from_bytes(f_bytes, "big")
                elif f.ie_id in (IE_POST_OCTET_DELTA_COUNT, IE_OCTET_TOTAL_COUNT) and octets == 0:
                    octets = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_PACKET_DELTA_COUNT:
                    packets = int.from_bytes(f_bytes, "big")
                elif f.ie_id in (IE_POST_PACKET_DELTA_COUNT, IE_PACKET_TOTAL_COUNT) and packets == 0:
                    packets = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_TCP_CONTROL_BITS:
                    tcp_flags = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_INGRESS_INTERFACE:
                    input_if = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_EGRESS_INTERFACE:
                    output_if = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_FLOW_START_SECONDS:
                    start_time = float(int.from_bytes(f_bytes, "big"))
                elif f.ie_id == IE_FLOW_END_SECONDS:
                    end_time = float(int.from_bytes(f_bytes, "big"))
                elif f.ie_id == IE_FLOW_START_MILLISECONDS:
                    start_time = int.from_bytes(f_bytes, "big") / 1000.0
                elif f.ie_id == IE_FLOW_END_MILLISECONDS:
                    end_time = int.from_bytes(f_bytes, "big") / 1000.0
                elif f.ie_id == IE_FLOW_START_MICROSECONDS:
                    start_time = int.from_bytes(f_bytes, "big") / 1_000_000.0
                elif f.ie_id == IE_FLOW_END_MICROSECONDS:
                    end_time = int.from_bytes(f_bytes, "big") / 1_000_000.0
                elif f.ie_id == IE_FLOW_START_NANOSECONDS:
                    start_time = int.from_bytes(f_bytes, "big") / 1_000_000_000.0
                elif f.ie_id == IE_FLOW_END_NANOSECONDS:
                    end_time = int.from_bytes(f_bytes, "big") / 1_000_000_000.0
                elif f.ie_id == IE_FLOW_START_DELTA_MICROSECONDS:
                    start_time = float(export_time) - (int.from_bytes(f_bytes, "big") / 1_000_000.0)
                elif f.ie_id == IE_FLOW_END_DELTA_MICROSECONDS:
                    end_time = float(export_time) - (int.from_bytes(f_bytes, "big") / 1_000_000.0)
                elif f.ie_id == IE_FLOW_START_SYS_UP_TIME:
                    first_up_ms = int.from_bytes(f_bytes, "big")
                elif f.ie_id == IE_FLOW_END_SYS_UP_TIME:
                    last_up_ms = int.from_bytes(f_bytes, "big")

            if not record_valid or rec_offset <= offset:
                break
            offset = rec_offset

            if src_ip and dst_ip:
                if start_time is None and first_up_ms is not None and last_up_ms is not None:
                    dur_sec = max(0.0, (last_up_ms - first_up_ms) / 1000.0)
                    start_time = float(export_time) - dur_sec
                    if end_time is None:
                        end_time = float(export_time)
                if start_time is None:
                    start_time = float(export_time)
                if end_time is None:
                    end_time = float(export_time)

                flows.append(
                    {
                        "version": 10,
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
                        "start_time": start_time,
                        "end_time": end_time,
                    }
                )

        return flows
