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
    ConversationsResponse,
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
    WanLinkItem,
    derive_traffic_profile,
    format_dscp_label,
    format_tcp_flags,
)
from app.schemas.endpoints import InterfaceConfig
from app.services import conversations as conversation_queries
from app.services import wan_edge
from app.services.driver_manager import driver_manager
from app.services.flow_metrics import (
    PROTOCOL_MAP,
    TRAFFIC_ZONES,
    bits_per_second,
    classify_zone,
    configured_speed_mbps,
    dscp_breakdown,
    dscp_from_tos,
    interface_alias,
    interface_role,
    is_internal_address,
    percentile_with_idle,
    tcp_anomaly,
    utilization_pct,
)

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


WINDOWS = {
    "1h": datetime.timedelta(hours=1),
    "6h": datetime.timedelta(hours=6),
    "24h": datetime.timedelta(hours=24),
    "7d": datetime.timedelta(days=7),
    "30d": datetime.timedelta(days=30),
    "1y": datetime.timedelta(days=365),
}


def _get_window_delta(window: str) -> datetime.timedelta:
    """The window's length; 400 for any other value (it used to fall back to 1h silently while
    the response still echoed the unknown window)."""
    delta = WINDOWS.get(window.lower().strip())
    if delta is None:
        raise HTTPException(status_code=400, detail=f"window must be one of {', '.join(WINDOWS)}.")
    return delta


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


def _sum_where(condition: Any, column: Any) -> Any:
    """SUM(column) over the rows matching `condition`, 0 when none do."""
    return func.coalesce(func.sum(case((condition, column), else_=0)), 0)


def _counted_once(model: Any, filters: List[Any], with_tos: bool = False) -> Any:
    """`model`'s rows matching `filters` with each conversation counted once per bucket (the
    largest report among the exporters that saw it), as a subquery; see
    conversation_queries.per_minute_cte. The hourly tier keeps no addresses, so there a
    conversation is the endpoint pair, protocol and port: only used for one endpoint's traffic."""
    keys = [model.bucket]
    if hasattr(model, "src_ip"):
        keys += [model.src_ip, model.dst_ip]
    keys += [model.src_endpoint_id, model.dst_endpoint_id, model.protocol, model.dst_port]
    names = ("bytes", "packets", "flow_count") + (("tos",) if with_tos else ())
    measures = [func.max(getattr(model, name)).label(name) for name in names]
    return select(*keys, *measures).where(*filters).group_by(*keys).subquery("counted_once")


def _select_from(stmt: Any, subquery: Any) -> Any:
    return stmt if subquery is None else stmt.select_from(subquery)


# The overview averages the last five complete minutes; the current minute is still filling.
OVERVIEW_WINDOW_SECONDS = 300


@router.get("/overview", response_model=APIResponse)
async def get_bandwidth_overview(
    exporter_id: Optional[UUID] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Throughput, packet rates and TCP anomalies over the last five complete minutes.

    Fleet-wide by default, or for one exporter with `exporter_id`.
    - Ingress: flows towards a monitored endpoint; egress: flows from one. A flow between two
      monitored endpoints counts in both.
    - Transit: flows with no monitored endpoint on either side, reported separately.
    - avg_packet_size: total bytes / total packets.
    - Anomalies: SYN-only (half-open) and RST-without-FIN flows.
    - WAN: in/out over interfaces an admin marked as WAN, which counts each packet once at the
      network edge (the totals above count it once per exporter it crossed).
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - datetime.timedelta(seconds=OVERVIEW_WINDOW_SECONDS)
    window_filters = [FlowMinuteRollup.bucket >= window_start, FlowMinuteRollup.bucket < window_end]
    if exporter_id is not None:
        window_filters.append(FlowMinuteRollup.exporter_id == exporter_id)

    to_endpoint = FlowMinuteRollup.dst_endpoint_id.is_not(None)
    from_endpoint = FlowMinuteRollup.src_endpoint_id.is_not(None)
    unattributed = FlowMinuteRollup.src_endpoint_id.is_(None) & FlowMinuteRollup.dst_endpoint_id.is_(None)

    stmt = select(
        func.coalesce(func.sum(FlowMinuteRollup.bytes), 0).label("total_bytes"),
        _sum_where(to_endpoint, FlowMinuteRollup.bytes).label("ingress_bytes"),
        _sum_where(from_endpoint, FlowMinuteRollup.bytes).label("egress_bytes"),
        _sum_where(unattributed, FlowMinuteRollup.bytes).label("transit_bytes"),
        func.coalesce(func.sum(FlowMinuteRollup.packets), 0).label("total_packets"),
        _sum_where(to_endpoint, FlowMinuteRollup.packets).label("ingress_packets"),
        _sum_where(from_endpoint, FlowMinuteRollup.packets).label("egress_packets"),
        _sum_where(unattributed, FlowMinuteRollup.packets).label("transit_packets"),
        func.coalesce(func.sum(FlowMinuteRollup.flow_count), 0).label("total_flows"),
        func.count(
            func.distinct(
                case(
                    (FlowMinuteRollup.exporter_id != SENTINEL_UUID, FlowMinuteRollup.exporter_id),
                    else_=None,
                )
            )
        ).label("active_exporters"),
    ).where(*window_filters)

    res = await db.execute(stmt)
    row = res.one_or_none()
    (
        total_bytes,
        ingress_bytes,
        egress_bytes,
        transit_bytes,
        total_packets,
        ingress_packets,
        egress_packets,
        transit_packets,
        total_flows,
        active_exporters,
    ) = (int(v or 0) for v in (row if row else (0,) * 10))

    seconds = float(OVERVIEW_WINDOW_SECONDS)
    avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0

    # Anomalies use the same per-conversation rule as the conversation table and the PDF report:
    # a conversation is anomalous when the OR of its TCP flags over the window is SYN-only or
    # RST-without-FIN.
    anomaly_flows_count = 0
    anomaly_conversations_count = 0
    try:
        where_sql, where_params = conversation_queries.build_filters(window_start, exporter_id=exporter_id)
        where_sql += " AND bucket < :window_end"
        summary = await conversation_queries.anomaly_summary(db, where_sql, {**where_params, "window_end": window_end})
        anomaly_flows_count = summary["flows"]
        anomaly_conversations_count = summary["conversations"]
    except Exception as exc:
        logger.debug("Failed to calculate anomaly flow count: %s", exc)

    wan = await _wan_overview(db, exporter_id, window_start, window_end, seconds)

    # Fetch unmatched count from FastStore
    unmatched_count = 0
    store_client = await get_flow_store()
    if store_client:
        try:
            await _forget_registered_exporters(store_client, db)
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
            total_ingress_bps=round(bits_per_second(ingress_bytes, seconds), 2),
            total_egress_bps=round(bits_per_second(egress_bytes, seconds), 2),
            total_transit_bps=round(bits_per_second(transit_bytes, seconds), 2),
            active_exporters_count=active_exporters,
            unmatched_exporters_count=unmatched_count,
            total_flows_count=total_flows,
            total_ingress_pps=round(ingress_packets / seconds, 2),
            total_egress_pps=round(egress_packets / seconds, 2),
            total_transit_pps=round(transit_packets / seconds, 2),
            avg_packet_size=avg_packet_size,
            anomaly_flows_count=anomaly_flows_count,
            anomaly_conversations_count=anomaly_conversations_count,
            window_seconds=OVERVIEW_WINDOW_SECONDS,
            exporter_id=exporter_id,
            **wan,
        )
    )


async def _wan_overview(
    db: AsyncSession,
    exporter_id: Optional[UUID],
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    seconds: float,
) -> Dict[str, Any]:
    """WAN-edge fields of the overview: totals and one row per WAN link, busiest first."""
    interfaces = await wan_edge.list_wan_interfaces(db, exporter_id)
    totals = await wan_edge.wan_link_totals(db, interfaces, window_start, window_end)
    zero = {"in_bytes": 0, "out_bytes": 0, "in_packets": 0, "out_packets": 0}
    stats = [(w, totals.get((w.exporter_id, w.interface_idx), zero)) for w in interfaces]
    all_bytes = sum(t["in_bytes"] + t["out_bytes"] for _, t in stats)

    links: List[WanLinkItem] = []
    for w, t in stats:
        in_bps = bits_per_second(t["in_bytes"], seconds)
        out_bps = bits_per_second(t["out_bytes"], seconds)
        links.append(
            WanLinkItem(
                exporter_id=w.exporter_id,
                exporter_hostname=w.exporter_hostname,
                interface_idx=w.interface_idx,
                name=w.name,
                in_bps=round(in_bps, 2),
                out_bps=round(out_bps, 2),
                in_pps=round(t["in_packets"] / seconds, 2),
                out_pps=round(t["out_packets"] / seconds, 2),
                speed_mbps=w.speed_mbps,
                utilization_percentage=(
                    round(utilization_pct(max(in_bps, out_bps), w.speed_mbps), 2) if w.speed_mbps else None
                ),
                share_percentage=(
                    round((t["in_bytes"] + t["out_bytes"]) * 100.0 / all_bytes, 2) if all_bytes else 0.0
                ),
            )
        )
    links.sort(key=lambda link: link.in_bps + link.out_bps, reverse=True)

    return {
        "wan_interfaces_count": len(interfaces),
        "wan_in_bps": round(bits_per_second(sum(t["in_bytes"] for _, t in stats), seconds), 2),
        "wan_out_bps": round(bits_per_second(sum(t["out_bytes"] for _, t in stats), seconds), 2),
        "wan_in_pps": round(sum(t["in_packets"] for _, t in stats) / seconds, 2),
        "wan_out_pps": round(sum(t["out_packets"] for _, t in stats) / seconds, 2),
        "wan_links": links,
    }


@router.get("/traffic-series", response_model=APIResponse)
async def get_traffic_series(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    endpoint_id: Optional[UUID] = Query(default=None),
    interface_idx: Optional[int] = Query(default=None),
    wan_only: bool = Query(default=False),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns time-series data points for Chart.js stacked area chart.
    Dynamically routes to 1-minute hypertable, 1-hour CAGG, or 1-day CAGG based on window.
    For flow exporter nodes and per-interface isolation, queries FlowInterfaceMinuteRollup.
    With `wan_only` (fleet or exporter scope, no single interface), returns in/out summed over
    WAN-role interfaces as ingress/egress; when none are marked it falls back to the normal
    series and reports `wan_only: false`.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta

    if wan_only and not endpoint_id and interface_idx is None:
        wan_interfaces = await wan_edge.list_wan_interfaces(db, exporter_id)
        if wan_interfaces:
            predicate, params = wan_edge.interface_filter_sql(wan_interfaces)
            return await _interface_series(db, window, delta, start_time, predicate, params, wan_only=True)

    # Exporter scope (optionally one interface): interface rollups.
    if exporter_id and not endpoint_id:
        params: Dict[str, Any] = {"exporter_id": str(exporter_id)}
        predicate = "exporter_id = CAST(:exporter_id AS uuid)"
        if interface_idx is not None:
            predicate += " AND interface_idx = :interface_idx"
            params["interface_idx"] = interface_idx
        return await _interface_series(db, window, delta, start_time, predicate, params)

    # Standard endpoint or fleet-wide flow rollup query
    model, bucket_seconds = _select_rollup_model_and_bucket_seconds(window, endpoint_id)
    directional = hasattr(model, "src_endpoint_id")

    filters = [model.bucket >= start_time]
    if exporter_id:
        filters.append(model.exporter_id == exporter_id)
    if endpoint_id and directional:
        filters.append(
            (model.src_endpoint_id == endpoint_id)
            | (model.dst_endpoint_id == endpoint_id)
        )

    source, counted_once = model, None
    if endpoint_id and directional:
        # One endpoint's traffic: each conversation once per bucket, however many routers
        # export it. Every row involves the endpoint, so nothing is transit.
        counted_once = _counted_once(model, filters)
        source, filters = counted_once.c, []
        to_side = source.dst_endpoint_id == endpoint_id
        from_side = source.src_endpoint_id == endpoint_id
        transit_side = None
    elif directional:
        to_side = model.dst_endpoint_id.is_not(None)
        from_side = model.src_endpoint_id.is_not(None)
        transit_side = model.src_endpoint_id.is_(None) & model.dst_endpoint_id.is_(None)
    else:
        # The daily tier keeps no endpoint attribution: everything is reported as transit.
        to_side = from_side = None
        transit_side = True

    # Only the sides this tier can resolve are queried; the rest stay 0.
    measures: List[Tuple[str, Any]] = [("flow_count", func.coalesce(func.sum(source.flow_count), 0))]
    for side, condition in (("ingress", to_side), ("egress", from_side), ("transit", transit_side)):
        if condition is None:
            continue
        for unit, column in (("bytes", source.bytes), ("packets", source.packets)):
            expr = func.coalesce(func.sum(column), 0) if condition is True else _sum_where(condition, column)
            measures.append((f"{side}_{unit}", expr))

    stmt = (
        select(source.bucket, *(expr.label(name) for name, expr in measures))
        .where(*filters)
        .group_by(source.bucket)
        .order_by(source.bucket.asc())
    )

    res = await db.execute(_select_from(stmt, counted_once))
    rows = res.all()

    points: List[TrafficSeriesPoint] = []
    for row in rows:
        bucket_time = row[0]
        values = {name: int(row[i + 1] or 0) for i, (name, _) in enumerate(measures)}
        in_bytes, eg_bytes, tr_bytes = (values.get(f"{side}_bytes", 0) for side in ("ingress", "egress", "transit"))
        in_pkts, eg_pkts, tr_pkts = (values.get(f"{side}_packets", 0) for side in ("ingress", "egress", "transit"))
        flows_cnt = values["flow_count"]

        iso_str = bucket_time.astimezone(datetime.timezone.utc).isoformat()
        points.append(
            TrafficSeriesPoint(
                timestamp=iso_str,
                ingress_bps=round(bits_per_second(in_bytes, bucket_seconds), 2),
                egress_bps=round(bits_per_second(eg_bytes, bucket_seconds), 2),
                transit_bps=round(bits_per_second(tr_bytes, bucket_seconds), 2),
                ingress_pps=round(float(in_pkts) / bucket_seconds, 2),
                egress_pps=round(float(eg_pkts) / bucket_seconds, 2),
                transit_pps=round(float(tr_pkts) / bucket_seconds, 2),
                flows_per_sec=round(float(flows_cnt) / bucket_seconds, 2),
            )
        )

    return APIResponse.success(
        data=TrafficSeriesResponse(
            window=window,
            points=points,
            bucket_seconds=int(bucket_seconds),
            directional=directional,
        )
    )


async def _interface_series(
    db: AsyncSession,
    window: str,
    delta: datetime.timedelta,
    start_time: datetime.datetime,
    predicate: str,
    params: Dict[str, Any],
    wan_only: bool = False,
) -> Any:
    """Series from the interface rollups for rows matching `predicate` (in -> ingress, out -> egress).

    Windows up to 6h use the minute table; longer ones use flow_interface_hourly_rollups, which
    covers 30 days (the minute table keeps 7) and keeps a 7-day chart at 168 points, not 10,080.
    """
    if delta > datetime.timedelta(hours=6):
        source, bucket_seconds = "flow_interface_hourly_rollups", 3600
    else:
        source, bucket_seconds = "flow_interface_minute_rollups", 60
    res = await db.execute(
        text(f"""
            SELECT bucket,
                   COALESCE(SUM(in_bytes), 0) AS in_bytes, COALESCE(SUM(out_bytes), 0) AS out_bytes,
                   COALESCE(SUM(in_packets), 0) AS in_packets, COALESCE(SUM(out_packets), 0) AS out_packets,
                   COALESCE(SUM(flow_count), 0) AS flow_count
            FROM {source}
            WHERE bucket >= :start AND {predicate}
            GROUP BY bucket
            ORDER BY bucket
        """),
        {**params, "start": start_time},
    )

    points: List[TrafficSeriesPoint] = []
    for row in res.all():
        in_bytes, eg_bytes, in_pkts, eg_pkts, flows_cnt = (int(row[i] or 0) for i in range(1, 6))
        points.append(
            TrafficSeriesPoint(
                timestamp=row[0].astimezone(datetime.timezone.utc).isoformat(),
                ingress_bps=round(bits_per_second(in_bytes, bucket_seconds), 2),
                egress_bps=round(bits_per_second(eg_bytes, bucket_seconds), 2),
                ingress_pps=round(in_pkts / bucket_seconds, 2),
                egress_pps=round(eg_pkts / bucket_seconds, 2),
                flows_per_sec=round(flows_cnt / bucket_seconds, 2),
            )
        )

    return APIResponse.success(
        data=TrafficSeriesResponse(window=window, points=points, bucket_seconds=bucket_seconds, wan_only=wan_only)
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
    """Top addresses by total volume, plus the largest conversations (first page of
    GET /bandwidth/conversations, kept for existing clients).

    Each address's ingress (as destination) and egress (as source) come from one aggregation,
    so neither side is cut off by a separate per-direction limit.
    """
    delta = _get_window_delta(window)
    start_time = datetime.datetime.now(datetime.timezone.utc) - delta
    where_sql, params = conversation_queries.build_filters(start_time, exporter_id, endpoint_id)

    talkers_res = await db.execute(
        text(f"""
            WITH {conversation_queries.per_minute_cte(where_sql)},
            sides AS (
                SELECT src_ip AS ip, src_endpoint_id AS endpoint_id,
                       0::BIGINT AS ingress_bytes, bytes AS egress_bytes,
                       flow_count, packets
                FROM per_minute
                UNION ALL
                SELECT dst_ip, dst_endpoint_id,
                       bytes, 0::BIGINT,
                       flow_count, packets
                FROM per_minute
            )
            SELECT ip,
                   (array_agg(endpoint_id) FILTER (WHERE endpoint_id IS NOT NULL))[1] AS endpoint_id,
                   SUM(ingress_bytes) AS ingress_bytes,
                   SUM(egress_bytes) AS egress_bytes,
                   SUM(ingress_bytes + egress_bytes) AS total_bytes,
                   SUM(flow_count) AS flow_count,
                   SUM(packets) AS total_packets
            FROM sides
            GROUP BY ip
            ORDER BY total_bytes DESC, ip
            LIMIT :limit
        """),
        {**params, "limit": limit},
    )
    talker_rows = [dict(r) for r in talkers_res.mappings()]

    ep_ids = [r["endpoint_id"] for r in talker_rows if r["endpoint_id"]]
    hostnames: Dict[Any, str] = {}
    if ep_ids:
        h_res = await db.execute(select(Endpoint.id, Endpoint.hostname).where(Endpoint.id.in_(ep_ids)))
        hostnames = {e_id: h_name for e_id, h_name in h_res.all()}

    top_endpoints = []
    for r in talker_rows:
        total_bytes = int(r["total_bytes"] or 0)
        total_packets = int(r["total_packets"] or 0)
        top_endpoints.append(
            TopTalkerItem(
                endpoint_id=r["endpoint_id"],
                ip_address=str(r["ip"]),
                hostname=hostnames.get(r["endpoint_id"]),
                total_bytes=total_bytes,
                ingress_bytes=int(r["ingress_bytes"] or 0),
                egress_bytes=int(r["egress_bytes"] or 0),
                flow_count=int(r["flow_count"] or 0),
                total_packets=total_packets,
                avg_packet_size=int(total_bytes // total_packets) if total_packets > 0 else 0,
            )
        )

    page = await conversation_queries.query_conversations(
        db, start_time, exporter_id=exporter_id, endpoint_id=endpoint_id, limit=limit
    )
    return APIResponse.success(
        data=TopTalkersResponse(top_endpoints=top_endpoints, top_conversations=page["items"])
    )


@router.get("/conversations", response_model=APIResponse)
async def get_conversations(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    endpoint_id: Optional[UUID] = Query(default=None),
    ip: Optional[str] = Query(default=None),
    port: Optional[int] = Query(default=None, ge=0, le=65535),
    dscp: Optional[int] = Query(default=None, ge=0, le=63),
    quick: str = Query(default="all"),
    sort: str = Query(default="bytes"),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Conversations (src, dst, protocol, service port) filtered and paged on the server.

    Filters: `ip` (either side), `port` (service port), `dscp`, plus `quick` = all | anomalies |
    qos | bulk. `counts` gives the size of each quick filter over every conversation matching
    the other filters, not just the returned page. `sort` = bytes | flows | packets.
    """
    if quick not in conversation_queries.QUICK_FILTERS:
        raise HTTPException(status_code=400, detail=f"quick must be one of {', '.join(conversation_queries.QUICK_FILTERS)}.")
    if sort not in conversation_queries.SORT_COLUMNS:
        raise HTTPException(status_code=400, detail=f"sort must be one of {', '.join(conversation_queries.SORT_COLUMNS)}.")
    clean_ip = None
    if ip:
        clean_ip = ip.strip().split("/")[0]
        try:
            ipaddress.ip_address(clean_ip)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"'{clean_ip}' is not a valid IP address.")

    start_time = datetime.datetime.now(datetime.timezone.utc) - _get_window_delta(window)
    page = await conversation_queries.query_conversations(
        db,
        start_time,
        exporter_id=exporter_id,
        endpoint_id=endpoint_id,
        ip=clean_ip,
        port=port,
        dscp=dscp,
        quick=quick,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return APIResponse.success(
        data=ConversationsResponse(
            window=window,
            items=page["items"],
            counts=page["counts"],
            total=page["total"],
            limit=limit,
            offset=offset,
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
    # One endpoint's traffic counts each conversation once per bucket (see _counted_once).
    counted_once = endpoint_id is not None and hasattr(model, "src_endpoint_id")
    source, app_from = model, None
    if counted_once:
        app_from = _counted_once(model, app_filters)
        source, app_filters = app_from.c, []

    # 1. Top 15 (protocol, dst_port) Application Services
    stmt = (
        select(
            source.protocol,
            source.dst_port,
            func.sum(source.bytes).label("total_bytes"),
        )
        .where(*app_filters)
        .group_by(source.protocol, source.dst_port)
        .order_by(func.sum(source.bytes).desc())
        .limit(15)
    )

    res = await db.execute(_select_from(stmt, app_from))
    rows = res.all()

    # Shares are of all traffic in the window, not of the top 15; the rest becomes "Other".
    total_res = await db.execute(_select_from(select(func.coalesce(func.sum(source.bytes), 0)).where(*app_filters), app_from))
    overall_bytes = int(total_res.scalar() or 0)

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
    other_bytes = overall_bytes - sum(a.total_bytes for a in apps)
    if other_bytes > 0:
        apps.append(
            ApplicationDistributionItem(
                protocol_name="Other",
                port=0,
                service_label="Other",
                total_bytes=other_bytes,
                percentage=round(other_bytes / overall_bytes * 100.0, 2),
                is_other=True,
            )
        )

    # 2. QoS / DSCP Breakdown
    # On TimescaleDB the hourly/daily tiers are continuous aggregates that migration 0013 could
    # not extend with `tos`; fall back to the 1-minute tier (90-day retention) for ToS data.
    # The response reports the tier and start actually used, since a 1y window then only gets
    # 90 days of QoS data next to 365 days of application data.
    qos_model = model
    qos_start = start_time
    if model is not FlowMinuteRollup and not await _db_table_has_column(db, model.__tablename__, "tos"):
        qos_model = FlowMinuteRollup
        qos_start = max(start_time, now - _MINUTE_ROLLUP_RETENTION)
    qos_filters = _rollup_window_filters(qos_model, qos_start, exporter_id, endpoint_id)
    qos_source, qos_from = qos_model, None
    if counted_once:
        qos_from = _counted_once(qos_model, qos_filters, with_tos=True)
        qos_source, qos_filters = qos_from.c, []
    qos_stmt = (
        select(
            qos_source.tos,
            func.coalesce(func.sum(qos_source.bytes), 0).label("total_bytes"),
            func.coalesce(func.sum(qos_source.packets), 0).label("total_packets"),
            func.coalesce(func.sum(qos_source.flow_count), 0).label("flow_count"),
        )
        .where(*qos_filters)
        .group_by(qos_source.tos)
    )
    qos_res = await db.execute(_select_from(qos_stmt, qos_from))
    qos_classes: List[QoSDistributionItem] = [
        QoSDistributionItem(
            dscp=item["dscp"],
            dscp_label=item["dscp_label"],
            total_bytes=item["total_bytes"],
            total_packets=item["total_packets"],
            flow_count=item["flow_count"],
            percentage=round(item["percentage"], 2),
        )
        for item in dscp_breakdown(qos_res.all())
    ]

    # 3. Traffic Zone Breakdown (East-West, North-South, External)
    zone_sums = {zone: {"bytes": 0, "flows": 0} for zone in TRAFFIC_ZONES}

    zones_resolution = "ip" if hasattr(model, "src_ip") else ("endpoint" if hasattr(model, "src_endpoint_id") else "unavailable")
    if hasattr(model, "src_ip"):
        zone_stmt = (
            select(
                source.src_ip,
                source.dst_ip,
                source.src_endpoint_id,
                source.dst_endpoint_id,
                func.sum(source.bytes).label("total_bytes"),
                func.sum(source.flow_count).label("flow_count"),
            )
            .where(*app_filters)
            .group_by(
                source.src_ip,
                source.dst_ip,
                source.src_endpoint_id,
                source.dst_endpoint_id,
            )
        )
        zone_res = await db.execute(_select_from(zone_stmt, app_from))
        for z_row in zone_res.all():
            if len(z_row) < 5:
                continue
            s_ip = str(z_row[0])
            d_ip = str(z_row[1])
            s_ep = z_row[2]
            d_ep = z_row[3]
            z_b = int(z_row[4] or 0)
            z_f = int(z_row[5] or 0) if len(z_row) > 5 else 0

            z_key = classify_zone(is_internal_address(s_ip, s_ep), is_internal_address(d_ip, d_ep))
            zone_sums[z_key]["bytes"] += z_b
            zone_sums[z_key]["flows"] += z_f
    elif hasattr(model, "src_endpoint_id"):
        # Medium-term aggregation tier (FlowHourlyRollup): individual IP addresses are pruned,
        # but correlated src_endpoint_id and dst_endpoint_id are preserved.
        zone_stmt = (
            select(
                source.src_endpoint_id,
                source.dst_endpoint_id,
                func.sum(source.bytes).label("total_bytes"),
                func.sum(source.flow_count).label("flow_count"),
            )
            .where(*app_filters)
            .group_by(
                source.src_endpoint_id,
                source.dst_endpoint_id,
            )
        )
        zone_res = await db.execute(_select_from(zone_stmt, app_from))
        for z_row in zone_res.all():
            if len(z_row) < 3:
                continue
            s_ep = z_row[0]
            d_ep = z_row[1]
            z_b = int(z_row[2] or 0)
            z_f = int(z_row[3] or 0) if len(z_row) > 3 else 0

            # Without IPs only known endpoints count as internal.
            z_key = classify_zone(s_ep is not None, d_ep is not None)
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
    for z_name in TRAFFIC_ZONES:
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
            total_bytes=overall_bytes,
            zones_resolution=zones_resolution,
        )
    )


async def _forget_registered_exporters(store_client: Any, db: AsyncSession) -> None:
    """Drops addresses that now belong to an endpoint from the unmatched exporter lists.

    The flow daemon lists an exporter it cannot match for 24 h; once the address is enrolled
    or linked as an alias its flows are matched, so the warning must not linger for a day.
    Same rule as the correlator: any endpoint's primary IP or one of its flow_exporter_ips.
    """
    res = await db.execute(
        text("""
            SELECT host(ip_address) AS ip, flow_exporter_ips
            FROM endpoints
            WHERE deleted_at IS NULL AND endpoint_status != 'DELETED'
        """)
    )
    registered = set()
    for ip, aliases in res.all():
        registered.add(str(ip))
        registered.update(str(a).split("/")[0].strip() for a in (aliases or []))
    for key in ("flow:unmatched:zset", "flow:unmatched:set"):
        members = (
            await store_client.zrangebyscore(key, "-inf", "+inf")
            if key.endswith("zset")
            else await store_client.smembers(key)
        )
        stale = [m for m in members or [] if (m.decode("utf-8") if isinstance(m, bytes) else str(m)) in registered]
        if stale:
            await (store_client.zrem(key, *stale) if key.endswith("zset") else store_client.srem(key, *stale))


async def _unmatched_flow_rate(store_client: Any, ip: str) -> Optional[float]:
    """Recent flows/s the flow daemon recorded for an unregistered exporter, if any."""
    try:
        raw = await store_client.get(f"flow:unmatched:{ip}:fps")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None


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

    # Exporter health: newest flow minute in the last 24 h and the rate over the last five
    # complete minutes.
    window_end = now_utc.replace(second=0, microsecond=0)
    health: Dict[str, Tuple[Any, int]] = {}
    try:
        health_res = await db.execute(
            text("""
                SELECT exporter_id,
                       MAX(bucket) AS last_bucket,
                       COALESCE(SUM(flow_count) FILTER (WHERE bucket >= :recent AND bucket < :window_end), 0) AS recent_flows
                FROM flow_minute_rollups
                WHERE bucket >= :since
                GROUP BY exporter_id
            """),
            {
                "since": since_24h,
                "recent": window_end - datetime.timedelta(seconds=OVERVIEW_WINDOW_SECONDS),
                "window_end": window_end,
            },
        )
        for h_row in health_res.all():
            health[str(h_row[0])] = (h_row[1], int(h_row[2] or 0))
    except Exception as exc:
        logger.debug("Exporter health query failed: %s", exc)

    exporters: List[FlowExporterItem] = []
    for ep in endpoints:
        exp_ips = [str(ip) for ip in (ep.flow_exporter_ips or [])]
        aliases = ep.flow_interface_aliases or {}
        last_bucket, recent_flows = health.get(str(ep.id), (None, 0))
        exporters.append(
            FlowExporterItem(
                id=ep.id,
                hostname=ep.hostname,
                primary_ip=str(ep.ip_address).split("/")[0],
                flow_exporter_ips=exp_ips,
                interface_aliases=aliases,
                device_role=getattr(ep, "device_role", "ACTIVE_HOST"),
                last_flow_time=last_bucket.astimezone(datetime.timezone.utc).isoformat() if last_bucket else None,
                flows_per_sec=round(recent_flows / OVERVIEW_WINDOW_SECONDS, 2),
            )
        )

    unmatched_list: List[UnmatchedExporterItem] = []
    store_client = await get_flow_store()
    if store_client:
        try:
            await _forget_registered_exporters(store_client, db)
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
                                flows_per_sec=await _unmatched_flow_rate(store_client, ip_str),
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

    # Get endpoint for alias & speed mappings
    ep_stmt = select(Endpoint).where(
        Endpoint.id == exporter_id,
        Endpoint.deleted_at.is_(None),
    )
    ep_res = await db.execute(ep_stmt)
    endpoint = ep_res.scalar_one_or_none()
    aliases = getattr(endpoint, "flow_interface_aliases", {}) or {}

    # Up to 7 days the minute table answers directly; 30 days needs the hourly rollup (the minute
    # table is kept 7 days). p95 is over minutes, or over hourly averages for the hourly tier.
    hourly = delta > datetime.timedelta(days=7)
    if hourly:
        source, slot_seconds = "flow_interface_hourly_rollups", 3600
        peak_in_col, peak_out_col = "peak_in_minute_bytes", "peak_out_minute_bytes"
    else:
        source, slot_seconds = "flow_interface_minute_rollups", 60
        peak_in_col, peak_out_col = "in_bytes", "out_bytes"
    params = {"exporter_id": str(exporter_id), "start": start_time}

    first_res = await db.execute(
        text(f"SELECT MIN(bucket) FROM {source} WHERE exporter_id = CAST(:exporter_id AS uuid) AND bucket >= :start"),
        params,
    )
    first_bucket = first_res.scalar()
    coverage_start = max(start_time, first_bucket) if first_bucket else start_time
    covered_seconds = max(60.0, (now - coverage_start).total_seconds())
    total_slots = int(-(-covered_seconds // slot_seconds))

    res = await db.execute(
        text(f"""
            SELECT interface_idx,
                   SUM(in_bytes) AS in_bytes, SUM(out_bytes) AS out_bytes,
                   SUM(in_packets) AS in_packets, SUM(out_packets) AS out_packets,
                   SUM(flow_count) AS flow_count,
                   MAX({peak_in_col}) AS peak_in, MAX({peak_out_col}) AS peak_out,
                   array_agg(in_bytes ORDER BY in_bytes) AS in_slots,
                   array_agg(out_bytes ORDER BY out_bytes) AS out_slots
            FROM {source}
            WHERE exporter_id = CAST(:exporter_id AS uuid) AND bucket >= :start
            GROUP BY interface_idx
            ORDER BY interface_idx
        """),
        params,
    )
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

    zero_stats = {
        "in_bytes": 0, "out_bytes": 0, "in_packets": 0, "out_packets": 0, "flow_count": 0,
        "peak_in": 0, "peak_out": 0, "in_slots": [], "out_slots": [],
    }
    all_if_indices: dict[int, dict[str, Any]] = {}
    for row in rows:
        all_if_indices[int(row[0])] = {
            "in_bytes": int(row[1] or 0),
            "out_bytes": int(row[2] or 0),
            "in_packets": int(row[3] or 0),
            "out_packets": int(row[4] or 0),
            "flow_count": int(row[5] or 0),
            "peak_in": int(row[6] or 0),
            "peak_out": int(row[7] or 0),
            "in_slots": list(row[8] or []),
            "out_slots": list(row[9] or []),
        }

    # Include any interfaces configured in aliases even if 0 traffic recorded
    for k_str in aliases:
        try:
            all_if_indices.setdefault(int(k_str), dict(zero_stats))
        except ValueError:
            pass

    interfaces: List[InterfaceTelemetryItem] = []
    for if_idx in sorted(all_if_indices.keys()):
        stats = all_if_indices[if_idx]
        in_b = stats["in_bytes"]
        out_b = stats["out_bytes"]
        in_bps = bits_per_second(in_b, covered_seconds)
        out_bps = bits_per_second(out_b, covered_seconds)
        in_p95 = bits_per_second(percentile_with_idle(stats["in_slots"], total_slots), slot_seconds)
        out_p95 = bits_per_second(percentile_with_idle(stats["out_slots"], total_slots), slot_seconds)
        in_peak = bits_per_second(stats["peak_in"], 60.0)
        out_peak = bits_per_second(stats["peak_out"], 60.0)

        alias_data = aliases.get(str(if_idx))
        name, speed_mbps = interface_alias(alias_data, f"Interface {if_idx}")
        speed_mbps = max(1, speed_mbps)
        speed_configured = configured_speed_mbps(alias_data) is not None

        def _util(bps_in: float, bps_out: float) -> Optional[float]:
            return round(utilization_pct(max(bps_in, bps_out), speed_mbps), 2) if speed_configured else None

        is_active = (if_idx in active_store_indices) or (stats["flow_count"] > 0) or ((in_b + out_b) > 0)

        interfaces.append(
            InterfaceTelemetryItem(
                interface_idx=if_idx,
                name=name,
                speed_mbps=speed_mbps,
                speed_configured=speed_configured,
                role=interface_role(alias_data),
                in_bytes=in_b,
                out_bytes=out_b,
                in_bps=round(in_bps, 2),
                out_bps=round(out_bps, 2),
                in_packets=stats["in_packets"],
                out_packets=stats["out_packets"],
                flow_count=stats["flow_count"],
                utilization_percentage=_util(in_bps, out_bps),
                is_active=is_active,
                in_p95_bps=round(in_p95, 2),
                out_p95_bps=round(out_p95, 2),
                in_peak_bps=round(in_peak, 2),
                out_peak_bps=round(out_peak, 2),
                p95_utilization_percentage=_util(in_p95, out_p95),
                peak_utilization_percentage=_util(in_peak, out_peak),
            )
        )

    return APIResponse.success(
        data=InterfaceTelemetryResponse(
            exporter_id=exporter_id,
            window=window,
            interfaces=interfaces,
            covered_seconds=int(covered_seconds),
            p95_resolution_seconds=slot_seconds,
            source=source,
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
