"""Flow telemetry calculations shared by the bandwidth API and the PDF report compiler.

Both `app.routers.bandwidth` and `app.services.report_compiler` turn the same rollup rows into
rates, utilization, QoS classes, traffic zones and TCP anomaly counts. Keeping that arithmetic
here means the dashboard and the report cannot drift apart.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.schemas.bandwidth import format_dscp_label

# IP protocol numbers shown by name; anything else renders as "PROTO-<n>".
PROTOCOL_MAP: Dict[int, str] = {
    1: "ICMP",
    6: "TCP",
    17: "UDP",
    47: "GRE",
    50: "ESP",
    58: "IPv6-ICMP",
    89: "OSPF",
}

# Link speed assumed for interfaces whose alias carries no speed.
DEFAULT_LINK_SPEED_MBPS = 1000

ZONE_EAST_WEST = "East-West (Internal)"
ZONE_NORTH_SOUTH_EGRESS = "North-South Egress"
ZONE_NORTH_SOUTH_INGRESS = "North-South Ingress"
ZONE_EXTERNAL_TRANSIT = "External / Transit"
# Display order used by the API response and the report table.
TRAFFIC_ZONES: Tuple[str, ...] = (
    ZONE_EAST_WEST,
    ZONE_NORTH_SOUTH_EGRESS,
    ZONE_NORTH_SOUTH_INGRESS,
    ZONE_EXTERNAL_TRANSIT,
)

ANOMALY_SYN_ONLY = "SYN_ONLY"
ANOMALY_TCP_RST = "TCP_RST"

_TCP_FIN = 0x01
_TCP_SYN = 0x02
_TCP_RST = 0x04
_TCP_ACK = 0x10


def bits_per_second(byte_count: float, seconds: float) -> float:
    """Average bit rate of `byte_count` bytes spread over `seconds`."""
    return (float(byte_count) * 8.0) / seconds


def utilization_pct(bps: float, speed_mbps: float) -> float:
    """Share of the link capacity used by `bps`, capped at 100 %; 0 when the speed is unknown."""
    capacity_bps = speed_mbps * 1_000_000.0
    if capacity_bps <= 0:
        return 0.0
    return min(100.0, (bps / capacity_bps) * 100.0)


def interface_alias(alias_data: Any, default_name: str) -> Tuple[str, int]:
    """Resolve a stored `flow_interface_aliases` entry to (display name, speed in Mbps).

    Entries are either a legacy plain-string name or a {"name", "speed_mbps"} dict; anything else
    (including a missing entry) falls back to `default_name` and the default link speed.
    """
    if isinstance(alias_data, dict):
        name = str(alias_data.get("name", default_name))
        speed_mbps = int(alias_data.get("speed_mbps") or DEFAULT_LINK_SPEED_MBPS)
        return name, speed_mbps
    if isinstance(alias_data, str):
        return alias_data, DEFAULT_LINK_SPEED_MBPS
    return default_name, DEFAULT_LINK_SPEED_MBPS


def percentile_with_idle(sorted_values: Sequence[float], total_slots: int, q: float = 0.95) -> float:
    """Percentile of per-slot traffic where slots missing from `sorted_values` were idle (0).

    Rollups only hold rows for minutes (or hours) that carried traffic, so a percentile over the
    rows alone would ignore idle time and overstate a bursty link. `sorted_values` is ascending;
    `total_slots` is how many slots the covered period has. Interpolates like percentile_cont.
    """
    n_values = len(sorted_values)
    n = max(total_slots, n_values)
    if n == 0:
        return 0.0
    zeros = n - n_values

    def at(i: int) -> float:
        return 0.0 if i < zeros else float(sorted_values[i - zeros])

    pos = q * (n - 1)
    lower = int(pos)
    upper = min(lower + 1, n - 1)
    return at(lower) + (at(upper) - at(lower)) * (pos - lower)


def configured_speed_mbps(alias_data: Any) -> Optional[int]:
    """Link speed an admin actually set for an interface, or None when it would be a guess.

    Legacy string aliases and dicts without a positive `speed_mbps` carry no speed.
    """
    if not isinstance(alias_data, dict):
        return None
    try:
        speed = int(alias_data.get("speed_mbps") or 0)
    except (TypeError, ValueError):
        return None
    return speed if speed > 0 else None


# Interface roles an admin can set in `flow_interface_aliases`. "wan" marks a link to the outside
# (internet or provider uplink); everything else is "lan".
ROLE_WAN = "wan"
ROLE_LAN = "lan"
INTERFACE_ROLES: Tuple[str, ...] = (ROLE_WAN, ROLE_LAN)


def interface_role(alias_data: Any) -> str:
    """Role of an interface: "wan" only when an admin set it; legacy and missing entries are "lan"."""
    if isinstance(alias_data, dict) and alias_data.get("role") == ROLE_WAN:
        return ROLE_WAN
    return ROLE_LAN


def dscp_from_tos(tos: Optional[int]) -> int:
    """6-bit DSCP codepoint from the 8-bit ToS / Traffic Class byte (RFC 2474)."""
    return ((tos or 0) & 0xFF) >> 2


def dscp_breakdown(rows: Iterable[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Group (tos, bytes[, packets[, flows]]) rows into DSCP classes, largest first.

    Each class carries `dscp`, `dscp_label`, `total_bytes`, `total_packets`, `flow_count` and an
    unrounded `percentage` of the bytes across all rows.
    """
    buckets: Dict[int, Dict[str, Any]] = {}
    total_bytes = 0
    for row in rows:
        if len(row) < 2:
            continue
        tos_val = int(row[0] or 0)
        row_bytes = int(row[1] or 0)
        row_packets = int(row[2] or 0) if len(row) > 2 else 0
        row_flows = int(row[3] or 0) if len(row) > 3 else 0
        dscp = dscp_from_tos(tos_val)
        total_bytes += row_bytes
        bucket = buckets.setdefault(
            dscp,
            {
                "dscp": dscp,
                "dscp_label": format_dscp_label(tos_val),
                "total_bytes": 0,
                "total_packets": 0,
                "flow_count": 0,
            },
        )
        bucket["total_bytes"] += row_bytes
        bucket["total_packets"] += row_packets
        bucket["flow_count"] += row_flows

    classes = sorted(buckets.values(), key=lambda b: b["total_bytes"], reverse=True)
    for item in classes:
        item["percentage"] = (item["total_bytes"] / total_bytes * 100.0) if total_bytes > 0 else 0.0
    return classes


# Addresses counted as inside the network when no monitored endpoint claims them: RFC 1918,
# loopback, link-local and IPv6 unique-local. A fixed list rather than ipaddress.is_private,
# whose ranges change between Python versions and include the documentation ranges, and so the
# SQL rule (internal_sql) reads the same list as the Python one.
INTERNAL_NETWORKS: Tuple[str, ...] = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "fc00::/7",
    "fe80::/10",
    "::1/128",
)
_INTERNAL_NETS = tuple(ipaddress.ip_network(n) for n in INTERNAL_NETWORKS)


def is_internal_address(ip_value: Any, endpoint_id: Optional[Any]) -> bool:
    """True for a monitored endpoint or an address in INTERNAL_NETWORKS. The one meaning of
    "internal" on the Traffic page and in the PDF (zones, inbound/outbound, transit)."""
    if endpoint_id is not None:
        return True
    try:
        clean_ip = str(ip_value or "").split("/")[0].strip()
        if not clean_ip:
            return False
        addr = ipaddress.ip_address(clean_ip)
    except ValueError:
        return False
    return any(addr.version == net.version and addr in net for net in _INTERNAL_NETS)


def internal_sql(ip_expr: str, endpoint_expr: str) -> str:
    """SQL boolean for `is_internal_address` over an inet column and an endpoint id column.
    `<<=` between an IPv4 and an IPv6 value is false, so one list serves both families."""
    nets = ", ".join(f"'{n}'" for n in INTERNAL_NETWORKS)
    return (
        f"({endpoint_expr} IS NOT NULL OR "
        f"COALESCE({ip_expr} <<= ANY(CAST(ARRAY[{nets}] AS inet[])), FALSE))"
    )


def zone_sql(src_ip: str, src_endpoint: str, dst_ip: str, dst_endpoint: str) -> str:
    """SQL CASE yielding the traffic zone name; the same rule as `classify_zone`. Zones are
    summed in the database instead of pulling every address pair into Python."""
    src_in, dst_in = internal_sql(src_ip, src_endpoint), internal_sql(dst_ip, dst_endpoint)
    return (
        f"CASE WHEN {src_in} AND {dst_in} THEN '{ZONE_EAST_WEST}' "
        f"WHEN {src_in} THEN '{ZONE_NORTH_SOUTH_EGRESS}' "
        f"WHEN {dst_in} THEN '{ZONE_NORTH_SOUTH_INGRESS}' "
        f"ELSE '{ZONE_EXTERNAL_TRANSIT}' END"
    )


def classify_zone(src_internal: bool, dst_internal: bool) -> str:
    """Traffic zone for a flow whose ends are internal or external."""
    if src_internal and dst_internal:
        return ZONE_EAST_WEST
    if src_internal:
        return ZONE_NORTH_SOUTH_EGRESS
    if dst_internal:
        return ZONE_NORTH_SOUTH_INGRESS
    return ZONE_EXTERNAL_TRANSIT


# Bulk: a conversation moving a lot of data in full-size packets while it runs (a backup, a
# replication job). Packet size alone made every Microsoft 365 or Windows Update download
# "bulk", so the filter no longer singled out the jobs that fill a link (review C2). The rate is
# taken over the time the conversation was seen, not over the window: a nightly backup is two
# busy hours a night, whether the window is a day or a week. All must hold:
BULK_MIN_AVG_PACKET_BYTES = 1100
# - it was seen for at least this long (the buckets it appears in, added up);
BULK_MIN_SEEN_SECONDS = 1800
# - it averaged at least this rate over that time. An office's web and Microsoft 365 traffic
#   seldom holds 5 Mb/s on one conversation; a backup or replication job does.
BULK_MIN_AVG_BPS = 5_000_000


def is_bulk(avg_packet_size: float, seen_seconds: float, total_bytes: float) -> bool:
    """The Bulk rule; `bulk_sql` is the same rule in SQL (conversation table and quick filter)."""
    return (
        avg_packet_size >= BULK_MIN_AVG_PACKET_BYTES
        and seen_seconds >= BULK_MIN_SEEN_SECONDS
        and total_bytes * 8 >= BULK_MIN_AVG_BPS * seen_seconds
    )


def bulk_sql(avg_packet_expr: str, seen_seconds_expr: str, bytes_expr: str) -> str:
    """SQL boolean for `is_bulk` over a conversation's aggregated columns. Floats, so the
    products cannot overflow an integer column."""
    return (
        f"({avg_packet_expr} >= {BULK_MIN_AVG_PACKET_BYTES} "
        f"AND {seen_seconds_expr} >= {BULK_MIN_SEEN_SECONDS} "
        f"AND {bytes_expr} * 8.0 >= {float(BULK_MIN_AVG_BPS)} * {seen_seconds_expr})"
    )


# Service names by (protocol, service port), then by port alone. The one table for the
# Applications panel, the conversation table and the PDF. flowd stores the service port
# (monitoring/flow/aggregator.py), so a reply carries its service's port too.
_TCP, _UDP = 6, 17
SERVICE_NAMES_BY_PROTOCOL: Dict[Tuple[int, int], str] = {
    # UDP 443 is QUIC, not HTTPS over TCP: two "HTTPS" rows used to appear in Applications.
    (_UDP, 443): "QUIC (HTTP/3)",
    (_UDP, 500): "IPsec IKE",
    (_UDP, 4500): "IPsec NAT-T",
    (_UDP, 514): "Syslog",
    (_TCP, 514): "rsh",
    (_UDP, 1194): "OpenVPN",
    (_UDP, 3478): "STUN/TURN",
    (_UDP, 3389): "RDP (UDP)",
}
SERVICE_NAMES: Dict[int, str] = {
    20: "FTP data",
    21: "FTP",
    22: "SSH",
    23: "Telnet",
    25: "SMTP",
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    69: "TFTP",
    80: "HTTP",
    88: "Kerberos",
    110: "POP3",
    123: "NTP",
    135: "MS RPC",
    137: "NetBIOS",
    138: "NetBIOS",
    139: "NetBIOS",
    143: "IMAP",
    161: "SNMP",
    162: "SNMP trap",
    179: "BGP",
    389: "LDAP",
    443: "HTTPS",
    445: "SMB",
    465: "SMTPS",
    587: "SMTP submission",
    636: "LDAPS",
    873: "rsync",
    993: "IMAPS",
    995: "POP3S",
    1433: "MS SQL",
    1521: "Oracle",
    1723: "PPTP",
    1812: "RADIUS",
    1813: "RADIUS accounting",
    2049: "NFS",
    2055: "NetFlow",
    3268: "LDAP GC",
    3306: "MySQL",
    3389: "RDP",
    4739: "IPFIX",
    5060: "SIP",
    5061: "SIP TLS",
    5222: "XMPP",
    5432: "PostgreSQL",
    5900: "VNC",
    5985: "WinRM",
    5986: "WinRM",
    6343: "sFlow",
    6379: "Redis",
    8080: "HTTP-Alt",
    8443: "HTTPS-Alt",
    8801: "Zoom",
    9200: "Elasticsearch",
}


def service_name(protocol: int, port: int) -> Optional[str]:
    """The service's name, or None for a port with no name."""
    key = (int(protocol or 0), int(port or 0))
    return SERVICE_NAMES_BY_PROTOCOL.get(key) or SERVICE_NAMES.get(key[1])


def service_label(protocol: int, port: int) -> str:
    """"HTTPS", "QUIC (HTTP/3)", or "TCP/1434" when the port has no name."""
    name = service_name(protocol, port)
    if name:
        return name
    proto = PROTOCOL_MAP.get(int(protocol or 0), f"PROTO-{protocol}")
    return f"{proto}/{port}"


def tcp_anomaly_sql(flags_expr: str, protocol_expr: str) -> str:
    """SQL CASE yielding 'SYN_ONLY', 'TCP_RST' or NULL; the same rule as `tcp_anomaly`.

    Apply it to the OR of a conversation's flags over the window, so the overview KPI, the
    conversation table and the PDF report classify the same conversations.
    """
    return (
        f"CASE WHEN {protocol_expr} = 6 AND ({flags_expr} & {_TCP_SYN}) <> 0 AND ({flags_expr} & {_TCP_ACK}) = 0 "
        f"THEN '{ANOMALY_SYN_ONLY}' "
        f"WHEN {protocol_expr} = 6 AND ({flags_expr} & {_TCP_RST}) <> 0 AND ({flags_expr} & {_TCP_FIN}) = 0 "
        f"THEN '{ANOMALY_TCP_RST}' END"
    )


def tcp_anomaly(tcp_flags: Optional[int]) -> Optional[str]:
    """Classify a TCP flag mask (RFC 793).

    - SYN_ONLY: SYN without ACK, i.e. half-open connection attempts typical of port scans.
    - TCP_RST: RST without FIN, i.e. refused or reset connections.
    SYN_ONLY wins when both apply.
    """
    flags = int(tcp_flags or 0)
    if (flags & _TCP_SYN) and not (flags & _TCP_ACK):
        return ANOMALY_SYN_ONLY
    if (flags & _TCP_RST) and not (flags & _TCP_FIN):
        return ANOMALY_TCP_RST
    return None
