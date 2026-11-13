"""Traffic analytics routes (the Traffic page): overview, traffic series, top talkers,
conversations, applications and QoS, interfaces, and enrolling or mapping flow exporters.

Everything reads the rollups flowd writes. Which table a window reads, and how long each keeps
its data, is in _select_rollup_model_and_bucket_seconds and docs/database.md section 3; a
window longer than the data behind it is refused (_require_window_covered) rather than
answered with part of it. A conversation seen by several exporters is counted once, at the
largest report (_counted_once). The services this calls (conversations, flow_metrics,
wan_edge, flow_paths) hold the shared rules, so the dashboard and the PDF report agree.
"""
from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query, Request, status
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
    AnomalySummaryResponse,
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
    TopTalkerItem,
    TopTalkersResponse,
    TrafficSeriesPoint,
    TrafficSeriesResponse,
    TrafficZoneItem,
    UnmatchedExporterItem,
    DownSpan,
    UsualBand,
    WanLinkAlertItem,
    WanLinkSeriesItem,
    WanLinkItem,
    WanLinkNowItem,
    WanLinksResponse,
)
from app.schemas.endpoints import InterfaceConfig
from app.services import conversations as conversation_queries
from app.services import interface_table, interface_traffic, snmp_inventory, wan_edge, wan_links
from app.services.driver_manager import driver_manager
from app.services.endpoint_filter import EndpointFilter, filter_from_request, traffic_scope
from app.services.endpoint_filter import conditions as endpoint_filter_conditions
from app.services.flow_metrics import (
    PROTOCOL_MAP,
    TRAFFIC_ZONES,
    bits_per_second,
    configured_speed_mbps,
    dscp_breakdown,
    interface_alias,
    interface_role,
    percentile_with_idle,
    service_label,
    utilization_pct,
)

logger = logging.getLogger(__name__)

SENTINEL_UUID = UUID("00000000-0000-0000-0000-000000000000")

router = APIRouter(prefix="/bandwidth", tags=["bandwidth"])

_flow_redis_pool: Optional[Any] = None


# The driver manager's FastStore client when it has one; otherwise a standalone client is made
# here, unless FastStore is turned off in the config.
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
    """The table and bucket length behind the fleet and per-endpoint charts and applications.

    - 1h, 6h: FlowMinuteRollup (60s buckets, kept 90 days).
    - 24h, 7d, 30d: FlowHourlyRollup (3600s buckets, kept 30 days, with endpoints).
    - 1y, fleet only: FlowDailyRollup (86400s buckets, kept 365 days, no endpoints). One endpoint's
      1y is refused before it gets here (_require_window_covered).

    Addresses and ToS over more than 2 hours come from flow_conversation_hourly_rollups.
    docs/database.md section 3 has the whole map.
    """
    w = window.lower().strip()
    if w in ("1h", "6h"):
        return FlowMinuteRollup, 60.0
    if w in ("24h", "7d", "30d") or endpoint_id is not None:
        return FlowHourlyRollup, 3600.0
    if w == "1y":
        return FlowDailyRollup, 86400.0
    return FlowMinuteRollup, 60.0


def _sum_where(condition: Any, column: Any) -> Any:
    """SUM(column) over the rows matching `condition`, 0 when none do."""
    return func.coalesce(func.sum(case((condition, column), else_=0)), 0)


def _counted_once(model: Any, filters: List[Any], with_tos: bool = False) -> Any:
    """`model`'s rows matching `filters` with each conversation counted once per bucket (the
    largest report among the exporters that saw it), as a subquery; see
    conversation_queries.counted_once_cte. The hourly tier keeps no addresses, so there a
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


# How long the tables behind each kind of view keep their data (migrations 0008 and 0021).
_ENDPOINT_SERIES_RETENTION = datetime.timedelta(days=30)
_CONVERSATION_ROLLUP_RETENTION = datetime.timedelta(days=90)


def _require_window_covered(delta: datetime.timedelta, kept: datetime.timedelta, what: str) -> None:
    """400 for a window longer than the data behind it: a 1y answer holding 30 days of traffic
    looked like a year in which traffic had dropped."""
    if delta > kept:
        longest = max((length, name) for name, length in WINDOWS.items() if length <= kept)[1]
        raise HTTPException(
            status_code=400,
            detail=f"{what} are kept {kept.days} days, so the longest window here is {longest}.",
        )


async def _traffic_filter(request: Request) -> EndpointFilter:
    """The shared filter (site, location, tag, type, q) from the query string."""
    return filter_from_request(request)


@dataclass(frozen=True)
class _Scope:
    """What a Traffic page request covers (build plan 7.5 step 2).

    `routers`: None for every router plus flows from unregistered sources, else only what these
    routers reported (empty: nothing matched). `device`: the one device picked, when it is in
    the filter; the views that belong to one router (its interfaces, its interface chart) use it.
    """
    routers: Optional[List[UUID]]
    device: Optional[UUID]


def _as_uuid(value: Any) -> Optional[UUID]:
    """A query value, or None for anything else (route functions called directly in tests get
    the Query marker for a parameter they leave out)."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        return UUID(value)
    return None


async def _scope(
    db: AsyncSession, endpoint_filter: Any, device_id: Any = None, exporter_id: Any = None
) -> _Scope:
    """The filter plus an optional device, resolved by endpoint_filter.traffic_scope. `exporter_id`
    is the old name of `device_id` and is still accepted, so saved links keep working."""
    picked = _as_uuid(device_id) or _as_uuid(exporter_id)
    routers = await traffic_scope(db, endpoint_filter if isinstance(endpoint_filter, EndpointFilter) else None, picked)
    return _Scope(routers=routers, device=picked if routers and routers == [picked] else None)


@router.get("/overview", response_model=APIResponse)
async def get_bandwidth_overview(
    exporter_id: Optional[UUID] = Query(default=None),
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Throughput, packet rates and TCP anomalies over the last five complete minutes.

    Every router by default; the shared filter and `device_id` narrow it to the routers in
    scope (see _scope).
    - Ingress (inbound): external to internal; egress (outbound): internal to external;
      internal: both ends internal; transit: neither. Internal means a monitored endpoint or a
      private address, the Traffic zones rule. Each conversation counts once, however many
      routers reported it.
    - avg_packet_size: total bytes / total packets.
    - Anomalies: SYN-only (half-open) and RST-without-FIN flows.
    - WAN: in/out over interfaces an admin marked as WAN.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    window_end = now.replace(second=0, microsecond=0)
    window_start = window_end - datetime.timedelta(seconds=OVERVIEW_WINDOW_SECONDS)
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    where_sql, where_params = conversation_queries.build_filters(window_start, exporter_ids=scope.routers)
    where_sql += " AND bucket < :window_end"
    where_params["window_end"] = window_end

    rows = await conversation_queries.direction_totals(db, where_sql, where_params)
    side = {f"{d}_{unit}": 0 for d in conversation_queries.DIRECTIONS for unit in ("bytes", "packets")}
    side["flows"] = 0
    side.update({k: int(v or 0) for k, v in (rows[0] if rows else {}).items()})
    total_bytes = sum(side[f"{d}_bytes"] for d in conversation_queries.DIRECTIONS)
    total_packets = sum(side[f"{d}_packets"] for d in conversation_queries.DIRECTIONS)
    total_flows = side["flows"]

    # Routers that sent flows in the window (the sentinel is flowd's "unknown exporter").
    exporters_filters = [FlowMinuteRollup.bucket >= window_start, FlowMinuteRollup.bucket < window_end]
    if scope.routers is not None:
        exporters_filters.append(FlowMinuteRollup.exporter_id.in_(scope.routers))
    active_exporters = int((await db.execute(
        select(func.count(func.distinct(case(
            (FlowMinuteRollup.exporter_id != SENTINEL_UUID, FlowMinuteRollup.exporter_id), else_=None,
        )))).where(*exporters_filters)
    )).scalar() or 0)

    seconds = float(OVERVIEW_WINDOW_SECONDS)
    avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0

    # Anomalies use the same per-conversation rule as the conversation table and the PDF report:
    # a conversation is anomalous when the OR of its TCP flags over the window is SYN-only or
    # RST-without-FIN.
    anomaly_flows_count = 0
    anomaly_conversations_count = 0
    try:
        where_sql, where_params = conversation_queries.build_filters(window_start, exporter_ids=scope.routers)
        where_sql += " AND bucket < :window_end"
        summary = await conversation_queries.anomaly_summary(db, where_sql, {**where_params, "window_end": window_end})
        anomaly_flows_count = summary["flows"]
        anomaly_conversations_count = summary["conversations"]
    except Exception as exc:
        logger.debug("Failed to calculate anomaly flow count: %s", exc)

    wan = await _wan_overview(db, scope.routers, window_start, window_end, seconds)

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
            total_ingress_bps=round(bits_per_second(side["ingress_bytes"], seconds), 2),
            total_egress_bps=round(bits_per_second(side["egress_bytes"], seconds), 2),
            total_internal_bps=round(bits_per_second(side["internal_bytes"], seconds), 2),
            total_transit_bps=round(bits_per_second(side["transit_bytes"], seconds), 2),
            active_exporters_count=active_exporters,
            unmatched_exporters_count=unmatched_count,
            total_flows_count=total_flows,
            total_ingress_pps=round(side["ingress_packets"] / seconds, 2),
            total_egress_pps=round(side["egress_packets"] / seconds, 2),
            total_internal_pps=round(side["internal_packets"] / seconds, 2),
            total_transit_pps=round(side["transit_packets"] / seconds, 2),
            avg_packet_size=avg_packet_size,
            anomaly_flows_count=anomaly_flows_count,
            anomaly_conversations_count=anomaly_conversations_count,
            window_seconds=OVERVIEW_WINDOW_SECONDS,
            exporter_id=scope.device,
            **wan,
        )
    )


def _band(band: Any) -> Optional[UsualBand]:
    if band is None:
        return None
    return UsualBand(low_bps=round(band.low_bps, 2), high_bps=round(band.high_bps, 2), samples=band.samples)


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None else None


def _rates(values: List[Optional[float]]) -> List[Optional[float]]:
    # Whole bits/s: the charts need no more, and a 30-day chart has 720 points per column.
    return [round(v) if v is not None else None for v in values]


def _series_item(s: Any) -> WanLinkSeriesItem:
    return WanLinkSeriesItem(
        bucket_seconds=s.bucket_seconds,
        times=s.times,
        in_bps=_rates(s.in_bps),
        out_bps=_rates(s.out_bps),
        peak_in_bps=_rates(s.peak_in_bps),
        peak_out_bps=_rates(s.peak_out_bps),
        usual_in_low=_rates(s.usual_in_low),
        usual_in_high=_rates(s.usual_in_high),
        usual_out_low=_rates(s.usual_out_low),
        usual_out_high=_rates(s.usual_out_high),
        down_spans=[DownSpan(start=a, end=b) for a, b in s.down_spans],
        window_peak_bps=round(s.window_peak_bps),
        window_peak_percentage=_round(s.window_peak_pct, 1),
    )


@router.get("/wan-links", response_model=APIResponse)
async def get_wan_links(
    window: Optional[str] = Query(default=None),
    series: bool = Query(default=False),
    exporter_id: Optional[UUID] = Query(default=None),
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The Traffic page's "Now" area (build plan 7.5 step 4): every WAN link of the routers in
    scope over the last five complete minutes, against its speed, its usual range for this hour
    of the week and today's busiest minute, with what SNMP says of the port and any open traffic
    alert. Grouped by site (none last), busiest first within a site. Scope as on every other
    read here: the shared filter plus `device_id` (or its old name `exporter_id`).

    With `series=1` and a `window` (step 5), each link also carries its chart over the window
    (minutes up to 6h, hours beyond): in, out, each hour's busiest minute, the usual range per
    bucket and the spans SNMP saw the port down, for every link in one request."""
    delta = _get_window_delta(window) if series and window else None
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    now = await wan_links.wan_links_now(db, scope.routers, window=delta)
    items = []
    for r in now.links:
        w = r.link
        items.append(WanLinkNowItem(
            exporter_id=w.exporter_id,
            exporter_hostname=w.exporter_hostname,
            site=w.site,
            interface_idx=w.interface_idx,
            name=w.name,
            speed_mbps=w.speed_mbps,
            speed_source=w.speed_source,
            snmp_speed_mbps=w.snmp_speed_mbps,
            in_bps=round(r.in_bps, 2),
            out_bps=round(r.out_bps, 2),
            utilization_percentage=_round(r.utilization_pct),
            traffic_source="snmp" if r.from_snmp else "flow",
            usual_in=_band(r.usual_in),
            usual_out=_band(r.usual_out),
            usual_grouping=r.usual.grouping,
            usual_days=r.usual.days,
            above_usual_in_pct=_round(r.above_usual_in_pct, 1),
            above_usual_out_pct=_round(r.above_usual_out_pct, 1),
            today_peak_in_bps=_round(r.peak_in_bps),
            today_peak_in_hour=r.peak_in_hour,
            today_peak_out_bps=_round(r.peak_out_bps),
            today_peak_out_hour=r.peak_out_hour,
            link_state=r.link_state,
            down_since=r.down_since,
            snmp_last_ok_at=r.snmp_last_ok_at,
            alerts=[
                WanLinkAlertItem(kind=a.kind, direction=a.direction, since=a.since, peak_pct=_round(a.peak_pct, 1))
                for a in r.alerts
            ],
            series=_series_item(r.series) if r.series is not None else None,
        ))
    return APIResponse.success(data=WanLinksResponse(
        window_start=now.window_start,
        window_end=now.window_end,
        day_start=now.day_start,
        timezone=now.tz_name,
        links=items,
        window=window if delta is not None else None,
    ))


@router.get("/anomalies", response_model=APIResponse)
async def get_anomaly_summary(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Suspicious conversations over the picked window, for the Security tiles. Until v3.3.6 they
    came from the overview's five minutes while the table under them followed the picker
    (review P1); now both cover the same window and scope."""
    delta = _get_window_delta(window)
    _require_window_covered(delta, _CONVERSATION_ROLLUP_RETENTION, "Conversations")
    start_time = datetime.datetime.now(datetime.timezone.utc) - delta
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    where_sql, where_params = conversation_queries.build_filters(start_time, exporter_ids=scope.routers)
    summary = await conversation_queries.anomaly_summary(db, where_sql, where_params)
    return APIResponse.success(data=AnomalySummaryResponse(
        window=window,
        **{k: summary[k] for k in (
            "conversations", "flows", "all_flows", "syn_conversations", "syn_flows",
            "rst_conversations", "rst_flows",
        )},
    ))


async def _wan_overview(
    db: AsyncSession,
    routers: Optional[List[UUID]],
    window_start: datetime.datetime,
    window_end: datetime.datetime,
    seconds: float,
) -> Dict[str, Any]:
    """WAN-edge fields of the overview: totals and one row per WAN link, busiest first."""
    interfaces = await wan_edge.list_wan_interfaces(db, exporter_ids=routers)
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
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
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
    Without `wan_only` ("All flows"), points carry the four directions of the Traffic zones
    rule, each conversation counted once (see _direction_point), over the routers in scope.
    One picked device (`device_id`, or its old name `exporter_id`) keeps its interface totals.
    """
    delta = _get_window_delta(window)
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = now - delta
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)

    if wan_only and not endpoint_id and interface_idx is None:
        wan_interfaces = await wan_edge.list_wan_interfaces(db, exporter_ids=scope.routers)
        if wan_interfaces:
            predicate, params = wan_edge.interface_filter_sql(wan_interfaces)
            return await _interface_series(db, window, delta, start_time, predicate, params, wan_only=True)

    # One device (optionally one interface): interface rollups.
    if scope.device and not endpoint_id:
        params: Dict[str, Any] = {"exporter_id": str(scope.device)}
        predicate = "exporter_id = CAST(:exporter_id AS uuid)"
        if interface_idx is not None:
            predicate += " AND interface_idx = :interface_idx"
            params["interface_idx"] = interface_idx
        return await _interface_series(db, window, delta, start_time, predicate, params)

    # The routers in scope up to 30d: the four directions of the zones rule, each conversation
    # counted once (conversations.direction_totals). 1h and 6h read minutes, longer windows the
    # hourly conversation tier, so every point is one bucket long.
    if endpoint_id is None and delta <= WINDOWS["30d"]:
        bucket_seconds = 60.0 if delta <= WINDOWS["6h"] else 3600.0
        where_sql, params = conversation_queries.build_filters(start_time, exporter_ids=scope.routers)
        rows = await conversation_queries.direction_totals(
            db, where_sql, params, tier="minute" if bucket_seconds == 60.0 else "hour", by_bucket=True,
        )
        return APIResponse.success(
            data=TrafficSeriesResponse(
                window=window,
                points=[_direction_point(row, bucket_seconds) for row in rows],
                bucket_seconds=int(bucket_seconds),
                directional=True,
            )
        )

    # One endpoint's traffic, or the fleet over 1y (daily totals, no direction)
    if endpoint_id:
        _require_window_covered(delta, _ENDPOINT_SERIES_RETENTION, "One endpoint's hourly totals")
    model, bucket_seconds = _select_rollup_model_and_bucket_seconds(window, endpoint_id)
    directional = hasattr(model, "src_endpoint_id")

    filters = [model.bucket >= start_time]
    if scope.routers is not None:
        filters.append(model.exporter_id.in_(scope.routers))
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


def _direction_point(row: Dict[str, Any], bucket_seconds: float) -> TrafficSeriesPoint:
    def bps(side: str) -> float:
        return round(bits_per_second(int(row[f"{side}_bytes"] or 0), bucket_seconds), 2)

    def pps(side: str) -> float:
        return round(int(row[f"{side}_packets"] or 0) / bucket_seconds, 2)

    return TrafficSeriesPoint(
        timestamp=row["bucket"].astimezone(datetime.timezone.utc).isoformat(),
        ingress_bps=bps("ingress"), egress_bps=bps("egress"),
        internal_bps=bps("internal"), transit_bps=bps("transit"),
        ingress_pps=pps("ingress"), egress_pps=pps("egress"),
        internal_pps=pps("internal"), transit_pps=pps("transit"),
        flows_per_sec=round(int(row["flows"] or 0) / bucket_seconds, 2),
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
    is kept 365 days (the minute table keeps 7) and keeps a 7-day chart at 168 points, not 10,080.
    """
    hourly = delta > datetime.timedelta(hours=6)
    bucket_seconds = 3600 if hourly else 60
    # SNMP counters where the interface has them, flows otherwise (v3.3.6, services/interface_traffic).
    source = interface_traffic.source_for(hourly, "{t}.bucket >= :start")
    res = await db.execute(
        text(f"""
            SELECT bucket,
                   COALESCE(SUM(in_bytes), 0) AS in_bytes, COALESCE(SUM(out_bytes), 0) AS out_bytes,
                   COALESCE(SUM(in_packets), 0) AS in_packets, COALESCE(SUM(out_packets), 0) AS out_packets,
                   COALESCE(SUM(flow_count), 0) AS flow_count
            FROM {source} t
            WHERE {predicate}
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
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Top addresses by total volume, plus the largest conversations (first page of
    GET /bandwidth/conversations, kept for existing clients).

    Each address's ingress (received, as destination) and egress (sent, as source) come from
    one aggregation, so neither side is cut off by a separate per-direction limit. `sites`:
    the endpoint's site, or the sites of the routers that saw the address.
    """
    delta = _get_window_delta(window)
    _require_window_covered(delta, _CONVERSATION_ROLLUP_RETENTION, "Conversations")
    start_time = datetime.datetime.now(datetime.timezone.utc) - delta
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    where_sql, params = conversation_queries.build_filters(
        start_time, endpoint_id=endpoint_id, exporter_ids=scope.routers
    )
    counted_once, params = conversation_queries.counted_once_cte(where_sql, params)

    talkers_res = await db.execute(
        text(f"""
            WITH {counted_once},
            sides AS (
                SELECT src_ip AS ip, src_endpoint_id AS endpoint_id,
                       0::BIGINT AS ingress_bytes, bytes AS egress_bytes,
                       flow_count, packets
                FROM counted_once
                UNION ALL
                SELECT dst_ip, dst_endpoint_id,
                       bytes, 0::BIGINT,
                       flow_count, packets
                FROM counted_once
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

    # Where each talker is (review O5): an endpoint's own site, else the sites of the routers
    # that saw it (a user's PC or an Internet address is no endpoint).
    seen_by = await conversation_queries.address_exporters(
        db, where_sql, dict(params), [str(r["ip"]) for r in talker_rows]
    )
    ep_ids = {r["endpoint_id"] for r in talker_rows if r["endpoint_id"]}
    ep_ids |= {e for exporters in seen_by.values() for e in exporters}
    hostnames: Dict[Any, str] = {}
    sites: Dict[Any, Optional[str]] = {}
    if ep_ids:
        h_res = await db.execute(select(Endpoint.id, Endpoint.hostname, Endpoint.site).where(Endpoint.id.in_(ep_ids)))
        for e_id, h_name, site in h_res.all():
            hostnames[e_id], sites[e_id] = h_name, site

    def talker_sites(row: Dict[str, Any]) -> List[str]:
        own = sites.get(row["endpoint_id"]) if row["endpoint_id"] else None
        if own:
            return [own]
        exporters = seen_by.get(str(row["ip"]).split("/")[0], [])
        return sorted({sites[e] for e in exporters if sites.get(e)})

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
                sites=talker_sites(r),
            )
        )

    page = await conversation_queries.query_conversations(
        db, start_time, endpoint_id=endpoint_id, limit=limit, exporter_ids=scope.routers
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
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Conversations (src, dst, protocol, service port) filtered and paged on the server.

    Filters: `ip` (either side), `port` (service port), `dscp`, plus `quick` = all | anomalies |
    qos | bulk. `counts` gives the size of each quick filter over every conversation matching
    the other filters, not just the returned page. `sort` = bytes | flows | packets. The shared
    filter and `device_id` keep the conversations the routers in scope saw.
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

    delta = _get_window_delta(window)
    _require_window_covered(delta, _CONVERSATION_ROLLUP_RETENTION, "Conversations")
    start_time = datetime.datetime.now(datetime.timezone.utc) - delta
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    page = await conversation_queries.query_conversations(
        db,
        start_time,
        exporter_ids=scope.routers,
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


@router.get("/applications", response_model=APIResponse)
async def get_applications_breakdown(
    window: str = Query(default="1h"),
    exporter_id: Optional[UUID] = Query(default=None),
    endpoint_id: Optional[UUID] = Query(default=None),
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Services, QoS classes and traffic zones over the window.

    - Services: the top 15 (protocol, service port) by bytes, named by flow_metrics.service_label,
      plus "Other"; shares are of all traffic in the window.
    - QoS: bytes, packets and flows per DSCP class (RFC 2474 / 3246).
    - Zones: East-West (both ends internal), North-South Egress (internal to external),
      North-South Ingress (external to internal), External / Transit (neither). Internal means
      a monitored endpoint or a private address (flow_metrics.is_internal_address).

    All three count each conversation once, however many routers reported it, and read the
    same rows as Conversations (conversations.traffic_breakdown), so their totals agree. Those
    rows are kept 90 days: a 1y window gets its last 90 days, and `window_start` says so.
    """
    delta = _get_window_delta(window)
    if endpoint_id:
        _require_window_covered(delta, _ENDPOINT_SERIES_RETENTION, "One endpoint's hourly totals")
    now = datetime.datetime.now(datetime.timezone.utc)
    start_time = max(now - delta, now - _CONVERSATION_ROLLUP_RETENTION)

    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    where_sql, params = conversation_queries.build_filters(
        start_time, endpoint_id=endpoint_id, exporter_ids=scope.routers
    )
    params["end"] = now
    breakdown = await conversation_queries.traffic_breakdown(db, where_sql, params)
    overall_bytes = breakdown["total_bytes"]

    def share(part: int) -> float:
        return round(part / overall_bytes * 100.0, 2) if overall_bytes > 0 else 0.0

    apps: List[ApplicationDistributionItem] = [
        ApplicationDistributionItem(
            protocol_name=PROTOCOL_MAP.get(proto_id, f"P-{proto_id}"),
            port=port,
            service_label=service_label(proto_id, port),
            total_bytes=b_val,
            percentage=share(b_val),
        )
        for proto_id, port, b_val in breakdown["applications"][:15]
    ]
    other_bytes = overall_bytes - sum(a.total_bytes for a in apps)
    if other_bytes > 0:
        apps.append(
            ApplicationDistributionItem(
                protocol_name="Other", port=0, service_label="Other",
                total_bytes=other_bytes, percentage=share(other_bytes), is_other=True,
            )
        )

    qos_classes: List[QoSDistributionItem] = [
        QoSDistributionItem(
            dscp=item["dscp"],
            dscp_label=item["dscp_label"],
            total_bytes=item["total_bytes"],
            total_packets=item["total_packets"],
            flow_count=item["flow_count"],
            percentage=round(item["percentage"], 2),
        )
        for item in dscp_breakdown(breakdown["qos"])
    ]

    zone_bytes = sum(z["bytes"] for z in breakdown["zones"].values())
    traffic_zones = [
        TrafficZoneItem(
            zone=name,
            total_bytes=breakdown["zones"][name]["bytes"],
            flow_count=breakdown["zones"][name]["flows"],
            percentage=round(breakdown["zones"][name]["bytes"] / zone_bytes * 100.0, 2) if zone_bytes else 0.0,
        )
        for name in TRAFFIC_ZONES
    ]

    return APIResponse.success(
        data=ApplicationDistributionResponse(
            applications=apps,
            qos_classes=qos_classes,
            qos_source=conversation_queries.tier_table(where_sql, params),
            qos_window_start=start_time,
            traffic_zones=traffic_zones,
            total_bytes=overall_bytes,
            zones_resolution="ip",
            zones_window_start=start_time,
            window_start=start_time,
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


# Roles that mark an endpoint as a flow exporter (enrol sets FLOW_EXPORTER).
FLOW_EXPORTER_ROLES = ("FLOW_EXPORTER", "HYBRID_GATEWAY")


def _text_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


async def _snmp_device_ids(db: AsyncSession) -> List[str]:
    """Endpoints ephemres-snmpd keeps poll state for (migration 0026)."""
    try:
        res = await db.execute(text("SELECT endpoint_id FROM snmp_device_state"))
        return [str(r[0]) for r in res.all()]
    except Exception:
        # A database before migration 0026 has no SNMP.
        return []


@router.get("/exporters", response_model=APIResponse)
async def list_flow_exporters(
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The Traffic page's devices, and flow sources not yet registered.

    Devices are routers that export flows or are polled over SNMP (an SNMP-only router has
    traffic too: its WAN links' counters). With the shared filter, only the devices it matches,
    so the picker offers what the page can show. Sorted by site (none last), then name.
    Unregistered sources are not filtered: they have no site or tags yet.
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

    filter_conditions = (
        endpoint_filter_conditions(endpoint_filter) if isinstance(endpoint_filter, EndpointFilter) else []
    )
    # Polled over SNMP: a credential set now, or poll state from ephemres-snmpd (its counters
    # stay behind the WAN charts after a credential is removed).
    snmp_ids = await _snmp_device_ids(db)
    stmt = select(Endpoint).where(
        Endpoint.deleted_at.is_(None),
        Endpoint.endpoint_status != "DELETED",
        or_(
            Endpoint.device_role.in_(FLOW_EXPORTER_ROLES),
            func.cardinality(Endpoint.flow_exporter_ips) > 0,
            Endpoint.id.in_(recent_exporters_sub),
            Endpoint.id.in_(recent_if_exporters_sub),
            Endpoint.snmp_credential_id.is_not(None),
            Endpoint.id.in_(snmp_ids),
        ),
        *filter_conditions,
    ).order_by(Endpoint.hostname.asc())
    res = await db.execute(stmt)
    endpoints = sorted(
        res.scalars().all(),
        key=lambda ep: (getattr(ep, "site", None) is None, str(getattr(ep, "site", "") or "").lower(), ep.hostname.lower()),
    )

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
        sends_flows = bool(
            getattr(ep, "device_role", None) in FLOW_EXPORTER_ROLES or exp_ips or str(ep.id) in health
        )
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
                site=_text_or_none(getattr(ep, "site", None)),
                snmp_polled=getattr(ep, "snmp_credential_id", None) is not None or str(ep.id) in snmp_ids,
                sends_flows=sends_flows,
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
    exporter_id: Optional[UUID] = Query(default=None),
    window: str = Query(default="1h"),
    device_id: Optional[UUID] = Query(default=None),
    endpoint_filter: EndpointFilter = Depends(_traffic_filter),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Interfaces with their traffic now (last five complete minutes) and over the window (average,
    P95, busiest minute) and their status: of one device (`device_id`, or its old name
    `exporter_id`), or across every router the shared filter picks (build plan 7.5 step 6).

    Status comes from SNMP for a polled router (up / down / shut down, with the time it was down
    in the window), else from whether flows showed traffic. One device: by index; across devices:
    WAN links first, then the busiest; shut-down ports last (services/interface_table)."""
    delta = _get_window_delta(window)
    picked = _as_uuid(device_id) or _as_uuid(exporter_id)
    scope = await _scope(db, endpoint_filter, device_id, exporter_id)
    routers = scope.routers
    if picked is not None and routers is None:
        routers = [picked]

    # Interfaces flowd saw lately on each router, before they reach the rollups (FastStore).
    active: Dict[str, set] = {}
    store_client = await get_flow_store()
    if store_client and routers:
        for router_id in routers:
            try:
                members = await store_client.smembers(f"flow:exporter:{router_id}:interfaces")
                active[str(router_id)] = {int(m) for m in members if str(m).lstrip("-").isdigit()}
            except Exception as e:
                logger.debug("Failed to read active interfaces from FastStore: %s", e)

    table = await interface_table.interface_table(
        db, routers, delta, active_indices=active, single_device=picked is not None,
    )
    items = []
    for r in table.rows:
        alias_data = r.alias_data
        items.append(InterfaceTelemetryItem(
            **r.source_info,
            traffic_source=r.traffic_source,
            exporter_id=r.exporter_id,
            exporter_hostname=r.exporter_hostname,
            site=r.site,
            interface_idx=r.interface_idx,
            name=r.name,
            speed_mbps=r.speed_mbps,
            speed_configured=r.speed_configured,
            role=r.role,
            alert=alias_data.get("alert") if isinstance(alias_data, dict) and isinstance(alias_data.get("alert"), dict) else None,
            in_bytes=r.in_bytes,
            out_bytes=r.out_bytes,
            in_bps=round(r.in_bps, 2),
            out_bps=round(r.out_bps, 2),
            in_packets=r.in_packets,
            out_packets=r.out_packets,
            flow_count=r.flow_count,
            utilization_percentage=r.utilization_percentage,
            is_active=r.is_active,
            in_p95_bps=round(r.in_p95_bps, 2),
            out_p95_bps=round(r.out_p95_bps, 2),
            in_peak_bps=round(r.in_peak_bps, 2),
            out_peak_bps=round(r.out_peak_bps, 2),
            p95_utilization_percentage=r.p95_utilization_percentage,
            peak_utilization_percentage=r.peak_utilization_percentage,
            now_in_bps=round(r.now_in_bps, 2),
            now_out_bps=round(r.now_out_bps, 2),
            status=r.status,
            admin_status=r.admin_status,
            down_spans=[DownSpan(start=a, end=b) for a, b in r.down_spans],
            down_seconds=r.down_seconds,
        ))

    return APIResponse.success(
        data=InterfaceTelemetryResponse(
            exporter_id=picked if picked is not None and scope.routers != [] else None,
            window=window,
            interfaces=items,
            devices=len({i.exporter_id for i in items}),
            covered_seconds=int(table.covered_seconds),
            p95_resolution_seconds=table.slot_seconds,
            # The tier, named as before v3.3.6 (clients may read it). SNMP minutes are mixed in per
            # interface where present; each interface's traffic_source says so.
            source="flow_interface_hourly_rollups" if table.hourly else "flow_interface_minute_rollups",
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
        # No name typed: the key is left out (v3.3.6), so the device's SNMP name shows instead;
        # a stored null would read as the text "None". Other fields keep their documented shape
        # (speed_mbps null when unset).
        entry = config.model_dump()
        if not entry.get("name"):
            entry.pop("name", None)
        aliases[idx_str] = entry

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
