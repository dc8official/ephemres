from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_validator


class BandwidthOverview(BaseModel):
    """Snapshot of the last five complete minutes of flow telemetry, fleet-wide or for one exporter.

    Ingress counts flows towards a monitored endpoint and egress flows from one; a flow between
    two monitored endpoints counts in both. Transit is traffic with no monitored endpoint on
    either side; it is reported on its own rather than split into ingress and egress.
    """
    total_ingress_bps: float
    total_egress_bps: float
    active_exporters_count: int
    unmatched_exporters_count: int
    total_flows_count: int
    total_ingress_pps: float = 0.0
    total_egress_pps: float = 0.0
    total_transit_bps: float = 0.0
    total_transit_pps: float = 0.0
    avg_packet_size: int = 0
    # Flows in, and number of, conversations whose OR'd TCP flags are SYN-only or RST-without-FIN.
    anomaly_flows_count: int = 0
    anomaly_conversations_count: int = 0
    window_seconds: int = 300
    exporter_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)


class TrafficSeriesPoint(BaseModel):
    """Single time-bucket interval point for traffic time-series charts.

    Tracks dual-mode throughput: bit rate (bps) for bandwidth capacity planning,
    and packet rate (pps) + flow rate (flows/sec) for PPS exhaustion and DDoS detection.
    """
    timestamp: str
    ingress_bps: float
    egress_bps: float
    ingress_pps: float = 0.0
    egress_pps: float = 0.0
    transit_bps: float = 0.0
    transit_pps: float = 0.0
    flows_per_sec: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class TrafficSeriesResponse(BaseModel):
    """Time-series query response encapsulating window metadata and data points.

    `bucket_seconds` is the width of each point. `directional` is False when the rollup tier
    keeps no endpoint attribution (fleet-wide 30d/1y), in which case all bytes are reported as
    transit and ingress/egress are zero.
    """
    window: str
    points: List[TrafficSeriesPoint]
    bucket_seconds: int = 60
    directional: bool = True

    model_config = ConfigDict(from_attributes=True)


class TopTalkerItem(BaseModel):
    """Aggregated volume and packet statistics for an individual network host endpoint."""
    endpoint_id: Optional[UUID] = None
    ip_address: str
    hostname: Optional[str] = None
    total_bytes: int
    ingress_bytes: int
    egress_bytes: int
    flow_count: int
    total_packets: int = 0
    avg_packet_size: int = 0

    model_config = ConfigDict(from_attributes=True)


# RFC 793 (Transmission Control Protocol) standard 6 control bits + RFC 3168 ECN bits.
# Used for bitwise decomposition of observed TCP flag bitmasks in NetFlow / IPFIX records.
TCP_FLAG_MAP: List[Tuple[int, str]] = [
    (0x01, "FIN"),  # Finished: sender has finished sending data
    (0x02, "SYN"),  # Synchronize sequence numbers (connection initiation)
    (0x04, "RST"),  # Reset the connection (abrupt termination / port closed)
    (0x08, "PSH"),  # Push function: push buffered data to receiver application
    (0x10, "ACK"),  # Acknowledgment field is significant
    (0x20, "URG"),  # Urgent pointer field is significant
    (0x40, "ECE"),  # ECN-Echo (RFC 3168): network congestion signaled
    (0x80, "CWR"),  # Congestion Window Reduced (RFC 3168)
]

# RFC 2474 (Differentiated Services Field), RFC 2597 (Assured Forwarding), RFC 3246 (Expedited Forwarding).
# Maps 6-bit DSCP codepoints (bits 0-5 of IPv4 ToS / IPv6 Traffic Class) to standard human-readable classes.
DSCP_CLASS_MAP: Dict[int, str] = {
    0: "Best Effort (CS0)",       # Default routing / unclassified traffic
    8: "Scavenger (CS1)",         # Low-priority background bulk transfers
    10: "AF11",                   # Assured Forwarding Class 1, low drop precedence
    16: "CS2",                    # High-throughput data / operations management
    18: "AF21",                   # Assured Forwarding Class 2, low drop precedence
    24: "CS3",                    # Call signaling (SIP, H.323)
    26: "AF31",                   # Assured Forwarding Class 3, low drop precedence
    32: "CS4",                    # Real-time interactive streaming video
    34: "AF41 (Video)",           # Assured Forwarding Class 4 (multimedia conferencing)
    40: "CS5",                    # Broadcast video / critical control
    46: "EF (VoIP)",              # Expedited Forwarding (RFC 3246): low-loss, low-latency voice
    48: "CS6 (Network Ctrl)",     # Network Control (BGP, OSPF routing protocol traffic)
    56: "CS7",                    # Reserved for critical infrastructure management
}


def format_tcp_flags(mask: int) -> List[str]:
    """Decomposes an 8-bit TCP control bitmask into human-readable flag tokens (RFC 793/3168)."""
    m = mask or 0
    return [name for bit, name in TCP_FLAG_MAP if m & bit]


def format_dscp_label(tos: int) -> str:
    """Extracts the 6-bit DSCP codepoint from the 8-bit IPv4 ToS / IPv6 Traffic Class byte (RFC 2474)

    and maps it to its standard DiffServ class name.
    Formula: dscp = (tos & 0xFF) >> 2 (the upper 6 bits represent DSCP; lower 2 bits represent ECN).
    """
    dscp = ((tos or 0) & 0xFF) >> 2
    return DSCP_CLASS_MAP.get(dscp, f"DSCP {dscp}")


def derive_traffic_profile(
    anomaly_hint: Optional[str],
    avg_packet_size: int,
    packets_per_flow: float,
    dscp: int,
    dst_port: int,
) -> str:
    """Classifies network conversation traffic profile from observed packet physics and QoS markings.

    Heuristic Precedence:
    1. Security Anomalies:
       - Half-open SYN scans or probes (`SYN_ONLY`).
       - Connection resets or rejected attempts (`TCP_RST`).
    2. Bulk Transfer:
       - High MTU frame occupancy (`avg_packet_size >= 1100` bytes) with sustained
         packet sequence (`packets_per_flow >= 10`). Typical of file transfers, backups, or large downloads.
    3. Real-Time / VoIP:
       - RFC 3246 Expedited Forwarding (`dscp == 46` / 0x2E).
       - Standard SIP / RTP signaling and media ports (5060, 5061, 16384).
       - Checked BEFORE micro-flow heuristics to ensure small VoIP audio frames (e.g., G.711 / G.729
         at 60-120 B/pkt) are correctly attributed to voice/telephony rather than generic control traffic.
    4. Micro-Flow / Control:
       - Lightweight keepalives, health checks, or handshakes with small payload (`0 < avg_packet_size <= 120`)
         and low packet count (`0 < packets_per_flow <= 3`).
       - Strictly requires non-zero physics (`> 0`) to prevent unmeasured or zero-packet flows from false classification.
    5. Interactive:
       - Standard client-server transactions, web browsing, REST API requests, or general unclassified flows.
    """
    if anomaly_hint == "SYN_ONLY":
        return "SYN Scan / Probe"
    elif anomaly_hint == "TCP_RST":
        return "Reset / Refused"
    elif avg_packet_size >= 1100 and packets_per_flow >= 10:
        return "Bulk Transfer"
    elif dscp == 46 or dst_port in (5060, 5061, 16384):
        return "Real-Time / VoIP"
    elif 0 < avg_packet_size <= 120 and 0 < packets_per_flow <= 3:
        return "Micro-Flow / Control"
    return "Interactive"


class TopConversationItem(BaseModel):
    """Enriched conversation item capturing peer communication, packet physics, and QoS/security metadata.

    Includes packet physics (avg_packet_size, packets_per_flow), derived traffic_profile classification,
    DiffServ DSCP class tagging, and security anomaly detection (SYN_ONLY, TCP_RST).
    """
    src_ip: str
    dst_ip: str
    src_hostname: Optional[str] = None
    dst_hostname: Optional[str] = None
    protocol: str
    dst_port: int
    total_bytes: int
    flow_count: int
    total_packets: int = 0
    avg_packet_size: int = 0
    packets_per_flow: float = 0.0
    traffic_profile: str = "Standard"
    tcp_flags: int = 0
    tcp_flags_labels: List[str] = Field(default_factory=list)
    tos: int = 0
    dscp: int = 0
    dscp_label: str = "Best Effort (CS0)"
    anomaly_hint: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TopTalkersResponse(BaseModel):
    """Aggregated endpoint talkers and peer conversations for a selected time window."""
    top_endpoints: List[TopTalkerItem]
    top_conversations: List[TopConversationItem]

    model_config = ConfigDict(from_attributes=True)


class ConversationsResponse(BaseModel):
    """One page of server-filtered conversations.

    `counts` holds the size of each quick filter (all, anomalies, qos, bulk) over every
    conversation matching the other filters; `total` is the count for the requested filter.
    """
    window: str
    items: List[TopConversationItem]
    counts: Dict[str, int]
    total: int
    limit: int
    offset: int

    model_config = ConfigDict(from_attributes=True)


class ApplicationDistributionItem(BaseModel):
    """Protocol and destination port share of overall fleet traffic."""
    protocol_name: str
    port: int
    service_label: str
    total_bytes: int
    # Share of all traffic in the window (the list holds the top 15 plus an "Other" row).
    percentage: float
    is_other: bool = False

    model_config = ConfigDict(from_attributes=True)


class QoSDistributionItem(BaseModel):
    """Differentiated Services (DiffServ) DSCP class traffic distribution across bytes and packets."""
    dscp: int
    dscp_label: str
    total_bytes: int
    total_packets: int
    flow_count: int
    percentage: float

    model_config = ConfigDict(from_attributes=True)


class TrafficZoneItem(BaseModel):
    """Directional traffic categorization based on endpoint topology and RFC 1918 private addressing:

    - East-West (Internal): Lateral communication between two known/private endpoints.
    - North-South Ingress: Inbound traffic from external sources to internal endpoints.
    - North-South Egress: Outbound traffic from internal endpoints to external destinations.
    - External / Transit: Multi-hop external traffic traversing monitored exporters.
    """
    zone: str
    total_bytes: int
    flow_count: int
    percentage: float

    model_config = ConfigDict(from_attributes=True)


class ApplicationDistributionResponse(BaseModel):
    """Composite distribution response containing service applications, QoS classes, and traffic zones."""
    applications: List[ApplicationDistributionItem]
    qos_classes: List[QoSDistributionItem] = Field(default_factory=list)
    # Rollup table and window start the QoS breakdown was read from. These differ from the
    # applications query when the hourly/daily tier lacks `tos` (TimescaleDB continuous aggregates).
    qos_source: Optional[str] = None
    qos_window_start: Optional[datetime] = None
    traffic_zones: List[TrafficZoneItem] = Field(default_factory=list)
    total_bytes: int = 0
    # How zones were classified: "ip" (addresses + monitored endpoints, minute tier), "endpoint"
    # (monitored endpoints only, hourly tier) or "unavailable" (daily tier keeps neither).
    zones_resolution: str = "ip"

    model_config = ConfigDict(from_attributes=True)


class FlowExporterItem(BaseModel):
    id: UUID
    hostname: str
    primary_ip: str
    flow_exporter_ips: List[str] = Field(default_factory=list)
    interface_aliases: Dict[str, Any] = Field(default_factory=dict)
    device_role: Optional[str] = "FLOW_EXPORTER"
    # Newest minute bucket with flows from this exporter in the last 24 h (None: silent for 24 h).
    last_flow_time: Optional[str] = None
    # Flow records per second over the last five complete minutes.
    flows_per_sec: float = 0.0

    model_config = ConfigDict(from_attributes=True)

    @field_validator("interface_aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, v):
        if not isinstance(v, dict):
            return {}
        normalized = {}
        for k, val in v.items():
            if isinstance(val, str):
                normalized[str(k)] = {"name": val, "speed_mbps": 1000}
            elif isinstance(val, dict):
                normalized[str(k)] = {
                    "name": str(val.get("name", f"Interface {k}")),
                    "speed_mbps": int(val.get("speed_mbps", 1000)),
                }
            elif hasattr(val, "model_dump"):
                normalized[str(k)] = val.model_dump()
            else:
                normalized[str(k)] = val
        return normalized


class UnmatchedExporterItem(BaseModel):
    ip_address: str
    last_seen: str
    # Recent flow rate reported by the flow daemon; None until it has two sightings.
    flows_per_sec: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


class ExportersResponse(BaseModel):
    exporters: List[FlowExporterItem]
    unmatched: List[UnmatchedExporterItem]

    model_config = ConfigDict(from_attributes=True)


class InterfaceTelemetryItem(BaseModel):
    interface_idx: int
    name: str
    # Configured link speed, or the 1 Gbps default when `speed_configured` is False.
    speed_mbps: int
    speed_configured: bool = True
    in_bytes: int
    out_bytes: int
    in_bps: float
    out_bps: float
    in_packets: int
    out_packets: int
    flow_count: int
    # None when the link speed is not configured: utilization against a guessed speed is not shown.
    utilization_percentage: Optional[float] = None
    is_active: bool
    # 95th percentile and busiest minute over the covered period (idle slots count as 0).
    in_p95_bps: float = 0.0
    out_p95_bps: float = 0.0
    in_peak_bps: float = 0.0
    out_peak_bps: float = 0.0
    p95_utilization_percentage: Optional[float] = None
    peak_utilization_percentage: Optional[float] = None

    model_config = ConfigDict(from_attributes=True)


class InterfaceTelemetryResponse(BaseModel):
    """Per-interface rates for one exporter.

    Rates average over `covered_seconds`: from the later of the window start and the exporter's
    first data in it, to now. `p95_resolution_seconds` is 60 when p95 is over minutes and 3600
    when it is over hourly averages (30-day window); peaks are always the busiest minute.
    """
    exporter_id: UUID
    window: str
    interfaces: List[InterfaceTelemetryItem]
    covered_seconds: int = 0
    p95_resolution_seconds: int = 60
    source: str = "flow_interface_minute_rollups"

    model_config = ConfigDict(from_attributes=True)


class EnrollExporterRequest(BaseModel):
    ip_address: str
    hostname: str
    description: Optional[str] = None
    # Exporter roles listed by GET /bandwidth/exporters; validated here so a bad value is a 422,
    # not an IntegrityError from ck_endpoints_device_role.
    device_role: Literal["FLOW_EXPORTER", "HYBRID_GATEWAY"] = "FLOW_EXPORTER"


class MapExporterRequest(BaseModel):
    endpoint_id: UUID


class FlowPreflightResponse(BaseModel):
    redis_connected: bool
    redis_version: Optional[str] = None
    redis_version_supported: bool
    faststore_connected: Optional[bool] = None
    faststore_version: Optional[str] = None
    faststore_version_supported: Optional[bool] = None
    stream_write_success: bool
    rmem_max: Optional[int] = None
    rmem_max_supported: bool = True
    udp_ports_available: bool = True
    port_conflicts: List[int] = Field(default_factory=list)
    ready: bool
    message: str

    model_config = ConfigDict(from_attributes=True)
