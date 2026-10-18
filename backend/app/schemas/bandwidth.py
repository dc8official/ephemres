from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID
from app.core.schema import BaseModel, ConfigDict, Field, field_validator


class BandwidthOverview(BaseModel):
    total_ingress_bps: float
    total_egress_bps: float
    active_exporters_count: int
    unmatched_exporters_count: int
    total_flows_count: int

    model_config = ConfigDict(from_attributes=True)


class TrafficSeriesPoint(BaseModel):
    timestamp: str
    ingress_bps: float
    egress_bps: float

    model_config = ConfigDict(from_attributes=True)


class TrafficSeriesResponse(BaseModel):
    window: str
    points: List[TrafficSeriesPoint]

    model_config = ConfigDict(from_attributes=True)


class TopTalkerItem(BaseModel):
    endpoint_id: Optional[UUID] = None
    ip_address: str
    hostname: Optional[str] = None
    total_bytes: int
    ingress_bytes: int
    egress_bytes: int
    flow_count: int

    model_config = ConfigDict(from_attributes=True)


TCP_FLAG_MAP: List[Tuple[int, str]] = [
    (0x01, "FIN"),
    (0x02, "SYN"),
    (0x04, "RST"),
    (0x08, "PSH"),
    (0x10, "ACK"),
    (0x20, "URG"),
    (0x40, "ECE"),
    (0x80, "CWR"),
]

DSCP_CLASS_MAP: Dict[int, str] = {
    0: "Best Effort (CS0)",
    8: "Scavenger (CS1)",
    10: "AF11",
    16: "CS2",
    18: "AF21",
    24: "CS3",
    26: "AF31",
    32: "CS4",
    34: "AF41 (Video)",
    40: "CS5",
    46: "EF (VoIP)",
    48: "CS6 (Network Ctrl)",
    56: "CS7",
}


def format_tcp_flags(mask: int) -> List[str]:
    m = mask or 0
    return [name for bit, name in TCP_FLAG_MAP if m & bit]


def format_dscp_label(tos: int) -> str:
    dscp = ((tos or 0) & 0xFF) >> 2
    return DSCP_CLASS_MAP.get(dscp, f"DSCP {dscp}")


class TopConversationItem(BaseModel):
    src_ip: str
    dst_ip: str
    src_hostname: Optional[str] = None
    dst_hostname: Optional[str] = None
    protocol: str
    dst_port: int
    total_bytes: int
    flow_count: int
    tcp_flags: int = 0
    tcp_flags_labels: List[str] = Field(default_factory=list)
    tos: int = 0
    dscp: int = 0
    dscp_label: str = "Best Effort (CS0)"
    anomaly_hint: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class TopTalkersResponse(BaseModel):
    top_endpoints: List[TopTalkerItem]
    top_conversations: List[TopConversationItem]

    model_config = ConfigDict(from_attributes=True)


class ApplicationDistributionItem(BaseModel):
    protocol_name: str
    port: int
    service_label: str
    total_bytes: int
    percentage: float

    model_config = ConfigDict(from_attributes=True)


class ApplicationDistributionResponse(BaseModel):
    applications: List[ApplicationDistributionItem]

    model_config = ConfigDict(from_attributes=True)


class FlowExporterItem(BaseModel):
    id: UUID
    hostname: str
    primary_ip: str
    flow_exporter_ips: List[str] = Field(default_factory=list)
    interface_aliases: Dict[str, Any] = Field(default_factory=dict)
    device_role: Optional[str] = "FLOW_EXPORTER"
    last_flow_time: Optional[str] = None

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

    model_config = ConfigDict(from_attributes=True)


class ExportersResponse(BaseModel):
    exporters: List[FlowExporterItem]
    unmatched: List[UnmatchedExporterItem]

    model_config = ConfigDict(from_attributes=True)


class InterfaceTelemetryItem(BaseModel):
    interface_idx: int
    name: str
    speed_mbps: int
    in_bytes: int
    out_bytes: int
    in_bps: float
    out_bps: float
    in_packets: int
    out_packets: int
    flow_count: int
    utilization_percentage: float
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class InterfaceTelemetryResponse(BaseModel):
    exporter_id: UUID
    window: str
    interfaces: List[InterfaceTelemetryItem]

    model_config = ConfigDict(from_attributes=True)


class EnrollExporterRequest(BaseModel):
    ip_address: str
    hostname: str
    description: Optional[str] = None
    device_role: str = "FLOW_EXPORTER"


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
