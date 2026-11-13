"""The Traffic page's Interfaces table, for one device or across every router in scope.

v3.3.6 build plan 7.5 step 6 (review I1, I2, I3, decided with Kenneth on 2026-11-13). Until then
the table needed a router picked first, its Status column said only whether traffic was seen,
and its "Ingress / Egress rate" were the window's averages, which read like rates now. Now:

- One table across every router the filter picks, or one router's (`device_id`).
- Each interface has "now" (the last five complete minutes, as the Now area) next to the
  window's average / P95 / busiest minute.
- For a router polled over SNMP, its status is the port's own (up, down, shut down), with the
  time it was down within the window (snmp_interface_events). Flow-only routers keep "traffic
  seen" / "idle". Ports an admin shut down are listed (greyed at the bottom on the page).

Traffic comes from SNMP counters where the interface has them, flows otherwise
(services/interface_traffic), the same as every other interface figure.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from uuid import UUID

from app.core.orm import AsyncSession, text
from app.services import interface_traffic, snmp_inventory
from app.services.flow_metrics import (
    bits_per_second,
    configured_speed_mbps,
    interface_alias,
    interface_role,
    percentile_with_idle,
    utilization_pct,
)
from app.services.wan_links import NOW_MINUTES, _SNMP_NOT_ANSWERING, down_spans

# flowd's "unknown exporter": flows from a source not registered yet.
SENTINEL = "00000000-0000-0000-0000-000000000000"

# Up to 7 days the minute table answers; 30 days needs the hourly tier (minutes are kept 7 days).
MINUTE_TIER_UP_TO = datetime.timedelta(days=7)

_STATUS_ORDER_LAST = "shut_down"


@dataclass
class InterfaceRow:
    exporter_id: UUID
    exporter_hostname: str
    site: Optional[str]
    interface_idx: int
    name: str
    alias_data: Any
    speed_mbps: int
    speed_configured: bool
    role: str
    source_info: Dict[str, Any]
    traffic_source: str
    in_bytes: int
    out_bytes: int
    in_packets: int
    out_packets: int
    flow_count: int
    in_bps: float
    out_bps: float
    in_p95_bps: float
    out_p95_bps: float
    in_peak_bps: float
    out_peak_bps: float
    now_in_bps: float
    now_out_bps: float
    utilization_percentage: Optional[float]
    p95_utilization_percentage: Optional[float]
    peak_utilization_percentage: Optional[float]
    is_active: bool
    # "up", "down", "shut_down", "not_answering", "unknown" (SNMP); "traffic_seen" / "idle" (flows).
    status: str
    admin_status: Optional[str]
    covered_seconds: float
    down_spans: List[Tuple[datetime.datetime, Optional[datetime.datetime]]] = field(default_factory=list)
    down_seconds: int = 0


@dataclass
class InterfaceTable:
    rows: List[InterfaceRow]
    window_start: datetime.datetime
    window_end: datetime.datetime
    slot_seconds: int
    hourly: bool
    covered_seconds: float


def _site(value: Any) -> Optional[str]:
    text_value = str(value).strip() if value is not None else ""
    return text_value or None


async def _endpoints(db: AsyncSession, routers: Optional[List[UUID]], seen: Iterable[str]) -> List[Tuple]:
    """(id, hostname, aliases, site) of the routers to list: the ones in scope, or with no filter
    every live endpoint that reported interface traffic in the window, is polled over SNMP or has
    an interface map typed in."""
    if routers is not None:
        ids = tuple(str(r) for r in routers)
        if not ids:
            return []
        res = await db.execute(text("""
            SELECT id, hostname, flow_interface_aliases, site FROM endpoints
            WHERE deleted_at IS NULL AND endpoint_status != 'DELETED' AND id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids})
        return list(res.all())
    seen_ids = tuple(sorted(set(seen))) or (SENTINEL,)
    res = await db.execute(text("""
        SELECT id, hostname, flow_interface_aliases, site FROM endpoints e
        WHERE deleted_at IS NULL AND endpoint_status != 'DELETED'
          AND (id = ANY(CAST(:seen AS uuid[]))
               OR snmp_credential_id IS NOT NULL
               OR EXISTS (SELECT 1 FROM snmp_device_state s WHERE s.endpoint_id = e.id)
               OR (flow_interface_aliases IS NOT NULL AND flow_interface_aliases::text NOT IN ('{}', 'null')))
    """), {"seen": seen_ids})
    return list(res.all())


async def _device_states(db: AsyncSession, ids: Tuple[str, ...]) -> Dict[str, str]:
    if not ids:
        return {}
    try:
        res = await db.execute(text(
            "SELECT endpoint_id, status FROM snmp_device_state WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))"
        ), {"ids": ids})
    except Exception:
        return {}
    return {str(r[0]): str(r[1]) for r in res.all()}


def _status(snmp_row: Optional[Dict[str, Any]], device_status: Optional[str], is_active: bool) -> str:
    """The port's state from SNMP where the router is polled and knows the port, else whether
    flows showed traffic."""
    if snmp_row is not None:
        if device_status in _SNMP_NOT_ANSWERING:
            return "not_answering"
        if snmp_row.get("admin_status") == "down":
            return "shut_down"
        oper = snmp_row.get("oper_status")
        if oper in ("down", "lowerLayerDown"):
            return "down"
        if oper == "up":
            return "up"
        if oper:
            return "unknown"
    return "traffic_seen" if is_active else "idle"


def _order(rows: List[InterfaceRow], single_device: bool) -> List[InterfaceRow]:
    """One device: by index, as before. Across devices: WAN links first (busiest against their
    speed first), then the other interfaces by traffic. Shut-down ports last either way."""
    if single_device:
        return sorted(rows, key=lambda r: (r.status == _STATUS_ORDER_LAST, r.interface_idx))

    def key(r: InterfaceRow):
        shut = r.status == _STATUS_ORDER_LAST
        wan = r.role == "wan"
        util = r.utilization_percentage
        return (
            shut,
            not wan,
            (util is None) if wan else False,
            -(util or 0.0) if wan else 0.0,
            -max(r.in_bps, r.out_bps),
            r.exporter_hostname.lower(),
            r.interface_idx,
        )
    return sorted(rows, key=key)


async def interface_table(
    db: AsyncSession,
    routers: Optional[List[UUID]],
    window: datetime.timedelta,
    now: Optional[datetime.datetime] = None,
    active_indices: Optional[Dict[str, Set[int]]] = None,
    single_device: bool = False,
) -> InterfaceTable:
    """Every interface of `routers` (None: every router with interface data) over `window`.

    `active_indices`: per router, interfaces flowd saw recently (FastStore) though nothing is in
    the rollups yet; they count as "traffic seen"."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start = now - window
    hourly = window > MINUTE_TIER_UP_TO
    slot_seconds = 3600 if hourly else 60
    peak_in_col, peak_out_col = ("peak_in_minute_bytes", "peak_out_minute_bytes") if hourly else ("in_bytes", "out_bytes")
    now_end = now.replace(second=0, microsecond=0)
    now_start = now_end - datetime.timedelta(minutes=NOW_MINUTES)

    params: Dict[str, Any] = {"start": start, "sentinel": SENTINEL}
    if routers is not None:
        ids = tuple(str(r) for r in routers)
        if not ids:
            return InterfaceTable([], start, now, slot_seconds, hourly, 0.0)
        router_clause = "{t}.exporter_id = ANY(CAST(:ids AS uuid[])) AND "
        params["ids"] = ids
    else:
        router_clause = "{t}.exporter_id != CAST(:sentinel AS uuid) AND "
    source = interface_traffic.source_for(hourly, router_clause + "{t}.bucket >= :start")

    res = await db.execute(text(f"""
        -- Every column named: the ORM keys result columns by name.
        SELECT exporter_id, interface_idx,
               SUM(in_bytes) AS in_bytes, SUM(out_bytes) AS out_bytes,
               SUM(in_packets) AS in_packets, SUM(out_packets) AS out_packets, SUM(flow_count) AS flow_count,
               MAX({peak_in_col}) AS peak_in, MAX({peak_out_col}) AS peak_out,
               array_agg(in_bytes ORDER BY in_bytes) AS in_slots, array_agg(out_bytes ORDER BY out_bytes) AS out_slots,
               bool_or(source = 'snmp') AS from_snmp,
               MIN(MIN(bucket)) OVER (PARTITION BY exporter_id) AS first_bucket
        FROM {source} t
        GROUP BY exporter_id, interface_idx
    """), params)
    stats: Dict[Tuple[str, int], Dict[str, Any]] = {}
    first_bucket: Dict[str, datetime.datetime] = {}
    for r in res.all():
        ep = str(r[0])
        stats[(ep, int(r[1]))] = {
            "in_bytes": int(r[2] or 0), "out_bytes": int(r[3] or 0),
            "in_packets": int(r[4] or 0), "out_packets": int(r[5] or 0), "flow_count": int(r[6] or 0),
            "peak_in": int(r[7] or 0), "peak_out": int(r[8] or 0),
            "in_slots": list(r[9] or []), "out_slots": list(r[10] or []), "from_snmp": bool(r[11]),
        }
        if r[12] is not None:
            first_bucket[ep] = r[12]

    # "Now": the last five complete minutes, as the Now area and the overview.
    now_params = {**params, "now_start": now_start, "now_end": now_end}
    now_source = interface_traffic.minute_source(router_clause + "{t}.bucket >= :now_start AND {t}.bucket < :now_end")
    res = await db.execute(text(f"""
        SELECT exporter_id, interface_idx, SUM(in_bytes) AS in_bytes, SUM(out_bytes) AS out_bytes FROM {now_source} t
        GROUP BY exporter_id, interface_idx
    """), now_params)
    now_bytes = {(str(r[0]), int(r[1])): (int(r[2] or 0), int(r[3] or 0)) for r in res.all()}

    endpoints = await _endpoints(db, routers, {ep for ep, _ in stats})
    ids = tuple(str(e[0]) for e in endpoints)
    snmp = await snmp_inventory.load(db, ids)
    device_status = await _device_states(db, ids)
    spans = await down_spans(db, [UUID(i) for i in ids], start, now) if ids else {}
    active_indices = active_indices or {}

    rows: List[InterfaceRow] = []
    for ep_id, hostname, raw_aliases, site in endpoints:
        ep = str(ep_id)
        manual = snmp_inventory._decode(raw_aliases)
        snmp_map = snmp.get(ep, {})
        aliases = snmp_inventory.merge(manual, snmp_map)
        indices: Set[int] = {idx for e, idx in stats if e == ep}
        indices |= {int(k) for k in manual if str(k).lstrip("-").isdigit()}
        for idx, s_row in snmp_map.items():
            # Ports SNMP sees up (a WAN port can be marked before it carries flows), ports an
            # admin shut down (listed, greyed), and ports that went down within the window.
            if s_row.get("oper_status") == "up" or s_row.get("admin_status") == "down" or (UUID(ep), idx) in spans:
                indices.add(idx)
        indices |= set(active_indices.get(ep, set()))

        coverage_start = max(start, first_bucket[ep]) if ep in first_bucket else start
        covered = max(60.0, (now - coverage_start).total_seconds())
        total_slots = int(-(-covered // slot_seconds))
        zero = {"in_bytes": 0, "out_bytes": 0, "in_packets": 0, "out_packets": 0, "flow_count": 0,
                "peak_in": 0, "peak_out": 0, "in_slots": [], "out_slots": [], "from_snmp": False}

        for idx in sorted(indices):
            st = stats.get((ep, idx), zero)
            alias_data = aliases.get(str(idx))
            name, speed = interface_alias(alias_data, f"Interface {idx}")
            speed = max(1, speed)
            speed_configured = configured_speed_mbps(alias_data) is not None
            in_bps = bits_per_second(st["in_bytes"], covered)
            out_bps = bits_per_second(st["out_bytes"], covered)
            in_p95 = bits_per_second(percentile_with_idle(st["in_slots"], total_slots), slot_seconds)
            out_p95 = bits_per_second(percentile_with_idle(st["out_slots"], total_slots), slot_seconds)
            in_peak = bits_per_second(st["peak_in"], 60.0)
            out_peak = bits_per_second(st["peak_out"], 60.0)
            now_in, now_out = now_bytes.get((ep, idx), (0, 0))
            seconds_now = float(NOW_MINUTES * 60)

            def util(a: float, b: float) -> Optional[float]:
                return round(utilization_pct(max(a, b), speed), 2) if speed_configured else None

            is_active = idx in active_indices.get(ep, set()) or st["flow_count"] > 0 or (st["in_bytes"] + st["out_bytes"]) > 0
            snmp_row = snmp_map.get(idx)
            link_spans = spans.get((UUID(ep), idx), [])
            down_total = sum(int(((b or now) - a).total_seconds()) for a, b in link_spans)
            rows.append(InterfaceRow(
                exporter_id=UUID(ep), exporter_hostname=str(hostname), site=_site(site),
                interface_idx=idx, name=name, alias_data=alias_data, speed_mbps=speed,
                speed_configured=speed_configured, role=interface_role(alias_data),
                source_info=snmp_inventory.sources(manual, snmp_row, idx),
                traffic_source="snmp" if st["from_snmp"] else "flow",
                in_bytes=st["in_bytes"], out_bytes=st["out_bytes"],
                in_packets=st["in_packets"], out_packets=st["out_packets"], flow_count=st["flow_count"],
                in_bps=in_bps, out_bps=out_bps, in_p95_bps=in_p95, out_p95_bps=out_p95,
                in_peak_bps=in_peak, out_peak_bps=out_peak,
                now_in_bps=bits_per_second(now_in, seconds_now), now_out_bps=bits_per_second(now_out, seconds_now),
                utilization_percentage=util(in_bps, out_bps),
                p95_utilization_percentage=util(in_p95, out_p95),
                peak_utilization_percentage=util(in_peak, out_peak),
                is_active=is_active,
                status=_status(snmp_row, device_status.get(ep), is_active),
                admin_status=(snmp_row or {}).get("admin_status"),
                covered_seconds=covered,
                down_spans=link_spans,
                down_seconds=down_total,
            ))

    covered_all = max((r.covered_seconds for r in rows), default=max(60.0, window.total_seconds()))
    return InterfaceTable(_order(rows, single_device), start, now, slot_seconds, hourly, covered_all)
