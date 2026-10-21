from __future__ import annotations

import datetime
import ipaddress
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query, status
from app.core.orm import case, func, or_, select, text
from app.core.orm import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.endpoint import Endpoint
from app.models.flow_rollup import (
    FlowDailyRollup,
    FlowHourlyRollup,
    FlowInterfaceMinuteRollup,
    FlowMinuteRollup,
)
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse
from app.schemas.bandwidth import (
    ApplicationDistributionItem,
    ApplicationDistributionResponse,
    BandwidthOverview,
    EnrollExporterRequest,
    ExportersResponse,
    FlowExporterItem,
    InterfaceTelemetryItem,
    InterfaceTelemetryResponse,
    MapExporterRequest,
    QoSDistributionItem,
    TopConversationItem,
    TopTalkerItem,
    TopTalkersResponse,
    TrafficSeriesPoint,
    TrafficSeriesResponse,
    TrafficZoneItem,
    UnmatchedExporterItem,
    derive_traffic_profile,
    format_dscp_label,
    format_tcp_flags,
)
from app.schemas.endpoints import InterfaceConfig
from app.services.driver_manager import driver_manager

logger = logging.getLogger(__name__)

SENTINEL_UUID = UUID("00000000-0000-0000-0000-000000000000")

router = APIRouter(prefix="/bandwidth", tags=["bandwidth"])

_flow_redis_pool: Optional[Any] = None


async def _init_flow_store_pool() -> Optional[Any]:
    """
    Returns an async FastStore client for flow telemetry operations.
    Reuses driver_manager's client if available; otherwise initializes an independent pool.
    """
    global _flow_redis_pool
    dm_client = getattr(driver_manager, "store_client", getattr(driver_manager, "_store_client", getattr(driver_manager, "_redis_client", None)))
    if dm_client is not None:
        return dm_client

    faststore_cfg = getattr(settings, "faststore", getattr(settings, "redis", None))
    if faststore_cfg is not None and not getattr(faststore_cfg, "enabled", True):
        return None

    if _flow_redis_pool is None:
        try:
            from app.core.faststore import Redis
            _flow_redis_pool = Redis(
                host=faststore_cfg.host if faststore_cfg else "localhost",
                port=faststore_cfg.port if faststore_cfg else 6379,
                db=faststore_cfg.db if faststore_cfg else 0,
                password=(faststore_cfg.password or None) if faststore_cfg else None,
                decode_responses=True,
                socket_timeout=1.0,
                socket_connect_timeout=1.0,
            )
        except Exception as e:
            logger.debug("Failed to initialize standalone FastStore client for flow: %s", e)
            return None
    return _flow_redis_pool


async def get_flow_store() -> Optional[Any]:
    return await get_flow_redis()


async def get_flow_redis() -> Optional[Any]:
    return await _init_flow_store_pool()


PROTOCOL_MAP = {
    1: "ICMP",
    6: "TCP",
    17: "UDP",
    47: "GRE",
    50: "ESP",
    58: "IPv6-ICMP",
    89: "OSPF",
}

PORT_LABEL_MAP = {
    22: "SSH",
    53: "DNS",
    80: "HTTP",
    123: "NTP",
    161: "SNMP",
    443: "HTTPS",
    8080: "HTTP-Alt",
    8443: "HTTPS-Alt",
    3306: "MySQL",
    5432: "PostgreSQL",
    6379: "Redis",
    9200: "Elasticsearch",
    2055: "NetFlow",
    4739: "IPFIX",
}


def _get_window_delta(window: str) -> datetime.timedelta:
    w = window.lower().strip()
    if w == "1h":
        return datetime.timedelta(hours=1)
    if w == "6h":
        return datetime.timedelta(hours=6)
    if w == "24h":
        return datetime.timedelta(hours=24)
    if w == "7d":
        return datetime.timedelta(days=7)
    if w == "30d":
        return datetime.timedelta(days=30)
    if w == "1y":
        return datetime.timedelta(days=365)
    return datetime.timedelta(hours=1)


def _select_rollup_model_and_bucket_seconds(
    window: str, endpoint_id: Optional[UUID] = None
) -> Tuple[Any, float]:
    """Selects the optimal hypertable or continuous aggregate ORM model and bucket duration (seconds)

    aligned with TimescaleDB continuous aggregate policies:
    - 1h, 6h: FlowMinuteRollup (60s buckets, full IP/interface/ToS/flag resolution, retained 90 days).
    - 24h, 7d: FlowHourlyRollup (3600s buckets, endpoint-correlated aggregation, retained 30 days).
    - 30d, 1y: FlowDailyRollup (86400s buckets, protocol/port compaction, retained 365 days).
      Note: If endpoint_id filtering is requested for 30d/1y, fall back to FlowHourlyRollup since
      FlowDailyRollup compresses away individual endpoint keys to optimize disk usage.
    """
    w = window.lower().strip()
    if w in ("1h", "6h"):
        return FlowMinuteRollup, 60.0
    elif w in ("24h", "7d"):
        return FlowHourlyRollup, 3600.0
    elif w in ("30d", "1y"):
        if endpoint_id is not None:
            return FlowHourlyRollup, 3600.0
        return FlowDailyRollup, 86400.0
    else:
        return FlowMinuteRollup, 60.0


@router.get("/overview", response_model=APIResponse)
async def get_bandwidth_overview(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Fleet-wide real-time Ingress/Egress throughput, packet rates, and forensic anomalies.

    Aggregates flow telemetry over the trailing 5-minute rolling window:
    - Ingress/Egress bit rates (bps): (bytes * 8.0) / 300.0.
    - Ingress/Egress packet rates (pps): packets / 300.0.
    - Mean packet physics (avg_packet_size): total_bytes / total_packets.
    - Security anomalies: TCP SYN scans (half-open probes) and TCP RST bursts (rejected connections).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    five_min_ago = now - datetime.timedelta(minutes=5)

    stmt = select(
        func.coalesce(func.sum(FlowMinuteRollup.bytes), 0).label("total_bytes"),
        func.coalesce(
            func.sum(
                case(
                    (FlowMinuteRollup.dst_endpoint_id.is_not(None), FlowMinuteRollup.bytes),
                    else_=0,
                )
            ),
            0,
        ).label("ingress_bytes"),
        func.coalesce(
            func.sum(
                case(
                    (FlowMinuteRollup.src_endpoint_id.is_not(None), FlowMinuteRollup.bytes),
                    else_=0,
                )
            ),
            0,
        ).label("egress_bytes"),
        func.coalesce(func.sum(FlowMinuteRollup.flow_count), 0).label("total_flows"),
        func.count(
            func.distinct(
                case(
                    (FlowMinuteRollup.exporter_id != SENTINEL_UUID, FlowMinuteRollup.exporter_id),
                    else_=None,
                )
            )
        ).label("active_exporters"),
        func.coalesce(func.sum(FlowMinuteRollup.packets), 0).label("total_packets"),
        func.coalesce(
            func.sum(
                case(
                    (FlowMinuteRollup.dst_endpoint_id.is_not(None), FlowMinuteRollup.packets),
                    else_=0,
                )
            ),
            0,
        ).label("ingress_packets"),
        func.coalesce(
            func.sum(
                case(
                    (FlowMinuteRollup.src_endpoint_id.is_not(None), FlowMinuteRollup.packets),
                    else_=0,
                )
            ),
            0,
        ).label("egress_packets"),
    ).where(FlowMinuteRollup.bucket >= five_min_ago)

    res = await db.execute(stmt)
    row = res.one_or_none()

    total_packets = 0
    ingress_packets = 0
    egress_packets = 0

    if row and len(row) >= 8:
        total_bytes = int(row[0] or 0)
        ingress_bytes = int(row[1] or 0)
        egress_bytes = int(row[2] or 0)
        total_flows = int(row[3] or 0)
        active_exporters = int(row[4] or 0)
        total_packets = int(row[5] or 0)
        ingress_packets = int(row[6] or 0)
        egress_packets = int(row[7] or 0)
        if ingress_bytes == 0 and egress_bytes == 0 and total_bytes > 0:
            ingress_bytes = total_bytes // 2
            egress_bytes = total_bytes - ingress_bytes
        if ingress_packets == 0 and egress_packets == 0 and total_packets > 0:
            ingress_packets = total_packets // 2
            egress_packets = total_packets - ingress_packets
    elif row and len(row) >= 5:
        total_bytes = int(row[0] or 0)
        ingress_bytes = int(row[1] or 0)
        egress_bytes = int(row[2] or 0)
        total_flows = int(row[3] or 0)
        active_exporters = int(row[4] or 0)
        if ingress_bytes == 0 and egress_bytes == 0 and total_bytes > 0:
            ingress_bytes = total_bytes // 2
            egress_bytes = total_bytes - ingress_bytes
    elif row and len(row) >= 3:
        total_bytes = int(row[0] or 0)
        total_flows = int(row[1] or 0)
        active_exporters = int(row[2] or 0)
        ingress_bytes = total_bytes // 2
        egress_bytes = total_bytes - ingress_bytes
    else:
        total_bytes = ingress_bytes = egress_bytes = total_flows = active_exporters = 0

    # Calculate average bps and pps over 300 seconds
    ingress_bps = (float(ingress_bytes) * 8.0) / 300.0 if ingress_bytes > 0 else 0.0
    egress_bps = (float(egress_bytes) * 8.0) / 300.0 if egress_bytes > 0 else 0.0
    total_ingress_pps = round(float(ingress_packets) / 300.0, 2) if ingress_packets > 0 else 0.0
    total_egress_pps = round(float(egress_packets) / 300.0, 2) if egress_packets > 0 else 0.0
    avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0

    # Calculate anomaly flows count over 5-minute window (TCP SYN scan or RST bursts)
    stmt_anomaly = (
        select(
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.tcp_flags,
            func.sum(FlowMinuteRollup.flow_count),
        )
        .where(
            FlowMinuteRollup.bucket >= five_min_ago,
            FlowMinuteRollup.protocol == 6,
        )
        .group_by(FlowMinuteRollup.protocol, FlowMinuteRollup.tcp_flags)
    )
    anomaly_flows_count = 0
    try:
        res_anomaly = await db.execute(stmt_anomaly)
        for a_row in res_anomaly.all():
            a_flags = int(a_row[1] or 0)
            a_cnt = int(a_row[2] or 0)
            # SYN_ONLY (0x02 and not 0x10) or TCP_RST (0x04 and not 0x01)
            if ((a_flags & 0x02) and not (a_flags & 0x10)) or ((a_flags & 0x04) and not (a_flags & 0x01)):
                anomaly_flows_count += a_cnt
    except Exception as exc:
        logger.debug("Failed to calculate anomaly flow count: %s", exc)

    # Fetch unmatched count from FastStore
    unmatched_count = 0
    store_client = await get_flow_store()
    if store_client:
        try:
            unmatched_count = await store_client.zcard("flow:unmatched:zset")
            if not unmatched_count:
                unmatched_count = await store_client.scard("flow:unmatched:set")
        except Exception:
            try:
                unmatched_count = await store_client.scard("flow:unmatched:set")
            except Exception:
                pass

    return APIResponse.success(
        data=BandwidthOverview(
            total_ingress_bps=round(ingress_bps, 2),
            total_egress_bps=round(egress_bps, 2),
            active_exporters_count=active_exporters,
            unmatched_exporters_count=unmatched_count,
            total_flows_count=total_flows,
            total_ingress_pps=total_ingress_pps,
            total_egress_pps=total_egress_pps,
            avg_packet_size=avg_packet_size,
            anomaly_flows_count=anomaly_flows_count,
        )
    )


@router.get("/traffic-series", response_model=APIResponse)
async def get_traffic_series(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    endpoint_id: Optional[UUID] = Query(default=None),
    interface_idx: Optional[int] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns time-series data points for Chart.js stacked area chart.
    Dynamically routes to 1-minute hypertable, 1-hour CAGG, or 1-day CAGG based on window.
    For flow exporter nodes and per-interface isolation, queries FlowInterfaceMinuteRollup.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta

    # If querying an exporter node (without specific endpoint_id) or isolating an interface:
    if exporter_id and not endpoint_id:
        stmt = (
            select(
                FlowInterfaceMinuteRollup.bucket,
                func.coalesce(func.sum(FlowInterfaceMinuteRollup.in_bytes), 0).label("in_bytes"),
                func.coalesce(func.sum(FlowInterfaceMinuteRollup.out_bytes), 0).label("out_bytes"),
                func.coalesce(func.sum(FlowInterfaceMinuteRollup.in_packets), 0).label("in_pkts"),
                func.coalesce(func.sum(FlowInterfaceMinuteRollup.out_packets), 0).label("out_pkts"),
                func.coalesce(func.sum(FlowInterfaceMinuteRollup.flow_count), 0).label("flow_count"),
            )
            .where(
                FlowInterfaceMinuteRollup.exporter_id == exporter_id,
                FlowInterfaceMinuteRollup.bucket >= start_time,
            )
        )
        if interface_idx is not None:
            stmt = stmt.where(FlowInterfaceMinuteRollup.interface_idx == interface_idx)

        stmt = stmt.group_by(FlowInterfaceMinuteRollup.bucket).order_by(FlowInterfaceMinuteRollup.bucket.asc())
        res = await db.execute(stmt)
        rows = res.all()

        points: List[TrafficSeriesPoint] = []
        for row in rows:
            bucket_time = row[0]
            in_bytes = int(row[1] or 0)
            eg_bytes = int(row[2] or 0)
            in_pkts = int(row[3] or 0) if len(row) > 3 else 0
            eg_pkts = int(row[4] or 0) if len(row) > 4 else 0
            flows_cnt = int(row[5] or 0) if len(row) > 5 else 0

            # Each row in FlowInterfaceMinuteRollup represents a 1-minute bucket (60 seconds)
            in_bps = (float(in_bytes) * 8.0) / 60.0
            eg_bps = (float(eg_bytes) * 8.0) / 60.0
            in_pps = round(float(in_pkts) / 60.0, 2)
            eg_pps = round(float(eg_pkts) / 60.0, 2)
            flows_per_sec = round(float(flows_cnt) / 60.0, 2)

            iso_str = bucket_time.astimezone(datetime.timezone.utc).isoformat()
            points.append(
                TrafficSeriesPoint(
                    timestamp=iso_str,
                    ingress_bps=round(in_bps, 2),
                    egress_bps=round(eg_bps, 2),
                    ingress_pps=in_pps,
                    egress_pps=eg_pps,
                    flows_per_sec=flows_per_sec,
                )
            )

        return APIResponse.success(
            data=TrafficSeriesResponse(window=window, points=points)
        )

    # Standard endpoint or fleet-wide flow rollup query
    model, bucket_seconds = _select_rollup_model_and_bucket_seconds(window, endpoint_id)

    filters = [model.bucket >= start_time]
    if exporter_id:
        filters.append(model.exporter_id == exporter_id)
    if endpoint_id and hasattr(model, "src_endpoint_id"):
        filters.append(
            (model.src_endpoint_id == endpoint_id)
            | (model.dst_endpoint_id == endpoint_id)
        )

    # Ingress / Egress calculations via case expressions
    if endpoint_id and hasattr(model, "src_endpoint_id"):
        ingress_calc = func.sum(
            case((model.dst_endpoint_id == endpoint_id, model.bytes), else_=0)
        )
        egress_calc = func.sum(
            case((model.src_endpoint_id == endpoint_id, model.bytes), else_=0)
        )
        ingress_pkt_calc = func.sum(
            case((model.dst_endpoint_id == endpoint_id, model.packets), else_=0)
        )
        egress_pkt_calc = func.sum(
            case((model.src_endpoint_id == endpoint_id, model.packets), else_=0)
        )
    elif hasattr(model, "src_endpoint_id"):
        ingress_calc = func.sum(
            case((model.dst_endpoint_id.is_not(None), model.bytes), else_=0)
        )
        egress_calc = func.sum(
            case((model.src_endpoint_id.is_not(None), model.bytes), else_=0)
        )
        ingress_pkt_calc = func.sum(
            case((model.dst_endpoint_id.is_not(None), model.packets), else_=0)
        )
        egress_pkt_calc = func.sum(
            case((model.src_endpoint_id.is_not(None), model.packets), else_=0)
        )
    else:
        ingress_calc = func.sum(model.bytes) / 2
        egress_calc = func.sum(model.bytes) / 2
        ingress_pkt_calc = func.sum(model.packets) / 2
        egress_pkt_calc = func.sum(model.packets) / 2

    stmt = (
        select(
            model.bucket,
            func.coalesce(func.sum(model.bytes), 0).label("total_bytes"),
            func.coalesce(ingress_calc, 0).label("ingress_bytes"),
            func.coalesce(egress_calc, 0).label("egress_bytes"),
            func.coalesce(func.sum(model.packets), 0).label("total_packets"),
            func.coalesce(ingress_pkt_calc, 0).label("ingress_packets"),
            func.coalesce(egress_pkt_calc, 0).label("egress_packets"),
            func.coalesce(func.sum(model.flow_count), 0).label("flow_count"),
        )
        .where(*filters)
        .group_by(model.bucket)
        .order_by(model.bucket.asc())
    )

    res = await db.execute(stmt)
    rows = res.all()

    points: List[TrafficSeriesPoint] = []
    for row in rows:
        bucket_time = row[0]
        byte_sum = int(row[1] or 0)
        in_bytes = int(row[2] or 0) if len(row) > 2 else 0
        eg_bytes = int(row[3] or 0) if len(row) > 3 else 0
        pkt_sum = int(row[4] or 0) if len(row) > 4 else 0
        in_pkts = int(row[5] or 0) if len(row) > 5 else 0
        eg_pkts = int(row[6] or 0) if len(row) > 6 else 0
        flows_cnt = int(row[7] or 0) if len(row) > 7 else 0

        # If there is transit traffic not captured by monitored endpoint attribution:
        transit_bytes = max(0, byte_sum - (in_bytes + eg_bytes))
        if transit_bytes > 0:
            in_bytes += transit_bytes // 2
            eg_bytes += (transit_bytes - transit_bytes // 2)
        elif in_bytes == 0 and eg_bytes == 0 and byte_sum > 0:
            in_bytes = byte_sum // 2
            eg_bytes = byte_sum - in_bytes

        transit_pkts = max(0, pkt_sum - (in_pkts + eg_pkts))
        if transit_pkts > 0:
            in_pkts += transit_pkts // 2
            eg_pkts += (transit_pkts - transit_pkts // 2)
        elif in_pkts == 0 and eg_pkts == 0 and pkt_sum > 0:
            in_pkts = pkt_sum // 2
            eg_pkts = pkt_sum - in_pkts

        in_bps = (float(in_bytes) * 8.0) / bucket_seconds
        eg_bps = (float(eg_bytes) * 8.0) / bucket_seconds
        in_pps = round(float(in_pkts) / bucket_seconds, 2)
        eg_pps = round(float(eg_pkts) / bucket_seconds, 2)
        flows_per_sec = round(float(flows_cnt) / bucket_seconds, 2)

        iso_str = bucket_time.astimezone(datetime.timezone.utc).isoformat()
        points.append(
            TrafficSeriesPoint(
                timestamp=iso_str,
                ingress_bps=round(in_bps, 2),
                egress_bps=round(eg_bps, 2),
                ingress_pps=in_pps,
                egress_pps=eg_pps,
                flows_per_sec=flows_per_sec,
            )
        )

    return APIResponse.success(
        data=TrafficSeriesResponse(window=window, points=points)
    )


@router.get("/top-talkers", response_model=APIResponse)
async def get_top_talkers(
    window: str = Query(default="1h"),
    limit: int = Query(default=25, ge=1, le=200),
    endpoint_id: Optional[UUID] = Query(default=None),
    exporter_id: Optional[UUID] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns top communicating endpoints and top IP conversations.
    Supports scoping by router/exporter_id for transit flow forensics.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta

    # 1. Top endpoints (True Ingress & Egress accounting across src_ip and dst_ip)
    model = FlowMinuteRollup
    ep_filters = [model.bucket >= start_time]
    if exporter_id is not None:
        ep_filters.append(model.exporter_id == exporter_id)
    if endpoint_id is not None:
        ep_filters.append(
            or_(
                model.src_endpoint_id == endpoint_id,
                model.dst_endpoint_id == endpoint_id,
            )
        )

    egress_stmt = (
        select(
            model.src_ip,
            model.src_endpoint_id,
            func.sum(model.bytes).label("egress_bytes"),
            func.sum(model.flow_count).label("egress_flows"),
            func.coalesce(func.sum(model.packets), 0).label("egress_packets"),
        )
        .where(*ep_filters)
        .group_by(model.src_ip, model.src_endpoint_id)
        .order_by(func.sum(model.bytes).desc())
        .limit(limit * 3)
    )

    ingress_stmt = (
        select(
            model.dst_ip,
            model.dst_endpoint_id,
            func.sum(model.bytes).label("ingress_bytes"),
            func.sum(model.flow_count).label("ingress_flows"),
            func.coalesce(func.sum(model.packets), 0).label("ingress_packets"),
        )
        .where(*ep_filters)
        .group_by(model.dst_ip, model.dst_endpoint_id)
        .order_by(func.sum(model.bytes).desc())
        .limit(limit * 3)
    )

    egress_res = await db.execute(egress_stmt)
    ingress_res = await db.execute(ingress_stmt)

    egress_map: Dict[str, Dict[str, Any]] = {}
    for row in egress_res.all():
        ip_str = str(row[0])
        ep_id = row[1]
        eg_b = int(row[2] or 0)
        eg_f = int(row[3] or 0)
        eg_p = int(row[4] or 0) if len(row) > 4 else 0
        egress_map[ip_str] = {"ep_id": ep_id, "bytes": eg_b, "flows": eg_f, "packets": eg_p}

    ingress_map: Dict[str, Dict[str, Any]] = {}
    for row in ingress_res.all():
        ip_str = str(row[0])
        ep_id = row[1]
        in_b = int(row[2] or 0)
        in_f = int(row[3] or 0)
        in_p = int(row[4] or 0) if len(row) > 4 else 0
        ingress_map[ip_str] = {"ep_id": ep_id, "bytes": in_b, "flows": in_f, "packets": in_p}

    all_ips = set(egress_map.keys()) | set(ingress_map.keys())
    merged_candidates: List[Dict[str, Any]] = []
    for ip_str in all_ips:
        eg = egress_map.get(ip_str, {"ep_id": None, "bytes": 0, "flows": 0, "packets": 0})
        ing = ingress_map.get(ip_str, {"ep_id": None, "bytes": 0, "flows": 0, "packets": 0})
        eg_bytes = eg["bytes"]
        in_bytes = ing["bytes"]
        tot_bytes = eg_bytes + in_bytes
        tot_flows = eg["flows"] + ing["flows"]
        tot_pkts = eg["packets"] + ing["packets"]
        avg_pkt_sz = int(tot_bytes // tot_pkts) if tot_pkts > 0 else 0
        ep_id = eg["ep_id"] or ing["ep_id"]
        merged_candidates.append({
            "ip": ip_str,
            "endpoint_id": ep_id,
            "ingress_bytes": in_bytes,
            "egress_bytes": eg_bytes,
            "total_bytes": tot_bytes,
            "flow_count": tot_flows,
            "total_packets": tot_pkts,
            "avg_packet_size": avg_pkt_sz,
        })

    merged_candidates.sort(key=lambda x: x["total_bytes"], reverse=True)
    selected_candidates = merged_candidates[:limit]

    # Load hostnames for known endpoints
    ep_ids = [c["endpoint_id"] for c in selected_candidates if c["endpoint_id"]]
    hostnames: Dict[UUID, str] = {}
    if ep_ids:
        h_stmt = select(Endpoint.id, Endpoint.hostname).where(Endpoint.id.in_(ep_ids))
        h_res = await db.execute(h_stmt)
        for e_id, h_name in h_res.all():
            hostnames[e_id] = h_name

    ip_to_hostname: Dict[str, str] = {}
    for c in selected_candidates:
        if c.get("endpoint_id") and c["endpoint_id"] in hostnames:
            c_ip_clean = str(c["ip"]).split("/")[0].strip()
            ip_to_hostname[c_ip_clean] = hostnames[c["endpoint_id"]]
            ip_to_hostname[str(c["ip"])] = hostnames[c["endpoint_id"]]

    top_endpoints: List[TopTalkerItem] = []
    for c in selected_candidates:
        top_endpoints.append(
            TopTalkerItem(
                endpoint_id=c["endpoint_id"],
                ip_address=c["ip"],
                hostname=hostnames.get(c["endpoint_id"]),
                total_bytes=c["total_bytes"],
                ingress_bytes=c["ingress_bytes"],
                egress_bytes=c["egress_bytes"],
                flow_count=c["flow_count"],
                total_packets=c["total_packets"],
                avg_packet_size=c["avg_packet_size"],
            )
        )

    # 2. Top Conversations
    conv_filters = [FlowMinuteRollup.bucket >= start_time]
    if exporter_id is not None:
        conv_filters.append(FlowMinuteRollup.exporter_id == exporter_id)
    if endpoint_id is not None:
        conv_filters.append(
            or_(
                FlowMinuteRollup.src_endpoint_id == endpoint_id,
                FlowMinuteRollup.dst_endpoint_id == endpoint_id,
            )
        )

    top_conv_stmt = (
        select(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.dst_port,
            func.sum(FlowMinuteRollup.bytes).label("total_bytes"),
            func.sum(FlowMinuteRollup.flow_count).label("total_flows"),
            func.bit_or(FlowMinuteRollup.tcp_flags).label("tcp_flags_mask"),
            func.max(FlowMinuteRollup.tos).label("max_tos"),
            func.coalesce(func.sum(FlowMinuteRollup.packets), 0).label("total_packets"),
        )
        .where(*conv_filters)
        .group_by(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.dst_port,
        )
        .order_by(func.sum(FlowMinuteRollup.bytes).desc())
        .limit(limit)
    )

    conv_res = await db.execute(top_conv_stmt)
    top_conversations: List[TopConversationItem] = []
    for row in conv_res.all():
        s_ip = row[0]
        d_ip = row[1]
        proto_id = row[2]
        port = row[3]
        b_sum = int(row[4] or 0)
        f_sum = int(row[5] or 0)
        tcp_flags = int(row[6] or 0) if len(row) > 6 else 0
        tos = int(row[7] or 0) if len(row) > 7 else 0
        tot_packets = int(row[8] or 0) if len(row) > 8 else 0

        proto_name = PROTOCOL_MAP.get(proto_id, f"PROTO-{proto_id}")
        flags_labels = format_tcp_flags(tcp_flags) if proto_name == "TCP" else []
        dscp_val = (tos & 0xFF) >> 2
        dscp_lbl = format_dscp_label(tos)

        anomaly_hint = None
        if proto_name == "TCP":
            # SYN_ONLY: SYN bit set (0x02) and not ACK (0x10)
            if (tcp_flags & 0x02) and not (tcp_flags & 0x10):
                anomaly_hint = "SYN_ONLY"
            # TCP_RST: RST bit set (0x04) and not FIN (0x01)
            elif (tcp_flags & 0x04) and not (tcp_flags & 0x01):
                anomaly_hint = "TCP_RST"

        avg_pkt_size = int(b_sum // tot_packets) if tot_packets > 0 else 0
        pkts_per_flow = round(float(tot_packets) / float(f_sum), 1) if f_sum > 0 else 0.0
        traffic_prof = derive_traffic_profile(anomaly_hint, avg_pkt_size, pkts_per_flow, dscp_val, int(port))

        s_ip_str = str(s_ip)
        d_ip_str = str(d_ip)
        s_ip_clean = s_ip_str.split("/")[0].strip()
        d_ip_clean = d_ip_str.split("/")[0].strip()
        s_host = ip_to_hostname.get(s_ip_clean) or ip_to_hostname.get(s_ip_str)
        d_host = ip_to_hostname.get(d_ip_clean) or ip_to_hostname.get(d_ip_str)

        top_conversations.append(
            TopConversationItem(
                src_ip=s_ip_str,
                dst_ip=d_ip_str,
                src_hostname=s_host,
                dst_hostname=d_host,
                protocol=proto_name,
                dst_port=int(port),
                total_bytes=b_sum,
                flow_count=f_sum,
                total_packets=tot_packets,
                avg_packet_size=avg_pkt_size,
                packets_per_flow=pkts_per_flow,
                traffic_profile=traffic_prof,
                tcp_flags=tcp_flags,
                tcp_flags_labels=flags_labels,
                tos=tos,
                dscp=dscp_val,
                dscp_label=dscp_lbl,
                anomaly_hint=anomaly_hint,
            )
        )

    return APIResponse.success(
        data=TopTalkersResponse(
            top_endpoints=top_endpoints, top_conversations=top_conversations
        )
    )


def _rollup_window_filters(
    model: Any,
    start_time: datetime.datetime,
    exporter_id: Optional[UUID],
    endpoint_id: Optional[UUID],
) -> List[Any]:
    filters = [model.bucket >= start_time]
    if exporter_id is not None:
        filters.append(model.exporter_id == exporter_id)
    if endpoint_id is not None and hasattr(model, "src_endpoint_id"):
        filters.append(
            or_(
                model.src_endpoint_id == endpoint_id,
                model.dst_endpoint_id == endpoint_id,
            )
        )
    return filters


# flow_minute_rollups retention (migration 0010); the QoS fallback never reads further back.
_MINUTE_ROLLUP_RETENTION = datetime.timedelta(days=90)

# (table, column) -> exists; schema only changes through migrations, which restart the API.
# The cache is per worker, so rebuilding a continuous aggregate also needs an API restart.
_TABLE_COLUMN_CACHE: Dict[Tuple[str, str], bool] = {}


async def _db_table_has_column(db: AsyncSession, table: str, column: str) -> bool:
    """True if `column` exists on `table` (or continuous-aggregate view) in the live database."""
    key = (table, column)
    if key not in _TABLE_COLUMN_CACHE:
        res = await db.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column LIMIT 1"
            ),
            {"table": table, "column": column},
        )
        _TABLE_COLUMN_CACHE[key] = res.scalar() is not None
    return _TABLE_COLUMN_CACHE[key]


@router.get("/applications", response_model=APIResponse)
async def get_applications_breakdown(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    endpoint_id: Optional[UUID] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns protocol and service port volume distribution, QoS/DSCP class breakdown,

    and directional Traffic Zone breakdown.

    Analytics Pipeline:
    1. Service Port Breakdown: Top 15 (protocol, dst_port) combinations mapped to standard service names.
    2. QoS Class Compliance (RFC 2474 / 3246): 6-bit DSCP codepoint extraction from the ToS/Traffic Class
       byte, grouping byte volume, packet counts, and flow counts across CS0-CS7, AF11-AF41, and EF.
    3. Directional Traffic Zones:
       - East-West (Internal): Lateral communication between two monitored/RFC 1918 private endpoints.
       - North-South Ingress: External internet sources sending traffic to internal cluster endpoints.
       - North-South Egress: Internal cluster endpoints sending traffic to external destinations.
       - External / Transit: Monitored router transit flows between unmanaged external peers.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta

    model, _ = _select_rollup_model_and_bucket_seconds(window, endpoint_id)
    app_filters = _rollup_window_filters(model, start_time, exporter_id, endpoint_id)

    # 1. Top 15 (protocol, dst_port) Application Services
    stmt = (
        select(
            model.protocol,
            model.dst_port,
            func.sum(model.bytes).label("total_bytes"),
        )
        .where(*app_filters)
        .group_by(model.protocol, model.dst_port)
        .order_by(func.sum(model.bytes).desc())
        .limit(15)
    )

    res = await db.execute(stmt)
    rows = res.all()

    overall_bytes = sum(int(getattr(r, "total_bytes", r[2]) or 0) for r in rows)

    apps: List[ApplicationDistributionItem] = []
    for proto_id, port, byte_sum in rows:
        b_val = int(byte_sum or 0)
        p_name = PROTOCOL_MAP.get(proto_id, f"P-{proto_id}")
        label = PORT_LABEL_MAP.get(port, f"{p_name}/{port}")
        pct = (b_val / overall_bytes * 100.0) if overall_bytes > 0 else 0.0
        apps.append(
            ApplicationDistributionItem(
                protocol_name=p_name,
                port=int(port),
                service_label=label,
                total_bytes=b_val,
                percentage=round(pct, 2),
            )
        )

    # 2. QoS / DSCP Breakdown
    # On TimescaleDB the hourly/daily tiers are continuous aggregates that migration 0013 could
    # not extend with `tos`; fall back to the 1-minute tier (90-day retention) for ToS data.
    # The response reports the tier and start actually used, since a 1y window then only gets
    # 90 days of QoS data next to 365 days of application data.
    qos_model = model
    qos_filters = app_filters
    qos_start = start_time
    if model is not FlowMinuteRollup and not await _db_table_has_column(db, model.__tablename__, "tos"):
        qos_model = FlowMinuteRollup
        qos_start = max(start_time, now - _MINUTE_ROLLUP_RETENTION)
        qos_filters = _rollup_window_filters(qos_model, qos_start, exporter_id, endpoint_id)
    qos_stmt = (
        select(
            qos_model.tos,
            func.coalesce(func.sum(qos_model.bytes), 0).label("total_bytes"),
            func.coalesce(func.sum(qos_model.packets), 0).label("total_packets"),
            func.coalesce(func.sum(qos_model.flow_count), 0).label("flow_count"),
        )
        .where(*qos_filters)
        .group_by(qos_model.tos)
    )
    qos_res = await db.execute(qos_stmt)
    dscp_buckets: Dict[int, Dict[str, Any]] = {}
    total_qos_bytes = 0
    for q_row in qos_res.all():
        if len(q_row) < 2:
            continue
        tos_val = int(q_row[0] or 0)
        q_bytes = int(q_row[1] or 0)
        q_pkts = int(q_row[2] or 0) if len(q_row) > 2 else 0
        q_flows = int(q_row[3] or 0) if len(q_row) > 3 else 0
        dscp_val = (tos_val & 0xFF) >> 2
        total_qos_bytes += q_bytes
        if dscp_val not in dscp_buckets:
            dscp_buckets[dscp_val] = {
                "dscp": dscp_val,
                "dscp_label": format_dscp_label(tos_val),
                "total_bytes": 0,
                "total_packets": 0,
                "flow_count": 0,
            }
        dscp_buckets[dscp_val]["total_bytes"] += q_bytes
        dscp_buckets[dscp_val]["total_packets"] += q_pkts
        dscp_buckets[dscp_val]["flow_count"] += q_flows

    qos_classes: List[QoSDistributionItem] = []
    sorted_dscp = sorted(dscp_buckets.values(), key=lambda x: x["total_bytes"], reverse=True)
    for item in sorted_dscp:
        pct = (item["total_bytes"] / total_qos_bytes * 100.0) if total_qos_bytes > 0 else 0.0
        qos_classes.append(
            QoSDistributionItem(
                dscp=item["dscp"],
                dscp_label=item["dscp_label"],
                total_bytes=item["total_bytes"],
                total_packets=item["total_packets"],
                flow_count=item["flow_count"],
                percentage=round(pct, 2),
            )
        )

    # 3. Traffic Zone Breakdown (East-West, North-South, External)
    zone_sums = {
        "East-West (Internal)": {"bytes": 0, "flows": 0},
        "North-South Egress": {"bytes": 0, "flows": 0},
        "North-South Ingress": {"bytes": 0, "flows": 0},
        "External / Transit": {"bytes": 0, "flows": 0},
    }

    if hasattr(model, "src_ip"):
        zone_stmt = (
            select(
                model.src_ip,
                model.dst_ip,
                model.src_endpoint_id,
                model.dst_endpoint_id,
                func.sum(model.bytes).label("total_bytes"),
                func.sum(model.flow_count).label("flow_count"),
            )
            .where(*app_filters)
            .group_by(
                model.src_ip,
                model.dst_ip,
                model.src_endpoint_id,
                model.dst_endpoint_id,
            )
        )
        zone_res = await db.execute(zone_stmt)

        def _is_internal(ip_val: str, ep_val: Optional[Any]) -> bool:
            if ep_val is not None:
                return True
            try:
                clean_ip = str(ip_val or "").split("/")[0].strip()
                if not clean_ip:
                    return False
                return ipaddress.ip_address(clean_ip).is_private
            except Exception:
                return False

        for z_row in zone_res.all():
            if len(z_row) < 5:
                continue
            s_ip = str(z_row[0])
            d_ip = str(z_row[1])
            s_ep = z_row[2]
            d_ep = z_row[3]
            z_b = int(z_row[4] or 0)
            z_f = int(z_row[5] or 0) if len(z_row) > 5 else 0

            src_int = _is_internal(s_ip, s_ep)
            dst_int = _is_internal(d_ip, d_ep)
            if src_int and dst_int:
                z_key = "East-West (Internal)"
            elif src_int and not dst_int:
                z_key = "North-South Egress"
            elif not src_int and dst_int:
                z_key = "North-South Ingress"
            else:
                z_key = "External / Transit"

            zone_sums[z_key]["bytes"] += z_b
            zone_sums[z_key]["flows"] += z_f
    elif hasattr(model, "src_endpoint_id"):
        # Medium-term aggregation tier (FlowHourlyRollup): individual IP addresses are pruned,
        # but correlated src_endpoint_id and dst_endpoint_id are preserved.
        zone_stmt = (
            select(
                model.src_endpoint_id,
                model.dst_endpoint_id,
                func.sum(model.bytes).label("total_bytes"),
                func.sum(model.flow_count).label("flow_count"),
            )
            .where(*app_filters)
            .group_by(
                model.src_endpoint_id,
                model.dst_endpoint_id,
            )
        )
        zone_res = await db.execute(zone_stmt)
        for z_row in zone_res.all():
            if len(z_row) < 3:
                continue
            s_ep = z_row[0]
            d_ep = z_row[1]
            z_b = int(z_row[2] or 0)
            z_f = int(z_row[3] or 0) if len(z_row) > 3 else 0

            # Endpoint topology classification:
            # - Both known endpoints: East-West lateral mesh traffic.
            # - Source known, destination unknown: North-South egress out of cluster.
            # - Source unknown, destination known: North-South ingress into cluster.
            # - Neither known: External perimeter transit.
            if s_ep is not None and d_ep is not None:
                z_key = "East-West (Internal)"
            elif s_ep is not None and d_ep is None:
                z_key = "North-South Egress"
            elif s_ep is None and d_ep is not None:
                z_key = "North-South Ingress"
            else:
                z_key = "External / Transit"

            zone_sums[z_key]["bytes"] += z_b
            zone_sums[z_key]["flows"] += z_f
    else:
        # Long-term aggregation tier (FlowDailyRollup):
        # To maintain high compaction across 365-day storage windows, FlowDailyRollup compresses
        # away individual IP and endpoint telemetry. Traffic zones cannot be resolved at the daily
        # rollup level without expensive raw joins, so zone distributions remain zeroed rather than
        # failing with an AttributeError.
        pass

    total_zone_bytes = sum(z["bytes"] for z in zone_sums.values())
    traffic_zones: List[TrafficZoneItem] = []
    for z_name in [
        "East-West (Internal)",
        "North-South Egress",
        "North-South Ingress",
        "External / Transit",
    ]:
        data = zone_sums[z_name]
        pct = (data["bytes"] / total_zone_bytes * 100.0) if total_zone_bytes > 0 else 0.0
        traffic_zones.append(
            TrafficZoneItem(
                zone=z_name,
                total_bytes=data["bytes"],
                flow_count=data["flows"],
                percentage=round(pct, 2),
            )
        )

    return APIResponse.success(
        data=ApplicationDistributionResponse(
            applications=apps,
            qos_classes=qos_classes,
            qos_source=qos_model.__tablename__,
            qos_window_start=qos_start,
            traffic_zones=traffic_zones,
        )
    )


@router.get("/exporters", response_model=APIResponse)
async def list_flow_exporters(
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Lists configured flow exporters and detected unmatched exporter candidates.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    since_24h = now_utc - datetime.timedelta(hours=24)

    recent_exporters_sub = (
        select(FlowMinuteRollup.exporter_id)
        .where(FlowMinuteRollup.bucket >= since_24h)
        .distinct()
    )

    recent_if_exporters_sub = (
        select(FlowInterfaceMinuteRollup.exporter_id)
        .where(FlowInterfaceMinuteRollup.bucket >= since_24h)
        .distinct()
    )

    stmt = select(Endpoint).where(
        Endpoint.deleted_at.is_(None),
        Endpoint.endpoint_status != "DELETED",
        or_(
            Endpoint.device_role.in_(["FLOW_EXPORTER", "HYBRID_GATEWAY"]),
            func.cardinality(Endpoint.flow_exporter_ips) > 0,
            Endpoint.id.in_(recent_exporters_sub),
            Endpoint.id.in_(recent_if_exporters_sub),
        ),
    ).order_by(Endpoint.hostname.asc())
    res = await db.execute(stmt)
    endpoints = res.scalars().all()

    exporters: List[FlowExporterItem] = []
    for ep in endpoints:
        exp_ips = [str(ip) for ip in (ep.flow_exporter_ips or [])]
        aliases = ep.flow_interface_aliases or {}
        exporters.append(
            FlowExporterItem(
                id=ep.id,
                hostname=ep.hostname,
                primary_ip=str(ep.ip_address).split("/")[0],
                flow_exporter_ips=exp_ips,
                interface_aliases=aliases,
                device_role=getattr(ep, "device_role", "ACTIVE_HOST"),
                last_flow_time=None,
            )
        )

    unmatched_list: List[UnmatchedExporterItem] = []
    store_client = await get_flow_store()
    if store_client:
        try:
            now_ts = time.time()
            cutoff_ts = now_ts - 86400.0
            # 1. Prune and query FastStore ZSET flow:unmatched:zset (sliding 24h window)
            await store_client.zremrangebyscore("flow:unmatched:zset", "-inf", cutoff_ts)
            zset_members = await store_client.zrevrangebyscore(
                "flow:unmatched:zset", "+inf", "-inf", withscores=True, start=0, num=100
            )
            seen_ips = set()
            if zset_members:
                for item in zset_members:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        ip_val, score_val = item
                    else:
                        continue
                    ip_str = ip_val.decode("utf-8") if isinstance(ip_val, bytes) else str(ip_val)
                    try:
                        dt = datetime.datetime.fromtimestamp(float(score_val), tz=datetime.timezone.utc)
                        unmatched_list.append(
                            UnmatchedExporterItem(
                                ip_address=ip_str,
                                last_seen=dt.isoformat(),
                            )
                        )
                        seen_ips.add(ip_str)
                    except (ValueError, TypeError):
                        pass

            # 2. Complement with legacy set if any exists not in zset
            unmatched_ips = await store_client.smembers("flow:unmatched:set")
            for ip in unmatched_ips:
                ip_clean = ip.decode("utf-8") if isinstance(ip, bytes) else str(ip)
                if ip_clean in seen_ips:
                    continue
                ts_str = await store_client.get(f"flow:unmatched:{ip_clean}")
                if ts_str:
                    try:
                        ts_val = float(ts_str)
                        dt = datetime.datetime.fromtimestamp(ts_val, tz=datetime.timezone.utc)
                        unmatched_list.append(
                            UnmatchedExporterItem(
                                ip_address=ip_clean,
                                last_seen=dt.isoformat(),
                            )
                        )
                    except ValueError:
                        pass
                else:
                    await store_client.srem("flow:unmatched:set", ip)
        except Exception as e:
            logger.debug("Failed to retrieve unmatched exporters from FastStore: %s", e)

    return APIResponse.success(
        data=ExportersResponse(exporters=exporters, unmatched=unmatched_list)
    )


@router.post("/exporters/enroll", response_model=APIResponse)
async def enroll_flow_exporter(
    payload: EnrollExporterRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-protected 1-click enrollment for discovered NetFlow/IPFIX exporters.
    Creates an endpoint with device_role='FLOW_EXPORTER' and monitoring_enabled=False.
    """
    clean_ip = payload.ip_address.strip().split("/")[0].strip()
    try:
        ipaddress.ip_address(clean_ip)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{clean_ip}' is not a valid IP address.",
        )

    clean_hostname = payload.hostname.strip()
    if not clean_hostname:
        clean_hostname = f"flow-exporter-{clean_ip.replace('.', '-')}"

    # Check for duplicate IP or hostname
    dup_stmt = select(Endpoint).where(
        or_(
            Endpoint.ip_address == clean_ip,
            Endpoint.hostname == clean_hostname,
        ),
        Endpoint.deleted_at.is_(None),
    )
    dup_res = await db.execute(dup_stmt)
    if dup_res.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An endpoint with IP '{clean_ip}' or hostname '{clean_hostname}' already exists.",
        )

    endpoint = Endpoint(
        hostname=clean_hostname,
        ip_address=clean_ip,
        device_type="Router",
        device_role=payload.device_role or "FLOW_EXPORTER",
        description=payload.description or f"Flow Exporter enrolled from {clean_ip}",
        monitoring_enabled=False,
        endpoint_status="ACTIVE",
        flow_exporter_ips=[clean_ip],
        flow_interface_aliases={},
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)

    # Clean up from FastStore unmatched tracking
    store_client = await get_flow_store()
    if store_client:
        try:
            await store_client.delete(f"flow:unmatched:{clean_ip}")
            await store_client.srem("flow:unmatched:set", clean_ip)
            await store_client.zrem("flow:unmatched:zset", clean_ip)
            await store_client.publish(
                "channel:registry_sync",
                f'{{"event": "EXPORTER_ENROLLED", "endpoint_id": "{endpoint.id}", "ip": "{clean_ip}"}}',
            )
        except Exception as e:
            logger.warning("Failed to clear FastStore unmatched state for %s: %s", clean_ip, e)

    return APIResponse.success(
        data={
            "id": str(endpoint.id),
            "hostname": endpoint.hostname,
            "ip_address": str(endpoint.ip_address),
            "device_role": endpoint.device_role,
            "monitoring_enabled": endpoint.monitoring_enabled,
            "message": f"Successfully enrolled {endpoint.hostname} as flow exporter.",
        }
    )


@router.post("/exporters/{ip}/map", response_model=APIResponse)
async def map_unmatched_exporter(
    ip: str,
    payload: MapExporterRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    One-click binds an unmatched exporter IP to an existing endpoint's flow_exporter_ips.
    """
    clean_ip = unquote(ip).strip().split("/")[0].strip()
    try:
        ipaddress.ip_address(clean_ip)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{clean_ip}' is not a valid IP address.",
        )

    stmt = select(Endpoint).where(
        Endpoint.id == payload.endpoint_id,
        Endpoint.deleted_at.is_(None),
    )
    res = await db.execute(stmt)
    endpoint = res.scalar_one_or_none()
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Endpoint {payload.endpoint_id} not found.",
        )

    existing_ips = list(endpoint.flow_exporter_ips or [])
    if clean_ip not in existing_ips:
        existing_ips.append(clean_ip)
        endpoint.flow_exporter_ips = existing_ips
        await db.commit()

    # Clear from FastStore unmatched tracking (both ZSET and legacy SET)
    store_client = await get_flow_store()
    if store_client:
        try:
            await store_client.delete(f"flow:unmatched:{clean_ip}")
            await store_client.srem("flow:unmatched:set", clean_ip)
            await store_client.zrem("flow:unmatched:zset", clean_ip)
            await store_client.publish(
                "channel:registry_sync",
                f'{{"event": "EXPORTER_MAPPED", "endpoint_id": "{payload.endpoint_id}", "ip": "{clean_ip}"}}',
            )
        except Exception as e:
            logger.warning("Failed to publish registry sync message to FastStore: %s", e)

    return APIResponse.success(
        data={"message": f"Successfully mapped exporter IP {clean_ip} to {endpoint.hostname}."}
    )


@router.get("/interfaces", response_model=APIResponse)
async def get_interfaces_telemetry(
    exporter_id: UUID = Query(...),
    window: str = Query(default="1h"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns interface-level telemetry metrics for an exporter node.
    Calculates ingress/egress bps and link utilization.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta
    window_seconds = max(1.0, delta.total_seconds())

    # Get endpoint for alias & speed mappings
    ep_stmt = select(Endpoint).where(
        Endpoint.id == exporter_id,
        Endpoint.deleted_at.is_(None),
    )
    ep_res = await db.execute(ep_stmt)
    endpoint = ep_res.scalar_one_or_none()
    aliases = getattr(endpoint, "flow_interface_aliases", {}) or {}

    # Query flow_interface_minute_rollups
    stmt = (
        select(
            FlowInterfaceMinuteRollup.interface_idx,
            func.sum(FlowInterfaceMinuteRollup.in_bytes).label("in_bytes"),
            func.sum(FlowInterfaceMinuteRollup.out_bytes).label("out_bytes"),
            func.sum(FlowInterfaceMinuteRollup.in_packets).label("in_packets"),
            func.sum(FlowInterfaceMinuteRollup.out_packets).label("out_packets"),
            func.sum(FlowInterfaceMinuteRollup.flow_count).label("flow_count"),
        )
        .where(
            FlowInterfaceMinuteRollup.exporter_id == exporter_id,
            FlowInterfaceMinuteRollup.bucket >= start_time,
        )
        .group_by(FlowInterfaceMinuteRollup.interface_idx)
        .order_by(FlowInterfaceMinuteRollup.interface_idx.asc())
    )
    res = await db.execute(stmt)
    rows = res.all()

    # Active interfaces from FastStore if available
    active_store_indices: set[int] = set()
    active_redis_indices = active_store_indices
    store_client = await get_flow_store()
    if store_client:
        try:
            members = await store_client.smembers(f"flow:exporter:{exporter_id}:interfaces")
            for m in members:
                try:
                    active_store_indices.add(int(m))
                except ValueError:
                    pass
        except Exception as e:
            logger.debug("Failed to read active interfaces from FastStore: %s", e)

    # Collect known interface indexes from rollups and configured aliases
    all_if_indices: dict[int, dict[str, Any]] = {}
    for row in rows:
        idx = int(row[0])
        all_if_indices[idx] = {
            "in_bytes": int(row[1] or 0) if len(row) > 1 else 0,
            "out_bytes": int(row[2] or 0) if len(row) > 2 else 0,
            "in_packets": int(row[3] or 0) if len(row) > 3 else 0,
            "out_packets": int(row[4] or 0) if len(row) > 4 else 0,
            "flow_count": int(row[5] or 0) if len(row) > 5 else 0,
        }

    # Include any interfaces configured in aliases even if 0 traffic recorded
    for k_str, val in aliases.items():
        try:
            k_int = int(k_str)
            if k_int not in all_if_indices:
                all_if_indices[k_int] = {
                    "in_bytes": 0,
                    "out_bytes": 0,
                    "in_packets": 0,
                    "out_packets": 0,
                    "flow_count": 0,
                }
        except ValueError:
            pass

    interfaces: List[InterfaceTelemetryItem] = []
    for if_idx in sorted(all_if_indices.keys()):
        stats = all_if_indices[if_idx]
        in_b = stats["in_bytes"]
        out_b = stats["out_bytes"]
        in_bps = (in_b * 8.0) / window_seconds
        out_bps = (out_b * 8.0) / window_seconds

        # Resolve alias and configured speed
        alias_data = aliases.get(str(if_idx))
        if isinstance(alias_data, dict):
            name = str(alias_data.get("name", f"Interface {if_idx}"))
            speed_mbps = int(alias_data.get("speed_mbps", 1000))
        elif isinstance(alias_data, str):
            name = alias_data
            speed_mbps = 1000
        else:
            name = f"Interface {if_idx}"
            speed_mbps = 1000

        speed_mbps = max(1, speed_mbps)
        capacity_bps = speed_mbps * 1_000_000.0
        peak_bps = max(in_bps, out_bps)
        utilization = round(min(100.0, (peak_bps / capacity_bps) * 100.0), 2)
        is_active = (if_idx in active_store_indices) or (stats["flow_count"] > 0) or ((in_b + out_b) > 0)

        interfaces.append(
            InterfaceTelemetryItem(
                interface_idx=if_idx,
                name=name,
                speed_mbps=speed_mbps,
                in_bytes=in_b,
                out_bytes=out_b,
                in_bps=round(in_bps, 2),
                out_bps=round(out_bps, 2),
                in_packets=stats["in_packets"],
                out_packets=stats["out_packets"],
                flow_count=stats["flow_count"],
                utilization_percentage=utilization,
                is_active=is_active,
            )
        )

    return APIResponse.success(
        data=InterfaceTelemetryResponse(
            exporter_id=exporter_id,
            window=window,
            interfaces=interfaces,
        )
    )


@router.put("/endpoints/{id}/interfaces", response_model=APIResponse)
async def update_endpoint_interfaces(
    id: UUID,
    payload: Dict[str, InterfaceConfig],
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-protected endpoint to configure interface names and link capacities.
    """
    stmt = select(Endpoint).where(
        Endpoint.id == id,
        Endpoint.deleted_at.is_(None),
    )
    res = await db.execute(stmt)
    endpoint = res.scalar_one_or_none()
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Endpoint {id} not found.",
        )

    aliases: Dict[str, Any] = {}
    for idx_str, config in payload.items():
        try:
            int(idx_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid interface index '{idx_str}'. Must be an integer string.",
            )
        aliases[idx_str] = config.model_dump()

    endpoint.flow_interface_aliases = aliases
    await db.commit()

    return APIResponse.success(
        data={
            "id": str(endpoint.id),
            "hostname": endpoint.hostname,
            "flow_interface_aliases": endpoint.flow_interface_aliases,
            "message": f"Successfully updated {len(aliases)} interfaces for {endpoint.hostname}.",
        }
    )
