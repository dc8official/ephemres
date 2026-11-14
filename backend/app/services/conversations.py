"""Server-side conversation queries over flow_minute_rollups and its hourly aggregate.

A conversation is (src_ip, dst_ip, protocol, service port) over the window. Filters, the quick
triage filters and their counts are evaluated in SQL across every matching conversation, so the
UI no longer filters a 25-row sample.

Windows over HOURLY_TIER_MIN_SPAN read flow_conversation_hourly_rollups (migration 0021) for
their whole hours and the minute table only for the partial first hour; see counted_once_cte.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

from app.core.orm import AsyncSession, text
from app.schemas.bandwidth import (
    ConversationRouter,
    TopConversationItem,
    derive_traffic_profile,
    format_dscp_label,
    format_tcp_flags,
)
from app.services import wan_edge
from app.services.flow_metrics import (
    PROTOCOL_MAP,
    TRAFFIC_ZONES,
    ZONE_EAST_WEST,
    ZONE_EXTERNAL_TRANSIT,
    ZONE_NORTH_SOUTH_EGRESS,
    ZONE_NORTH_SOUTH_INGRESS,
    bulk_sql,
    dscp_from_tos,
    service_name,
    tcp_anomaly_sql,
    zone_sql,
)

QUICK_FILTERS = ("all", "anomalies", "qos", "bulk")
SORT_COLUMNS = {"bytes": "total_bytes", "flows": "total_flows", "packets": "total_packets"}

# Longer windows read the hourly aggregate; a shorter one has no whole hours worth skipping.
HOURLY_TIER_MIN_SPAN = datetime.timedelta(hours=2)
# The start of the window in every filter this module builds or is given.
START_CLAUSE = "bucket >= :start"

_QUICK_WHERE = {
    "all": "TRUE",
    "anomalies": "anomaly_hint IS NOT NULL",
    "qos": "dscp > 0",
    "bulk": "is_bulk",
}

# exporter_id of flows from a source matched to no endpoint (monitoring.flow.aggregator.SENTINEL_UUID).
UNMATCHED_EXPORTER = "00000000-0000-0000-0000-000000000000"

_MINUTE_TABLE = "flow_minute_rollups"
_HOURLY_TABLE = "flow_conversation_hourly_rollups"
_BUCKET_SECONDS = {_MINUTE_TABLE: 60, _HOURLY_TABLE: 3600}


def _counted_once_select(table: str, where_sql: str) -> str:
    # bucket_seconds: how much time each row stands for, so the time a conversation was seen
    # (the Bulk rule) adds up the same over minute and hourly rows.
    return f"""
            SELECT bucket, src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, protocol, dst_port,
                   MAX(bytes) AS bytes,
                   MAX(flow_count) AS flow_count,
                   MAX(COALESCE(packets, 0)) AS packets,
                   BIT_OR(tcp_flags) AS tcp_flags,
                   MAX(tos) AS tos,
                   {_BUCKET_SECONDS[table]} AS bucket_seconds
            FROM {table}
            WHERE {where_sql}
            GROUP BY bucket, src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, protocol, dst_port"""


def _window_end(params: Dict[str, Any]) -> datetime.datetime:
    return params.get("end") or params.get("window_end") or datetime.datetime.now(datetime.timezone.utc)


def _tiers(where_sql: str, params: Dict[str, Any], tier: Optional[str] = None) -> tuple[list[tuple[str, str]], Dict[str, Any]]:
    """[(table, where)] covering the window once, and the parameters to run them with.

    By default: windows up to HOURLY_TIER_MIN_SPAN read the minute table; longer ones read the
    hourly aggregate for their whole hours and the minute table for the partial first hour, so
    the start stays exact. `tier` "minute" or "hour" forces one table, for a chart whose points
    must all be the same length.
    """
    if tier == "minute":
        return [(_MINUTE_TABLE, where_sql)], params
    if tier == "hour":
        return [(_HOURLY_TABLE, where_sql)], params
    start = params["start"]
    if _window_end(params) - start <= HOURLY_TIER_MIN_SPAN:
        return [(_MINUTE_TABLE, where_sql)], params
    hour_start = start.replace(minute=0, second=0, microsecond=0)
    if hour_start < start:
        hour_start += datetime.timedelta(hours=1)
    return [
        (_MINUTE_TABLE, f"{where_sql} AND bucket < :hour_start"),
        (_HOURLY_TABLE, where_sql.replace(START_CLAUSE, "bucket >= :hour_start")),
    ], {**params, "hour_start": hour_start}


def counted_once_cte(
    where_sql: str, params: Dict[str, Any], tier: Optional[str] = None
) -> tuple[str, Dict[str, Any]]:
    """`counted_once`: the matching rollup rows with each conversation counted once per bucket.
    Traffic crossing two exporting routers is reported by both; summing the reports showed it
    twice, so only the largest one is kept. Returns the CTE and the parameters to run it with.

    The tables read are chosen by _tiers. A window's end (`bucket <= :end`) counts its last
    hour whole. In the whole hours the largest report is taken per hour instead of per minute:
    the same when routers report a conversation alike, and lower than the minute figure when a
    conversation moved between routers within the hour.
    """
    tiers, params = _tiers(where_sql, params, tier)
    selects = "\n UNION ALL ".join(_counted_once_select(table, where) for table, where in tiers)
    return f"counted_once AS ({selects}\n)", params


def tier_table(where_sql: str, params: Dict[str, Any]) -> str:
    """The table holding most of the window: what the API reports as the data's source."""
    tiers, _ = _tiers(where_sql, params)
    return tiers[-1][0]


def _conversation_cte(where_sql: str, params: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    counted_once, params = counted_once_cte(where_sql, params)
    avg_packet = "COALESCE(total_bytes / NULLIF(total_packets, 0), 0)"
    return f"""
        WITH {counted_once},
        conv AS (
            SELECT src_ip, dst_ip, protocol, dst_port,
                   SUM(bytes) AS total_bytes,
                   SUM(flow_count) AS total_flows,
                   SUM(packets) AS total_packets,
                   BIT_OR(tcp_flags) AS tcp_flags_mask,
                   MAX(tos) AS max_tos,
                   SUM(bucket_seconds) AS seen_seconds,
                   -- First and last seen (review S1): the start of the first bucket and the end
                   -- of the last, so to the minute up to 2 hours and to the hour beyond (the
                   -- hourly tier). The current bucket ends in the future: capped at now.
                   MIN(bucket) AS first_seen,
                   LEAST(MAX(bucket + bucket_seconds * INTERVAL '1 second'), now()) AS last_seen
            FROM counted_once
            GROUP BY src_ip, dst_ip, protocol, dst_port
        ),
        labelled AS (
            SELECT conv.*,
                   {tcp_anomaly_sql("tcp_flags_mask", "protocol")} AS anomaly_hint,
                   (COALESCE(max_tos, 0) & 255) >> 2 AS dscp,
                   {avg_packet} AS avg_packet_size,
                   {bulk_sql(avg_packet, "seen_seconds", "total_bytes")} AS is_bulk
            FROM conv
        )
    """, params


def build_filters(
    start: datetime.datetime,
    exporter_id: Optional[UUID] = None,
    endpoint_id: Optional[UUID] = None,
    ip: Optional[str] = None,
    port: Optional[int] = None,
    exporter_ids: Optional[Sequence[Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    """WHERE text and parameters for the conversation tables. `exporter_ids` is a Traffic page
    scope (endpoint_filter.traffic_scope): None for every router, else only rows those routers
    reported, so an empty list matches nothing."""
    clauses = [START_CLAUSE]
    params: Dict[str, Any] = {"start": start}
    if exporter_id is not None:
        clauses.append("exporter_id = CAST(:exporter_id AS uuid)")
        params["exporter_id"] = str(exporter_id)
    if exporter_ids is not None:
        clauses.append("exporter_id = ANY(CAST(:exporter_ids AS uuid[]))")
        params["exporter_ids"] = tuple(str(e) for e in exporter_ids)
    if endpoint_id is not None:
        clauses.append(
            "(src_endpoint_id = CAST(:endpoint_id AS uuid) OR dst_endpoint_id = CAST(:endpoint_id AS uuid))"
        )
        params["endpoint_id"] = str(endpoint_id)
    if ip:
        clauses.append("(src_ip = CAST(:ip AS inet) OR dst_ip = CAST(:ip AS inet))")
        params["ip"] = ip
    if port is not None:
        clauses.append("dst_port = :port")
        params["port"] = port
    return " AND ".join(clauses), params


async def _hostnames(db: AsyncSession, ips: Sequence[str]) -> Dict[str, str]:
    if not ips:
        return {}
    # One bind parameter per address: list parameters are not passed through as arrays.
    params = {f"ip{i}": ip for i, ip in enumerate(ips)}
    placeholders = ", ".join(f":{name}" for name in params)
    res = await db.execute(
        text(f"""
            SELECT host(ip_address) AS ip, hostname
            FROM endpoints
            WHERE deleted_at IS NULL AND host(ip_address) IN ({placeholders})
        """),
        params,
    )
    return {str(r["ip"]): r["hostname"] for r in res.mappings()}


def _ip_text(value: Any) -> str:
    return str(value).split("/")[0].strip()


def _conversation_key(row: Dict[str, Any]) -> tuple:
    return (_ip_text(row["src_ip"]), _ip_text(row["dst_ip"]), int(row["protocol"]), int(row["dst_port"]))


def to_item(
    row: Dict[str, Any], hostnames: Dict[str, str], routers: Sequence[ConversationRouter] = ()
) -> TopConversationItem:
    proto_id = int(row["protocol"])
    proto_name = PROTOCOL_MAP.get(proto_id, f"PROTO-{proto_id}")
    flags = int(row["tcp_flags_mask"] or 0)
    tos = int(row["max_tos"] or 0)
    total_bytes = int(row["total_bytes"] or 0)
    total_flows = int(row["total_flows"] or 0)
    total_packets = int(row["total_packets"] or 0)
    avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0
    # Whole packets: "156608.2 pkts/flow" was noise (review C4).
    packets_per_flow = round(total_packets / total_flows) if total_flows > 0 else 0
    dscp = dscp_from_tos(tos)
    port = int(row["dst_port"])
    anomaly = row["anomaly_hint"]
    src, dst = _ip_text(row["src_ip"]), _ip_text(row["dst_ip"])
    return TopConversationItem(
        src_ip=str(row["src_ip"]),
        dst_ip=str(row["dst_ip"]),
        src_hostname=hostnames.get(src),
        dst_hostname=hostnames.get(dst),
        protocol=proto_name,
        dst_port=port,
        total_bytes=total_bytes,
        flow_count=total_flows,
        total_packets=total_packets,
        avg_packet_size=avg_packet_size,
        packets_per_flow=packets_per_flow,
        service_name=service_name(proto_id, port),
        traffic_profile=derive_traffic_profile(
            anomaly, avg_packet_size, packets_per_flow, dscp, port, bulk=bool(row.get("is_bulk"))
        ),
        tcp_flags=flags,
        tcp_flags_labels=format_tcp_flags(flags) if proto_name == "TCP" else [],
        tos=tos,
        dscp=dscp,
        dscp_label=format_dscp_label(tos),
        anomaly_hint=anomaly,
        first_seen=row.get("first_seen"),
        last_seen=row.get("last_seen"),
        routers=list(routers),
    )


async def conversation_routers(
    db: AsyncSession, where_sql: str, params: Dict[str, Any], rows: Sequence[Dict[str, Any]]
) -> Dict[tuple, list[ConversationRouter]]:
    """{conversation key: [ConversationRouter]} for `rows` (one page): the routers in scope that
    reported each, by hostname, with their site (review C1) and their WAN link when they have
    exactly one (review S1). `where_sql` / `params` are build_filters' own, before the CTE."""
    seen = await conversation_exporters(db, where_sql, dict(params), [_conversation_key(r) for r in rows])
    # The all-zero id stands for flows from a source matched to no endpoint (v3.3.0 stored those;
    # an upgraded server keeps them for the conversation tier's 90 days). It is no router and has
    # no site, so it is left out rather than listed as a nameless one.
    seen = {key: [e for e in exporters if str(e) != UNMATCHED_EXPORTER] for key, exporters in seen.items()}
    ids = sorted({str(e) for exporters in seen.values() for e in exporters})
    if not ids:
        return {}
    res = await db.execute(
        text("""
            SELECT id, hostname, site FROM endpoints
            WHERE deleted_at IS NULL AND id = ANY(CAST(:ids AS uuid[]))
        """),
        {"ids": tuple(ids)},
    )
    known = {str(r["id"]): (r["hostname"], r["site"]) for r in res.mappings()}
    links: Dict[str, list] = {}
    for w in await wan_edge.list_wan_interfaces(db, exporter_ids=ids):
        links.setdefault(str(w.exporter_id), []).append(w.name)
    out: Dict[tuple, list[ConversationRouter]] = {}
    for key, exporters in seen.items():
        routers = []
        for e in {str(x) for x in exporters}:
            hostname, site = known.get(e, (None, None))
            own = links.get(e, [])
            routers.append(ConversationRouter(
                exporter_id=e, hostname=hostname, site=site, wan_link=own[0] if len(own) == 1 else None,
            ))
        # By site (none last), then name, so a row reads the same on every refresh.
        routers.sort(key=lambda r: (r.site is None, (r.site or "").lower(), (r.hostname or "").lower()))
        out[key] = routers
    return out


async def query_conversations(
    db: AsyncSession,
    start: datetime.datetime,
    *,
    exporter_id: Optional[UUID] = None,
    endpoint_id: Optional[UUID] = None,
    ip: Optional[str] = None,
    port: Optional[int] = None,
    dscp: Optional[int] = None,
    quick: str = "all",
    sort: str = "bytes",
    limit: int = 25,
    offset: int = 0,
    exporter_ids: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """One page of conversations plus quick-filter counts over every matching conversation."""
    where_sql, params = build_filters(start, exporter_id, endpoint_id, ip, port, exporter_ids=exporter_ids)
    filter_params = dict(params)
    post = "TRUE"
    if dscp is not None:
        post = "dscp = :dscp"
        params["dscp"] = dscp
    cte, params = _conversation_cte(where_sql, params)

    counts_res = await db.execute(
        text(f"""
            {cte}
            SELECT COUNT(*) AS all_count,
                   COUNT(*) FILTER (WHERE {_QUICK_WHERE["anomalies"]}) AS anomalies,
                   COUNT(*) FILTER (WHERE {_QUICK_WHERE["qos"]}) AS qos,
                   COUNT(*) FILTER (WHERE {_QUICK_WHERE["bulk"]}) AS bulk
            FROM labelled WHERE {post}
        """),
        params,
    )
    c = dict(counts_res.mappings().first() or {})
    counts = {
        "all": int(c.get("all_count") or 0),
        "anomalies": int(c.get("anomalies") or 0),
        "qos": int(c.get("qos") or 0),
        "bulk": int(c.get("bulk") or 0),
    }

    order_col = SORT_COLUMNS.get(sort, "total_bytes")
    page_res = await db.execute(
        text(f"""
            {cte}
            SELECT * FROM labelled
            WHERE {post} AND {_QUICK_WHERE.get(quick, "TRUE")}
            ORDER BY {order_col} DESC, total_bytes DESC
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    )
    rows = [dict(r) for r in page_res.mappings()]
    ips = sorted({_ip_text(r["src_ip"]) for r in rows} | {_ip_text(r["dst_ip"]) for r in rows})
    hostnames = await _hostnames(db, ips)
    routers = await conversation_routers(db, where_sql, filter_params, rows)
    return {
        "items": [to_item(r, hostnames, routers.get(_conversation_key(r), [])) for r in rows],
        "counts": counts,
        "total": counts.get(quick, counts["all"]),
    }


async def top_conversation_rows(
    db: AsyncSession, where_sql: str, params: Dict[str, Any], limit: int = 10
) -> list[Dict[str, Any]]:
    """The largest conversations over a filtered window, counted once, as raw rows (the PDF's
    top ten; to_item turns one into the API's item)."""
    cte, params = _conversation_cte(where_sql, params)
    res = await db.execute(
        text(f"{cte} SELECT * FROM labelled ORDER BY total_bytes DESC LIMIT :limit"),
        {**params, "limit": limit},
    )
    return [dict(r) for r in res.mappings()]


async def anomaly_summary(db: AsyncSession, where_sql: str, params: Dict[str, Any]) -> Dict[str, int]:
    """Flows and conversations classified anomalous by the shared rule over a filtered window, and
    every flow in it (`all_flows`, for "of N flows")."""
    cte, params = _conversation_cte(where_sql, params)
    res = await db.execute(
        text(f"""
            {cte}
            SELECT COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint IS NOT NULL), 0) AS flows,
                   COALESCE(SUM(total_flows), 0) AS all_flows,
                   COUNT(*) FILTER (WHERE anomaly_hint IS NOT NULL) AS conversations,
                   COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint = 'SYN_ONLY'), 0) AS syn_flows,
                   COALESCE(SUM(total_bytes) FILTER (WHERE anomaly_hint = 'SYN_ONLY'), 0) AS syn_bytes,
                   COUNT(*) FILTER (WHERE anomaly_hint = 'SYN_ONLY') AS syn_conversations,
                   COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint = 'TCP_RST'), 0) AS rst_flows,
                   COALESCE(SUM(total_bytes) FILTER (WHERE anomaly_hint = 'TCP_RST'), 0) AS rst_bytes,
                   COUNT(*) FILTER (WHERE anomaly_hint = 'TCP_RST') AS rst_conversations
            FROM labelled
        """),
        params,
    )
    row = dict(res.mappings().first() or {})
    return {
        k: int(row.get(k) or 0)
        for k in (
            "flows", "all_flows", "conversations", "syn_flows", "syn_bytes", "syn_conversations",
            "rst_flows", "rst_bytes", "rst_conversations",
        )
    }



async def traffic_breakdown(db: AsyncSession, where_sql: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """Applications, QoS classes, traffic zones and the total over a filtered window, each
    conversation counted once (counted_once_cte), in one pass over the rollups.

    The Traffic page and the PDF both read this. Applications and zones used to sum every
    router's report, so traffic between two sites counted twice there and once in
    Conversations (review P2). Returns:
    - applications: [(protocol, dst_port, bytes)], largest first, every service;
    - qos: [(tos, bytes, packets, flows)] for flow_metrics.dscp_breakdown;
    - zones: {zone: {"bytes", "flows"}} for every zone in TRAFFIC_ZONES;
    - total_bytes.
    """
    counted_once, params = counted_once_cte(where_sql, params)
    zone = zone_sql("src_ip", "src_endpoint_id", "dst_ip", "dst_endpoint_id")
    # GROUPING() is 0 for the columns a row is grouped by: one row per service, per ToS byte,
    # per zone, and the grand total (every bit set), from a single scan.
    res = await db.execute(
        text(f"""
            WITH {counted_once},
            zoned AS (
                SELECT protocol, dst_port, tos, bytes, packets, flow_count, {zone} AS zone
                FROM counted_once
            )
            SELECT GROUPING(protocol, dst_port) AS g_app, GROUPING(tos) AS g_tos,
                   GROUPING(zone) AS g_zone, protocol, dst_port, tos, zone,
                   COALESCE(SUM(bytes), 0) AS bytes, COALESCE(SUM(packets), 0) AS packets,
                   COALESCE(SUM(flow_count), 0) AS flows
            FROM zoned
            GROUP BY GROUPING SETS ((protocol, dst_port), (tos), (zone), ())
        """),
        params,
    )
    applications, qos = [], []
    zones = {name: {"bytes": 0, "flows": 0} for name in TRAFFIC_ZONES}
    total_bytes = 0
    for r in res.mappings():
        b, p, f = int(r["bytes"] or 0), int(r["packets"] or 0), int(r["flows"] or 0)
        if r["g_app"] == 0:
            applications.append((int(r["protocol"]), int(r["dst_port"]), b))
        elif r["g_tos"] == 0:
            qos.append((r["tos"], b, p, f))
        elif r["g_zone"] == 0:
            zones[r["zone"]] = {"bytes": b, "flows": f}
        else:
            total_bytes = b
    applications.sort(key=lambda a: a[2], reverse=True)
    return {"applications": applications, "qos": qos, "zones": zones, "total_bytes": total_bytes}


async def conversation_exporters(
    db: AsyncSession, where_sql: str, params: Dict[str, Any], keys: Sequence[tuple]
) -> Dict[tuple, list]:
    """{(src_ip, dst_ip, protocol, dst_port): [exporter ids]}: the routers that reported each
    of `keys` in the window. A conversation counted once may have been seen by several; the
    PDF names them all."""
    if not keys:
        return {}
    tiers, params = _tiers(where_sql, params)
    values, key_params = [], {}
    for i, (src, dst, proto, port) in enumerate(keys):
        values.append(f"(CAST(:ks{i} AS inet), CAST(:kd{i} AS inet), CAST(:kp{i} AS smallint), CAST(:kt{i} AS integer))")
        key_params.update({f"ks{i}": _ip_text(src), f"kd{i}": _ip_text(dst), f"kp{i}": int(proto), f"kt{i}": int(port)})
    wanted = f"(src_ip, dst_ip, protocol, dst_port) IN (VALUES {', '.join(values)})"
    union = "\n UNION ".join(
        f"SELECT DISTINCT src_ip, dst_ip, protocol, dst_port, exporter_id FROM {table} WHERE {where} AND {wanted}"
        for table, where in tiers
    )
    res = await db.execute(text(union), {**params, **key_params})
    seen: Dict[tuple, list] = {}
    for r in res.mappings():
        key = (_ip_text(r["src_ip"]), _ip_text(r["dst_ip"]), int(r["protocol"]), int(r["dst_port"]))
        seen.setdefault(key, []).append(r["exporter_id"])
    return seen


# Direction of traffic by the zones rule: inbound and outbound cross the network's edge,
# internal stays inside it, transit has neither end inside (flow_metrics.zone_sql).
DIRECTIONS = {
    "ingress": ZONE_NORTH_SOUTH_INGRESS,
    "egress": ZONE_NORTH_SOUTH_EGRESS,
    "internal": ZONE_EAST_WEST,
    "transit": ZONE_EXTERNAL_TRANSIT,
}


async def direction_totals(
    db: AsyncSession,
    where_sql: str,
    params: Dict[str, Any],
    *,
    tier: Optional[str] = None,
    by_bucket: bool = False,
) -> list[Dict[str, Any]]:
    """Bytes and packets per direction (DIRECTIONS) plus flows, each conversation counted once.

    One row for the window, or one per bucket with `by_bucket` (pass `tier` so every bucket is
    the same length). The overview tiles and the chart's "All flows" mode read this, so
    "inbound" means the same there as in Traffic zones (review P3): it used to mean "towards a
    monitored endpoint", which left users' PCs, not being endpoints, as transit.
    """
    counted_once, params = counted_once_cte(where_sql, params, tier)
    zone = zone_sql("src_ip", "src_endpoint_id", "dst_ip", "dst_endpoint_id")
    measures = ["COALESCE(SUM(flow_count), 0) AS flows"]
    for side, zone_name in DIRECTIONS.items():
        for unit in ("bytes", "packets"):
            measures.append(f"COALESCE(SUM({unit}) FILTER (WHERE zone = '{zone_name}'), 0) AS {side}_{unit}")
    columns = ", ".join((["bucket"] if by_bucket else []) + measures)
    group = "GROUP BY bucket ORDER BY bucket" if by_bucket else ""
    res = await db.execute(
        text(f"""
            WITH {counted_once},
            zoned AS (SELECT bucket, bytes, packets, flow_count, {zone} AS zone FROM counted_once)
            SELECT {columns}
            FROM zoned {group}
        """),
        params,
    )
    return [dict(r) for r in res.mappings()]


async def address_exporters(
    db: AsyncSession, where_sql: str, params: Dict[str, Any], ips: Sequence[str]
) -> Dict[str, list]:
    """{address: [exporter ids]}: the routers that saw each of `ips`, as source or destination,
    in the window. Top talkers places an address that is no endpoint by these routers' sites."""
    if not ips:
        return {}
    tiers, params = _tiers(where_sql, params)
    ip_params = {f"ta{i}": _ip_text(ip) for i, ip in enumerate(ips)}
    wanted = ", ".join(f"CAST(:{k} AS inet)" for k in ip_params)
    union = "\n UNION ".join(
        f"SELECT DISTINCT {side} AS ip, exporter_id FROM {table} WHERE {where} AND {side} IN ({wanted})"
        for table, where in tiers
        for side in ("src_ip", "dst_ip")
    )
    res = await db.execute(text(union), {**params, **ip_params})
    seen: Dict[str, list] = {}
    for r in res.mappings():
        seen.setdefault(_ip_text(r["ip"]), []).append(r["exporter_id"])
    return seen
