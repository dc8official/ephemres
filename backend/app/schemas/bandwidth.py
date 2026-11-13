from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Tuple
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_validator


def normalize_interface_aliases(v: Any) -> Dict[str, Dict[str, Any]]:
    """Stored `flow_interface_aliases` as {ifIndex: {name, speed_mbps, role}} for API responses.

    Legacy entries are plain name strings. `speed_mbps` is None unless a positive speed was set
    (utilization is then not shown), and `role` is "wan" only when an admin set it.
    """
    if not isinstance(v, dict):
        return {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for k, val in v.items():
        if hasattr(val, "model_dump"):
            val = val.model_dump()
        if isinstance(val, str):
            normalized[str(k)] = {"name": val, "speed_mbps": None, "role": "lan"}
        elif isinstance(val, dict):
            try:
                speed = int(val.get("speed_mbps") or 0)
            except (TypeError, ValueError):
                speed = 0
            normalized[str(k)] = {
                "name": str(val.get("name") or f"Interface {k}"),
                "speed_mbps": speed if speed > 0 else None,
                "role": "wan" if val.get("role") == "wan" else "lan",
            }
    return normalized


class WanLinkItem(BaseModel):
    """One WAN-role interface over the overview window. In is traffic arriving from outside."""
    exporter_id: UUID
    exporter_hostname: str
    interface_idx: int
    name: str
    in_bps: float = 0.0
    out_bps: float = 0.0
    in_pps: float = 0.0
    out_pps: float = 0.0
    # None when the link speed is not configured.
    speed_mbps: Optional[int] = None
    utilization_percentage: Optional[float] = None
    # Share of all WAN bytes (in + out) carried by this link, 0-100.
    share_percentage: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class BandwidthOverview(BaseModel):
    """Snapshot of the last five complete minutes of flow telemetry, fleet-wide or for one exporter.

    Directions follow the Traffic zones rule (internal = a monitored endpoint or a private
    address): ingress is external to internal, egress internal to external, internal stays
    inside, transit has neither end inside. Each conversation counts once, however many
    routers reported it. The Traffic page no longer shows these as tiles (review P3); the
    chart's "All flows" mode draws the same four directions.
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
    total_internal_bps: float = 0.0
    total_internal_pps: float = 0.0
    avg_packet_size: int = 0
    # Flows in, and number of, conversations whose OR'd TCP flags are SYN-only or RST-without-FIN.
    anomaly_flows_count: int = 0
    anomaly_conversations_count: int = 0
    window_seconds: int = 300
    exporter_id: Optional[UUID] = None
    # Edge totals over WAN-role interfaces only: what entered or left the network, from the
    # interface counters. All zero, with `wan_interfaces_count` 0, until an interface is WAN.
    wan_interfaces_count: int = 0
    wan_in_bps: float = 0.0
    wan_out_bps: float = 0.0
    wan_in_pps: float = 0.0
    wan_out_pps: float = 0.0
    wan_links: List[WanLinkItem] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


# --- The "Now" area (build plan 7.5 step 4, services/wan_links) ---------------------------------

class UsualBand(BaseModel):
    """The usual range of a link's hourly average for this hour of the week (services/traffic_usual)."""
    low_bps: float
    high_bps: float
    # How many past hours it was worked out from.
    samples: int


class WanLinkAlertItem(BaseModel):
    """An open traffic alert on the link: "high" (busy since, in one direction) or "silent"."""
    kind: Literal["high", "silent"]
    direction: Optional[Literal["in", "out"]] = None
    since: Optional[datetime] = None
    peak_pct: Optional[float] = None


class DownSpan(BaseModel):
    """A span SNMP saw the port down; `end` None while it still is."""
    start: datetime
    end: Optional[datetime] = None


class WanLinkSeriesItem(BaseModel):
    """One link's chart over the window (build plan 7.5 step 5), column by column: entry i of each
    list belongs to times[i]. Rates in bits/s; None where no data was reported (a gap)."""
    bucket_seconds: int
    times: List[datetime] = Field(default_factory=list)
    in_bps: List[Optional[float]] = Field(default_factory=list)
    out_bps: List[Optional[float]] = Field(default_factory=list)
    # Each hour's busiest minute (hourly buckets only; empty values for minute buckets).
    peak_in_bps: List[Optional[float]] = Field(default_factory=list)
    peak_out_bps: List[Optional[float]] = Field(default_factory=list)
    # The usual range for each bucket's hour of the week, as worked out now (28 days before it).
    usual_in_low: List[Optional[float]] = Field(default_factory=list)
    usual_in_high: List[Optional[float]] = Field(default_factory=list)
    usual_out_low: List[Optional[float]] = Field(default_factory=list)
    usual_out_high: List[Optional[float]] = Field(default_factory=list)
    down_spans: List[DownSpan] = Field(default_factory=list)
    # The busiest point in the window (either direction, its peak minute where kept), in bits/s
    # and in % of the speed when set: the page opens the busiest charts first.
    window_peak_bps: float = 0.0
    window_peak_percentage: Optional[float] = None


class WanLinkNowItem(BaseModel):
    """One WAN link now: the last five complete minutes against its capacity, its usual range for
    this hour and today's busiest minute. In is traffic arriving from outside."""
    exporter_id: UUID
    exporter_hostname: str
    site: Optional[str] = None
    interface_idx: int
    name: str
    # None when no speed is set (typed, or from SNMP): no utilisation, and the page offers "Set speed".
    speed_mbps: Optional[int] = None
    # "manual" (typed), "snmp" (the port's own speed) or "default" (none).
    speed_source: Literal["manual", "snmp", "default"] = "default"
    # What SNMP reports for the port, shown when a typed speed differs from it.
    snmp_speed_mbps: Optional[int] = None
    in_bps: float = 0.0
    out_bps: float = 0.0
    # The busier direction against the speed, 0-100.
    utilization_percentage: Optional[float] = None
    # Where the five minutes came from: SNMP counters if any minute had them, else flows.
    traffic_source: Literal["snmp", "flow"] = "flow"
    usual_in: Optional[UsualBand] = None
    usual_out: Optional[UsualBand] = None
    # "weekday_weekend" or "day_of_week" (from 21 days of data); None while there is no data.
    usual_grouping: Optional[str] = None
    # Local days with data in the last 28, which the range was worked out from.
    usual_days: int = 0
    # Percent above the top of the usual range, only when more than 10% over it.
    above_usual_in_pct: Optional[float] = None
    above_usual_out_pct: Optional[float] = None
    # Today's busiest minute (since the server's local midnight) and the hour it was in.
    today_peak_in_bps: Optional[float] = None
    today_peak_in_hour: Optional[datetime] = None
    today_peak_out_bps: Optional[float] = None
    today_peak_out_hour: Optional[datetime] = None
    # From SNMP: "up", "down", "shut_down" (by an admin), "not_answering" (the router doesn't
    # reply, so the port's state is unknown); None when the router isn't polled.
    link_state: Optional[Literal["up", "down", "shut_down", "not_answering"]] = None
    down_since: Optional[datetime] = None
    snmp_last_ok_at: Optional[datetime] = None
    alerts: List[WanLinkAlertItem] = Field(default_factory=list)
    # With ?window=…&series=1 only: the link's chart over the window.
    series: Optional[WanLinkSeriesItem] = None


class WanLinksResponse(BaseModel):
    """GET /bandwidth/wan-links: every WAN link of the routers in scope, grouped by site (none
    last) and busiest first within a site."""
    window_start: datetime
    window_end: datetime
    # The server's local midnight, where "today" starts, and its timezone name.
    day_start: datetime
    timezone: str
    links: List[WanLinkNowItem] = Field(default_factory=list)
    # The charts' window, when series were asked for.
    window: Optional[str] = None


class AnomalySummaryResponse(BaseModel):
    """GET /bandwidth/anomalies: suspicious conversations over the picked window, for the Security
    tiles, which follow the picker like the table under them (review P1, build plan 7.5 step 4).
    Same rule as the conversation table: TCP flags over the conversation SYN-only, or RST
    without FIN; each conversation counted once."""
    window: str
    conversations: int = 0
    flows: int = 0
    all_flows: int = 0
    syn_conversations: int = 0
    syn_flows: int = 0
    rst_conversations: int = 0
    rst_flows: int = 0


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
    # Both ends internal (fleet "All flows" only; see BandwidthOverview for the directions).
    internal_bps: float = 0.0
    internal_pps: float = 0.0
    flows_per_sec: float = 0.0

    model_config = ConfigDict(from_attributes=True)


class TrafficSeriesResponse(BaseModel):
    """Time-series query response encapsulating window metadata and data points.

    `bucket_seconds` is the width of each point. `directional` is False when the rollup tier
    keeps no endpoint attribution (fleet-wide 1y), in which case all bytes are reported as
    transit and ingress/egress are zero.
    """
    window: str
    points: List[TrafficSeriesPoint]
    bucket_seconds: int = 60
    directional: bool = True
    # True when the points are WAN-interface totals (ingress = in on WAN links, egress = out).
    wan_only: bool = False

    model_config = ConfigDict(from_attributes=True)


class TopTalkerItem(BaseModel):
    """Aggregated volume and packet statistics for an individual network host endpoint."""
    endpoint_id: Optional[UUID] = None
    ip_address: str
    hostname: Optional[str] = None
    total_bytes: int
    # From the host's side: ingress is what it received, egress what it sent.
    ingress_bytes: int
    egress_bytes: int
    flow_count: int
    total_packets: int = 0
    avg_packet_size: int = 0
    # The endpoint's site, else the sites of the routers that saw the address (sorted).
    sites: List[str] = Field(default_factory=list)

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
    bulk: bool = False,
) -> str:
    """Classifies network conversation traffic profile from observed packet physics and QoS markings.

    Heuristic Precedence:
    1. Security Anomalies:
       - Half-open SYN scans or probes (`SYN_ONLY`).
       - Connection resets or rejected attempts (`TCP_RST`).
    2. Bulk Transfer: `bulk`, the flow_metrics.is_bulk rule (full-size packets, seen for 30
       minutes or more, at least 5 Mb/s while seen). Typical of backups and replication.
    3. Download: full-size packets (`avg_packet_size >= 1100` bytes, `packets_per_flow >= 10`)
       without the steady volume of bulk, e.g. a Microsoft 365 or Windows Update download.
       Packet size alone used to call these "Bulk Transfer", so the Bulk filter did not single
       out the jobs that fill a link (review C2).
    4. Real-Time / VoIP:
       - RFC 3246 Expedited Forwarding (`dscp == 46` / 0x2E).
       - Standard SIP / RTP signaling and media ports (5060, 5061, 16384).
       - Checked BEFORE micro-flow heuristics to ensure small VoIP audio frames (e.g., G.711 / G.729
         at 60-120 B/pkt) are correctly attributed to voice/telephony rather than generic control traffic.
    5. Micro-Flow / Control:
       - Lightweight keepalives, health checks, or handshakes with small payload (`0 < avg_packet_size <= 120`)
         and low packet count (`0 < packets_per_flow <= 3`).
       - Strictly requires non-zero physics (`> 0`) to prevent unmeasured or zero-packet flows from false classification.
    6. Interactive:
       - Standard client-server transactions, web browsing, REST API requests, or general unclassified flows.
    """
    if anomaly_hint == "SYN_ONLY":
        return "SYN Scan / Probe"
    elif anomaly_hint == "TCP_RST":
        return "Reset / Refused"
    elif bulk:
        return "Bulk Transfer"
    elif avg_packet_size >= 1100 and packets_per_flow >= 10:
        return "Download"
    elif dscp == 46 or dst_port in (5060, 5061, 16384):
        return "Real-Time / VoIP"
    elif 0 < avg_packet_size <= 120 and 0 < packets_per_flow <= 3:
        return "Micro-Flow / Control"
    return "Interactive"


class ConversationRouter(BaseModel):
    """A router that reported a conversation in the window (review C1, S1).

    `wan_link` names the router's WAN link only when it has exactly one: the flow tables keep
    conversations per router, not per interface, so with two links the one it arrived on is not
    known (Kenneth, 2026-11-13). `hostname` is None for an exporter that is not an endpoint.
    """
    exporter_id: UUID
    hostname: Optional[str] = None
    site: Optional[str] = None
    wan_link: Optional[str] = None


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
    # flow_metrics.service_name: "HTTPS", "QUIC (HTTP/3)"; None for a port with no name. The
    # one port-name table, so the page and the PDF name ports alike (review P5).
    service_name: Optional[str] = None
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
    # When it was seen in the window: to the minute up to 2 hours, to the hour beyond (the
    # hourly tier). `last_seen` is the end of its last bucket, never later than now.
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    # The routers in scope that saw it, by hostname; its bytes are counted once (review C1).
    # Filled for GET /bandwidth/conversations; empty where a caller doesn't ask (the PDF).
    routers: List[ConversationRouter] = Field(default_factory=list)

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
    # Services, QoS and zones read the same rows (conversations.traffic_breakdown): the table
    # holding most of the window, and its start. The rows are kept 90 days, so a 1y window
    # starts 90 days back. qos_window_start and zones_window_start equal window_start; they
    # are kept for older clients.
    qos_source: Optional[str] = None
    window_start: Optional[datetime] = None
    qos_window_start: Optional[datetime] = None
    zones_window_start: Optional[datetime] = None
    traffic_zones: List[TrafficZoneItem] = Field(default_factory=list)
    total_bytes: int = 0
    # How zones were classified; always "ip" (addresses + monitored endpoints) since every
    # window reads tables that keep addresses. Kept for older clients.
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
    # The Traffic page's device picker groups devices by site (build plan 7.5 step 2).
    site: Optional[str] = None
    # Polled over SNMP (it has a credential set). A router polled over SNMP that exports no
    # flows is listed too, labelled "SNMP": its WAN traffic comes from its counters.
    snmp_polled: bool = False
    # Exports flows: marked as an exporter, given exporter addresses, or sent flows in 24 h.
    sends_flows: bool = True

    model_config = ConfigDict(from_attributes=True)

    @field_validator("interface_aliases", mode="before")
    @classmethod
    def normalize_aliases(cls, v):
        return normalize_interface_aliases(v)


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
    # The router (build plan 7.5 step 6: the table can list every router in scope).
    exporter_id: Optional[UUID] = None
    exporter_hostname: Optional[str] = None
    site: Optional[str] = None
    interface_idx: int
    name: str
    # Configured link speed, or the 1 Gbps default when `speed_configured` is False.
    speed_mbps: int
    speed_configured: bool = True
    # The link's traffic alert rule as stored ({"mode", "threshold_pct", "minutes"}), so the
    # interface editors can show it and keep it when they save the whole alias map (v3.3.6).
    alert: Optional[dict] = None
    role: Literal["wan", "lan"] = "lan"
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
    # Where `name` and `speed_mbps` come from (v3.3.6 SNMP poller): "manual" (typed in the
    # interface editor), "snmp" (the device's own, services/snmp_inventory) or "default".
    # The editors fill their fields from manual values only and show snmp_name /
    # snmp_speed_mbps as greyed defaults, so saving never turns an SNMP value into a manual one.
    name_source: Literal["manual", "snmp", "default"] = "default"
    speed_source: Literal["manual", "snmp", "default"] = "default"
    snmp_name: Optional[str] = None
    snmp_speed_mbps: Optional[int] = None
    oper_status: Optional[str] = None
    # Where the traffic figures come from (v3.3.6): "snmp" when any minute in the window came
    # from the device's counters (services/interface_traffic), else "flow".
    traffic_source: Literal["snmp", "flow"] = "flow"
    # Now: the last five complete minutes (review I3; in_bps / out_bps are the window's average).
    now_in_bps: float = 0.0
    now_out_bps: float = 0.0
    # From SNMP for a polled router: "up", "down", "shut_down" (admin down), "not_answering" (the
    # router doesn't reply), "unknown" (another oper status); for a router SNMP doesn't cover,
    # "traffic_seen" or "idle" from flows (review I2).
    status: Literal["up", "down", "shut_down", "not_answering", "unknown", "traffic_seen", "idle"] = "idle"
    admin_status: Optional[str] = None
    # Spans SNMP saw the port down within the window (end None: still down), and their total.
    down_spans: List[DownSpan] = Field(default_factory=list)
    down_seconds: int = 0

    model_config = ConfigDict(from_attributes=True)


class InterfaceTelemetryResponse(BaseModel):
    """Per-interface rates for one exporter, or across every router in scope (`exporter_id` None).

    Rates average over `covered_seconds`: from the later of the window start and the exporter's
    first data in it, to now. `p95_resolution_seconds` is 60 when p95 is over minutes and 3600
    when it is over hourly averages (30-day window); peaks are always the busiest minute.
    """
    exporter_id: Optional[UUID] = None
    window: str
    interfaces: List[InterfaceTelemetryItem]
    # How many routers the interfaces belong to.
    devices: int = 0
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
    """What ephemres-flowd needs: FastStore opens and its UDP ports are free (ready). A low
    rmem_max is reported but doesn't fail it. The Redis version and stream-write fields are
    gone: flowd no longer writes flows to a stream."""
    faststore_connected: bool
    rmem_max: Optional[int] = None
    rmem_max_supported: bool = True
    udp_ports_available: bool = True
    port_conflicts: List[int] = Field(default_factory=list)
    ready: bool
    message: str

    model_config = ConfigDict(from_attributes=True)
