"""Server-side conversation queries over flow_minute_rollups.

A conversation is (src_ip, dst_ip, protocol, service port) over the window. Filters, the quick
triage filters and their counts are evaluated in SQL across every matching conversation, so the
UI no longer filters a 25-row sample.
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, Optional, Sequence
from uuid import UUID

from app.core.orm import AsyncSession, text
from app.schemas.bandwidth import (
    TopConversationItem,
    derive_traffic_profile,
    format_dscp_label,
    format_tcp_flags,
)
from app.services.flow_metrics import (
    BULK_MIN_AVG_PACKET_BYTES,
    PROTOCOL_MAP,
    dscp_from_tos,
    tcp_anomaly_sql,
)

QUICK_FILTERS = ("all", "anomalies", "qos", "bulk")
SORT_COLUMNS = {"bytes": "total_bytes", "flows": "total_flows", "packets": "total_packets"}

_QUICK_WHERE = {
    "all": "TRUE",
    "anomalies": "anomaly_hint IS NOT NULL",
    "qos": "dscp > 0",
    "bulk": f"avg_packet_size >= {BULK_MIN_AVG_PACKET_BYTES}",
}


def per_minute_cte(where_sql: str) -> str:
    """`per_minute`: the matching flow_minute_rollups rows with each conversation counted once
    per minute. Traffic crossing two exporting routers is reported by both; summing the reports
    showed it twice, so only the largest one is kept."""
    return f"""
        per_minute AS (
            SELECT bucket, src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, protocol, dst_port,
                   MAX(bytes) AS bytes,
                   MAX(flow_count) AS flow_count,
                   MAX(COALESCE(packets, 0)) AS packets,
                   BIT_OR(tcp_flags) AS tcp_flags,
                   MAX(tos) AS tos
            FROM flow_minute_rollups
            WHERE {where_sql}
            GROUP BY bucket, src_ip, dst_ip, src_endpoint_id, dst_endpoint_id, protocol, dst_port
        )
    """


def _conversation_cte(where_sql: str) -> str:
    return f"""
        WITH {per_minute_cte(where_sql)},
        conv AS (
            SELECT src_ip, dst_ip, protocol, dst_port,
                   SUM(bytes) AS total_bytes,
                   SUM(flow_count) AS total_flows,
                   SUM(packets) AS total_packets,
                   BIT_OR(tcp_flags) AS tcp_flags_mask,
                   MAX(tos) AS max_tos
            FROM per_minute
            GROUP BY src_ip, dst_ip, protocol, dst_port
        ),
        labelled AS (
            SELECT conv.*,
                   {tcp_anomaly_sql("tcp_flags_mask", "protocol")} AS anomaly_hint,
                   (COALESCE(max_tos, 0) & 255) >> 2 AS dscp,
                   COALESCE(total_bytes / NULLIF(total_packets, 0), 0) AS avg_packet_size
            FROM conv
        )
    """


def build_filters(
    start: datetime.datetime,
    exporter_id: Optional[UUID] = None,
    endpoint_id: Optional[UUID] = None,
    ip: Optional[str] = None,
    port: Optional[int] = None,
) -> tuple[str, Dict[str, Any]]:
    clauses = ["bucket >= :start"]
    params: Dict[str, Any] = {"start": start}
    if exporter_id is not None:
        clauses.append("exporter_id = CAST(:exporter_id AS uuid)")
        params["exporter_id"] = str(exporter_id)
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


def to_item(row: Dict[str, Any], hostnames: Dict[str, str]) -> TopConversationItem:
    proto_id = int(row["protocol"])
    proto_name = PROTOCOL_MAP.get(proto_id, f"PROTO-{proto_id}")
    flags = int(row["tcp_flags_mask"] or 0)
    tos = int(row["max_tos"] or 0)
    total_bytes = int(row["total_bytes"] or 0)
    total_flows = int(row["total_flows"] or 0)
    total_packets = int(row["total_packets"] or 0)
    avg_packet_size = int(total_bytes // total_packets) if total_packets > 0 else 0
    packets_per_flow = round(total_packets / total_flows, 1) if total_flows > 0 else 0.0
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
        traffic_profile=derive_traffic_profile(anomaly, avg_packet_size, packets_per_flow, dscp, port),
        tcp_flags=flags,
        tcp_flags_labels=format_tcp_flags(flags) if proto_name == "TCP" else [],
        tos=tos,
        dscp=dscp,
        dscp_label=format_dscp_label(tos),
        anomaly_hint=anomaly,
    )


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
) -> Dict[str, Any]:
    """One page of conversations plus quick-filter counts over every matching conversation."""
    where_sql, params = build_filters(start, exporter_id, endpoint_id, ip, port)
    post = "TRUE"
    if dscp is not None:
        post = "dscp = :dscp"
        params["dscp"] = dscp
    cte = _conversation_cte(where_sql)

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
    return {
        "items": [to_item(r, hostnames) for r in rows],
        "counts": counts,
        "total": counts.get(quick, counts["all"]),
    }


async def anomaly_summary(db: AsyncSession, where_sql: str, params: Dict[str, Any]) -> Dict[str, int]:
    """Flows and conversations classified anomalous by the shared rule over a filtered window."""
    res = await db.execute(
        text(f"""
            {_conversation_cte(where_sql)}
            SELECT COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint IS NOT NULL), 0) AS flows,
                   COUNT(*) FILTER (WHERE anomaly_hint IS NOT NULL) AS conversations,
                   COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint = 'SYN_ONLY'), 0) AS syn_flows,
                   COALESCE(SUM(total_bytes) FILTER (WHERE anomaly_hint = 'SYN_ONLY'), 0) AS syn_bytes,
                   COALESCE(SUM(total_flows) FILTER (WHERE anomaly_hint = 'TCP_RST'), 0) AS rst_flows,
                   COALESCE(SUM(total_bytes) FILTER (WHERE anomaly_hint = 'TCP_RST'), 0) AS rst_bytes
            FROM labelled
        """),
        params,
    )
    row = dict(res.mappings().first() or {})
    return {k: int(row.get(k) or 0) for k in ("flows", "conversations", "syn_flows", "syn_bytes", "rst_flows", "rst_bytes")}

