from __future__ import annotations

import datetime
import ipaddress
import logging
import time
from typing import Any, Dict, List, Optional
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
    TopConversationItem,
    TopTalkerItem,
    TopTalkersResponse,
    TrafficSeriesPoint,
    TrafficSeriesResponse,
    UnmatchedExporterItem,
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
    """
    Selects the optimal hypertable or continuous aggregate model and bucket interval.
    1h, 6h -> FlowMinuteRollup (60s)
    24h, 7d -> FlowHourlyRollup (3600s)
    30d, 1y -> FlowDailyRollup (86400s) or FlowHourlyRollup if endpoint_id filtered
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
    """
    Fleet-wide real-time Ingress/Egress bps, active exporter count, and unmatched candidates.
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
    ).where(FlowMinuteRollup.bucket >= five_min_ago)

    res = await db.execute(stmt)
    row = res.one_or_none()

    if row and len(row) >= 5:
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

    # Calculate average bps over 300 seconds
    ingress_bps = (float(ingress_bytes) * 8.0) / 300.0 if ingress_bytes > 0 else 0.0
    egress_bps = (float(egress_bytes) * 8.0) / 300.0 if egress_bytes > 0 else 0.0

    # Fetch unmatched count from FastStore
    unmatched_count = 0
    store_client = await get_flow_store()
    if store_client:
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
            # Each row in FlowInterfaceMinuteRollup represents a 1-minute bucket (60 seconds)
            in_bps = (float(in_bytes) * 8.0) / 60.0
            eg_bps = (float(eg_bytes) * 8.0) / 60.0
            iso_str = bucket_time.astimezone(datetime.timezone.utc).isoformat()
            points.append(
                TrafficSeriesPoint(
                    timestamp=iso_str,
                    ingress_bps=round(in_bps, 2),
                    egress_bps=round(eg_bps, 2),
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
    elif hasattr(model, "src_endpoint_id"):
        ingress_calc = func.sum(
            case((model.dst_endpoint_id.is_not(None), model.bytes), else_=0)
        )
        egress_calc = func.sum(
            case((model.src_endpoint_id.is_not(None), model.bytes), else_=0)
        )
    else:
        ingress_calc = func.sum(model.bytes) / 2
        egress_calc = func.sum(model.bytes) / 2

    stmt = (
        select(
            model.bucket,
            func.coalesce(func.sum(model.bytes), 0).label("total_bytes"),
            func.coalesce(ingress_calc, 0).label("ingress_bytes"),
            func.coalesce(egress_calc, 0).label("egress_bytes"),
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
        if len(row) >= 4:
            in_bytes = int(row[2] or 0)
            eg_bytes = int(row[3] or 0)
            # If there is transit traffic not captured by monitored endpoint attribution:
            transit_bytes = max(0, byte_sum - (in_bytes + eg_bytes))
            if transit_bytes > 0:
                in_bytes += transit_bytes // 2
                eg_bytes += (transit_bytes - transit_bytes // 2)
            elif in_bytes == 0 and eg_bytes == 0 and byte_sum > 0:
                in_bytes = byte_sum // 2
                eg_bytes = byte_sum - in_bytes
        else:
            in_bytes = byte_sum // 2
            eg_bytes = byte_sum - in_bytes

        in_bps = (float(in_bytes) * 8.0) / bucket_seconds
        eg_bps = (float(eg_bytes) * 8.0) / bucket_seconds
        iso_str = bucket_time.astimezone(datetime.timezone.utc).isoformat()
        points.append(
            TrafficSeriesPoint(
                timestamp=iso_str,
                ingress_bps=round(in_bps, 2),
                egress_bps=round(eg_bps, 2),
            )
        )

    return APIResponse.success(
        data=TrafficSeriesResponse(window=window, points=points)
    )


@router.get("/top-talkers", response_model=APIResponse)
async def get_top_talkers(
    window: str = Query(default="1h"),
    limit: int = Query(default=10, ge=1, le=50),
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

    # 1. Top endpoints
    ep_filters = [FlowMinuteRollup.bucket >= start_time]
    if exporter_id is not None:
        ep_filters.append(FlowMinuteRollup.exporter_id == exporter_id)
    if endpoint_id is not None:
        ep_filters.append(
            or_(
                FlowMinuteRollup.src_endpoint_id == endpoint_id,
                FlowMinuteRollup.dst_endpoint_id == endpoint_id,
            )
        )

    top_ep_stmt = (
        select(
            FlowMinuteRollup.src_ip.label("ip"),
            FlowMinuteRollup.src_endpoint_id.label("ep_id"),
            func.sum(FlowMinuteRollup.bytes).label("total_bytes"),
            func.sum(FlowMinuteRollup.flow_count).label("total_flows"),
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
        )
        .where(*ep_filters)
        .group_by(FlowMinuteRollup.src_ip, FlowMinuteRollup.src_endpoint_id)
        .order_by(func.sum(FlowMinuteRollup.bytes).desc())
        .limit(limit)
    )

    top_ep_res = await db.execute(top_ep_stmt)
    ep_rows = top_ep_res.all()

    # Load hostnames for known endpoints
    ep_ids = [getattr(r, "ep_id", r[1]) for r in ep_rows if getattr(r, "ep_id", r[1])]
    hostnames: Dict[UUID, str] = {}
    if ep_ids:
        h_stmt = select(Endpoint.id, Endpoint.hostname).where(Endpoint.id.in_(ep_ids))
        h_res = await db.execute(h_stmt)
        for e_id, h_name in h_res.all():
            hostnames[e_id] = h_name

    top_endpoints: List[TopTalkerItem] = []
    for row in ep_rows:
        ip = row[0]
        ep_id = row[1]
        b_sum = row[2]
        f_sum = row[3]
        total_b = int(b_sum or 0)
        if len(row) >= 6:
            in_b = int(row[4] or 0)
            eg_b = int(row[5] or 0)
            if in_b == 0 and eg_b == 0:
                eg_b = total_b
        else:
            in_b = 0
            eg_b = total_b
        h_name = hostnames.get(ep_id)
        top_endpoints.append(
            TopTalkerItem(
                endpoint_id=ep_id,
                ip_address=str(ip),
                hostname=h_name,
                total_bytes=total_b,
                ingress_bytes=in_b,
                egress_bytes=eg_b,
                flow_count=int(f_sum or 0),
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
    for s_ip, d_ip, proto_id, port, b_sum, f_sum in conv_res.all():
        proto_name = PROTOCOL_MAP.get(proto_id, f"PROTO-{proto_id}")
        top_conversations.append(
            TopConversationItem(
                src_ip=str(s_ip),
                dst_ip=str(d_ip),
                protocol=proto_name,
                dst_port=int(port),
                total_bytes=int(b_sum or 0),
                flow_count=int(f_sum or 0),
            )
        )

    return APIResponse.success(
        data=TopTalkersResponse(
            top_endpoints=top_endpoints, top_conversations=top_conversations
        )
    )


@router.get("/applications", response_model=APIResponse)
async def get_applications_breakdown(
    window: str = Query(default="1h"),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns protocol and service port volume distribution for donut chart.
    Dynamically routes to hypertable or continuous aggregates based on window.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta

    model, _ = _select_rollup_model_and_bucket_seconds(window)

    stmt = (
        select(
            model.protocol,
            model.dst_port,
            func.sum(model.bytes).label("total_bytes"),
        )
        .where(model.bucket >= start_time)
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

    return APIResponse.success(data=ApplicationDistributionResponse(applications=apps))


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
    clean_ip = payload.ip_address.strip()
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
    clean_ip = ip.strip()
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
