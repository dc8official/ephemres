"""sFlow v5 decoder (sflow.org "sFlow Version 5", 2004).

sFlow is not NetFlow with another header: a device does not export flows, it exports one in
every N packets it forwards (the sampling rate), each as the first bytes of the packet. So each
sample here becomes one "flow" of a single packet, scaled by that sample's own rate: packets =
rate, bytes = the packet's IP length x rate. The figures are estimates that get better with
volume; a conversation of a few packets may not be sampled at all.

What is read: flow samples (compact and expanded) with a raw packet header (Ethernet, IPv4 or
IPv6), or the pre-decoded "sampled IPv4 / IPv6" records, plus the extended router record for the
next hop. Counter samples are skipped: interface counters come from the SNMP poller.

Bytes are the IP packet length, not the frame length, so the totals agree with NetFlow, which
counts layer 3 bytes.

Everything is XDR: big-endian 32-bit words, opaque data padded to a multiple of four bytes.
"""
from __future__ import annotations

import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

VERSION = 5
# Datagram header after the agent address: sub-agent id, sequence, uptime (ms), sample count.
_MIN_DATAGRAM = 28  # version + address type + IPv4 agent + the four words above

# A datagram fits in one UDP packet, so real ones carry a handful of samples. The caps keep a
# forged count from making the loops below spin.
MAX_SAMPLES = 256
MAX_RECORDS = 32

# Sample and record types are (enterprise << 12) | format; enterprise 0 is the standard set.
_FLOW_SAMPLE, _FLOW_SAMPLE_EXPANDED = 1, 3
_REC_RAW_HEADER, _REC_IPV4, _REC_IPV6, _REC_EXT_ROUTER = 1, 3, 4, 1002

# header_protocol values of a raw packet header record.
_HDR_ETHERNET, _HDR_IPV4, _HDR_IPV6 = 1, 11, 12

_ETH_IPV4, _ETH_IPV6 = 0x0800, 0x86DD
_ETH_VLAN = (0x8100, 0x88A8, 0x9100)

_TCP, _UDP, _SCTP = 6, 17, 132
# IPv6 extension headers that carry "next header" and a length in units of eight bytes.
_V6_EXTENSIONS = (0, 43, 60)
_V6_FRAGMENT = 44


class SflowError(ValueError):
    """The datagram is not sFlow v5 or is cut short."""


def is_sflow(data: bytes) -> bool:
    """True when the datagram starts like sFlow v5. Its version is a 32-bit 5, so the first 16
    bits are zero: no NetFlow or IPFIX packet (16-bit version 5, 9 or 10) can look like it."""
    return len(data) >= _MIN_DATAGRAM and data[:4] == b"\x00\x00\x00\x05" and data[4:8] in (
        b"\x00\x00\x00\x01", b"\x00\x00\x00\x02")


def _header(data: bytes) -> Tuple[int, int]:
    """(offset of the first sample, sample count)."""
    if not is_sflow(data):
        raise SflowError("not an sFlow v5 datagram")
    pos = 8 + (4 if data[7] == 1 else 16)  # agent address: IPv4 or IPv6
    if pos + 16 > len(data):
        raise SflowError("truncated header")
    count = struct.unpack_from("!I", data, pos + 12)[0]
    return pos + 16, count


def sample_count(data: bytes) -> int:
    """How many samples the datagram says it carries (for the exporter admission count)."""
    try:
        return min(_header(data)[1], MAX_SAMPLES)
    except SflowError:
        return 1


def _if_index(fmt: int, value: int, unknown: int) -> int:
    """An interface field as an ifIndex, or 0. Format 0 is a single interface; 1 (dropped, the
    value is a reason code) and 2 (several interfaces, the value is a count) are not one."""
    return value if fmt == 0 and value != unknown else 0


def _ports(protocol: int, l4: bytes) -> Tuple[int, int, int]:
    """(source port, destination port, TCP flags) from the start of the transport header."""
    if protocol in (_TCP, _UDP, _SCTP) and len(l4) >= 4:
        src, dst = struct.unpack_from("!HH", l4, 0)
        flags = l4[13] if protocol == _TCP and len(l4) >= 14 else 0
        return src, dst, flags
    return 0, 0, 0


def _decode_ipv4(pkt: bytes) -> Optional[Dict[str, Any]]:
    if len(pkt) < 20 or pkt[0] >> 4 != 4:
        return None
    ihl = (pkt[0] & 0x0F) * 4
    if ihl < 20:
        return None
    total_length, _ident, frag = struct.unpack_from("!HHH", pkt, 2)
    protocol = pkt[9]
    # Only the first fragment has a transport header; a later one would be read as ports.
    src_port, dst_port, flags = _ports(protocol, pkt[ihl:]) if frag & 0x1FFF == 0 else (0, 0, 0)
    return {
        "src_ip": socket.inet_ntoa(pkt[12:16]), "dst_ip": socket.inet_ntoa(pkt[16:20]),
        "protocol": protocol, "tos": pkt[1], "ip_length": total_length,
        "src_port": src_port, "dst_port": dst_port, "tcp_flags": flags,
    }


def _decode_ipv6(pkt: bytes) -> Optional[Dict[str, Any]]:
    if len(pkt) < 40 or pkt[0] >> 4 != 6:
        return None
    payload_length = struct.unpack_from("!H", pkt, 4)[0]
    next_header, pos, fragment = pkt[6], 40, False
    # Walk past the extension headers to the transport header, as far as the sample reaches.
    for _ in range(8):
        if next_header in _V6_EXTENSIONS and pos + 2 <= len(pkt):
            next_header, pos = pkt[pos], pos + (pkt[pos + 1] + 1) * 8
        elif next_header == _V6_FRAGMENT and pos + 8 <= len(pkt):
            fragment = fragment or struct.unpack_from("!H", pkt, pos + 2)[0] & 0xFFF8 != 0
            next_header, pos = pkt[pos], pos + 8
        else:
            break
    src_port, dst_port, flags = (0, 0, 0) if fragment else _ports(next_header, pkt[pos:])
    return {
        "src_ip": socket.inet_ntop(socket.AF_INET6, pkt[8:24]),
        "dst_ip": socket.inet_ntop(socket.AF_INET6, pkt[24:40]),
        "protocol": next_header, "tos": ((pkt[0] & 0x0F) << 4) | (pkt[1] >> 4),
        "ip_length": payload_length + 40,
        "src_port": src_port, "dst_port": dst_port, "tcp_flags": flags,
    }


def _decode_raw_header(rec: bytes) -> Optional[Dict[str, Any]]:
    """A raw packet header record: the first bytes of the sampled packet. None when it is not
    IP (ARP, spanning tree and the like are sampled too, and are not traffic we chart)."""
    if len(rec) < 16:
        return None
    header_protocol, frame_length, _stripped, header_length = struct.unpack_from("!IIII", rec, 0)
    header = rec[16:16 + header_length]
    l2 = 0
    if header_protocol == _HDR_ETHERNET:
        if len(header) < 14:
            return None
        ethertype, l2 = struct.unpack_from("!H", header, 12)[0], 14
        # 802.1Q / QinQ tags sit between the addresses and the real EtherType.
        while ethertype in _ETH_VLAN and l2 + 4 <= len(header):
            ethertype, l2 = struct.unpack_from("!H", header, l2 + 2)[0], l2 + 4
        if ethertype == _ETH_IPV4:
            flow = _decode_ipv4(header[l2:])
        elif ethertype == _ETH_IPV6:
            flow = _decode_ipv6(header[l2:])
        else:
            return None
    elif header_protocol == _HDR_IPV4:
        flow = _decode_ipv4(header)
    elif header_protocol == _HDR_IPV6:
        flow = _decode_ipv6(header)
    else:
        return None
    if flow is not None and not flow["ip_length"]:
        # Length 0 in the IP header: a segment the NIC will split later (TSO), or an IPv6
        # jumbogram. The frame length without the layer 2 header is the best figure left.
        flow["ip_length"] = max(frame_length - l2, 0)
    return flow


def _decode_sampled_ip(rec: bytes, v6: bool) -> Optional[Dict[str, Any]]:
    """The pre-decoded "sampled IPv4" / "sampled IPv6" record some agents send instead of the
    raw header: length, protocol, addresses, ports, TCP flags, ToS (IPv6: priority)."""
    alen = 16 if v6 else 4
    if len(rec) < 8 + 2 * alen + 16:
        return None
    length, protocol = struct.unpack_from("!II", rec, 0)
    family = socket.AF_INET6 if v6 else socket.AF_INET
    src_port, dst_port, flags, tos = struct.unpack_from("!IIII", rec, 8 + 2 * alen)
    return {
        "src_ip": socket.inet_ntop(family, rec[8:8 + alen]),
        "dst_ip": socket.inet_ntop(family, rec[8 + alen:8 + 2 * alen]),
        "protocol": protocol & 0xFF, "tos": tos & 0xFF, "ip_length": length,
        "src_port": src_port & 0xFFFF, "dst_port": dst_port & 0xFFFF, "tcp_flags": flags & 0xFF,
    }


def _decode_next_hop(rec: bytes) -> Optional[str]:
    """The next hop from an extended router record; None for the unspecified address."""
    if len(rec) < 8:
        return None
    addr_type = struct.unpack_from("!I", rec, 0)[0]
    if addr_type == 1 and len(rec) >= 8:
        raw, family = rec[4:8], socket.AF_INET
    elif addr_type == 2 and len(rec) >= 20:
        raw, family = rec[4:20], socket.AF_INET6
    else:
        return None
    return socket.inet_ntop(family, raw) if any(raw) else None


def _decode_flow_sample(body: bytes, expanded: bool) -> Optional[Dict[str, Any]]:
    """One flow sample as a flow dict without exporter or time, or None when it holds no IP
    packet we can read."""
    if expanded:
        if len(body) < 44:
            return None
        (_seq, _src_type, _src_index, rate, _pool, _drops,
         in_fmt, in_val, out_fmt, out_val, n_records) = struct.unpack_from("!11I", body, 0)
        input_if = _if_index(in_fmt, in_val, 0xFFFFFFFF)
        output_if = _if_index(out_fmt, out_val, 0xFFFFFFFF)
        pos = 44
    else:
        if len(body) < 32:
            return None
        _seq, _source, rate, _pool, _drops, in_raw, out_raw, n_records = struct.unpack_from("!8I", body, 0)
        # Compact form: the top two bits are the format, the other thirty the value.
        input_if = _if_index(in_raw >> 30, in_raw & 0x3FFFFFFF, 0x3FFFFFFF)
        output_if = _if_index(out_raw >> 30, out_raw & 0x3FFFFFFF, 0x3FFFFFFF)
        pos = 32

    flow: Optional[Dict[str, Any]] = None
    next_hop: Optional[str] = None
    for _ in range(min(n_records, MAX_RECORDS)):
        if pos + 8 > len(body):
            break
        rec_type, rec_len = struct.unpack_from("!II", body, pos)
        pos += 8
        if pos + rec_len > len(body):
            break
        rec = body[pos:pos + rec_len]
        pos += (rec_len + 3) & ~3  # records are padded to four bytes
        if rec_type == _REC_RAW_HEADER and flow is None:
            flow = _decode_raw_header(rec)
        elif rec_type in (_REC_IPV4, _REC_IPV6) and flow is None:
            flow = _decode_sampled_ip(rec, v6=rec_type == _REC_IPV6)
        elif rec_type == _REC_EXT_ROUTER:
            next_hop = _decode_next_hop(rec)
    if flow is None:
        return None

    # Rate 0 means the agent did not say (or sampling is off): count the packet as itself and
    # leave the admin's sampling multiplier to apply, as for NetFlow without a rate.
    scale = rate if rate > 0 else 1
    ip_length = flow.pop("ip_length")
    flow.update({
        "bytes": ip_length * scale,
        "packets": scale,
        "header_sampled": rate > 0,
        "sampling_rate": rate,
        "input_if": input_if,
        "output_if": output_if,
        "next_hop": next_hop,
    })
    return flow


def parse_sflow_packet(data: bytes, exporter_ip: str, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Parses an sFlow v5 datagram into the same flow dicts the NetFlow parsers produce.

    The exporter is the address the datagram came from, not the agent address inside it: that
    is what an admin enrols, and it is the same rule as for NetFlow. A sample has no start and
    end time (it is one packet seen just now), so both are the time of arrival. A sample that
    is cut short ends the datagram; the samples before it are kept."""
    pos, count = _header(data)
    stamp = time.time() if now is None else now
    flows: List[Dict[str, Any]] = []
    for _ in range(min(count, MAX_SAMPLES)):
        if pos + 8 > len(data):
            break
        sample_type, length = struct.unpack_from("!II", data, pos)
        pos += 8
        if pos + length > len(data):
            break
        body = data[pos:pos + length]
        pos += (length + 3) & ~3
        if sample_type not in (_FLOW_SAMPLE, _FLOW_SAMPLE_EXPANDED):
            continue  # counter samples and vendor types
        try:
            flow = _decode_flow_sample(body, expanded=sample_type == _FLOW_SAMPLE_EXPANDED)
        except (struct.error, IndexError, OSError, ValueError):
            continue  # one unreadable sample must not cost the others
        if flow is None:
            continue
        flow.update({"version": "sflow5", "exporter_ip": exporter_ip, "start_time": stamp, "end_time": stamp})
        flows.append(flow)
    return flows
