"""Flow telemetry calculations shared by the bandwidth API and the PDF report compiler.

Both `app.routers.bandwidth` and `app.services.report_compiler` turn the same rollup rows into
rates, utilization, QoS classes, traffic zones and TCP anomaly counts. Keeping that arithmetic
here means the dashboard and the report cannot drift apart.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.schemas.bandwidth import format_dscp_label

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


def is_internal_address(ip_value: Any, endpoint_id: Optional[Any]) -> bool:
    """True for a monitored endpoint or a private (RFC 1918 etc.) address."""
    if endpoint_id is not None:
        return True
    try:
        clean_ip = str(ip_value or "").split("/")[0].strip()
        if not clean_ip:
            return False
        return ipaddress.ip_address(clean_ip).is_private
    except Exception:
        return False


def classify_zone(src_internal: bool, dst_internal: bool) -> str:
    """Traffic zone for a flow whose ends are internal or external."""
    if src_internal and dst_internal:
        return ZONE_EAST_WEST
    if src_internal:
        return ZONE_NORTH_SOUTH_EGRESS
    if dst_internal:
        return ZONE_NORTH_SOUTH_INGRESS
    return ZONE_EXTERNAL_TRANSIT


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
