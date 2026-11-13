"""Gathers and formats the data behind the three PDF reports: availability (template A),
bandwidth (B) and master (C, both together). The router renders the result through
core/template.py and core/pdf.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.core.orm import or_, select, text
from app.core.orm import AsyncSession

from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.rca_incident import EndpointRCAIncident
from app.models.system_setting import AppSetting
from app.services import conversations as conversation_queries
from app.services.flow_metrics import (
    ROLE_WAN,
    ZONE_EAST_WEST,
    ZONE_EXTERNAL_TRANSIT,
    ZONE_NORTH_SOUTH_EGRESS,
    ZONE_NORTH_SOUTH_INGRESS,
    bits_per_second,
    configured_speed_mbps,
    dscp_breakdown,
    interface_alias,
    interface_role,
    service_name,
    utilization_pct,
)
from app.services.uptime_calculator import (
    calculate_device_gap_seconds,
    calculate_uptime_denominator_and_percentage,
    get_service_gap_intervals,
    merge_intervals,
)

logger = logging.getLogger(__name__)


def format_bps(bps: float) -> str:
    if not bps or bps <= 0:
        return "0.0 bps"
    if bps >= 1e12:
        return f"{bps / 1e12:.2f} Tbps"
    if bps >= 1e9:
        return f"{bps / 1e9:.2f} Gbps"
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} Mbps"
    if bps >= 1e3:
        return f"{bps / 1e3:.1f} kbps"
    return f"{bps:.0f} bps"


def format_bytes(b: int) -> str:
    if not b or b <= 0:
        return "0 B"
    if b >= 1e12:
        return f"{b / 1e12:.2f} TB"
    if b >= 1e9:
        return f"{b / 1e9:.2f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.1f} MB"
    if b >= 1e3:
        return f"{b / 1e3:.1f} KB"
    return f"{b} B"


def format_duration(seconds: int) -> str:
    """Two most significant units, never skipping one in between: "1d 5h", "21h", "5h 3m",
    "47m 12s", "45s". (Dropping a zero middle unit read "21h 0m 36s" as "21h 36s".)"""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0s"
    units = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    for i, (name, size) in enumerate(units):
        if seconds >= size:
            major, rest = divmod(seconds, size)
            parts = [f"{major}{name}"]
            if i + 1 < len(units):
                minor = rest // units[i + 1][1]
                if minor:
                    parts.append(f"{minor}{units[i + 1][0]}")
            return " ".join(parts)
    return "0s"


def format_period(start_dt: datetime, end_dt: datetime) -> str:
    """The report period: dates for whole days, date and time otherwise (a 3-day window from
    09:30 to 09:30 would otherwise read as four calendar days)."""
    def at_midnight(t: datetime) -> bool:
        return t.time() == datetime.min.time()

    def end_of_day(t: datetime) -> bool:
        return (t.hour, t.minute, t.second) == (23, 59, 59)

    if at_midnight(start_dt) and (at_midnight(end_dt) or end_of_day(end_dt)):
        return f"{start_dt:%Y-%m-%d} to {end_dt:%Y-%m-%d} UTC"
    return f"{start_dt:%Y-%m-%d %H:%M} to {end_dt:%Y-%m-%d %H:%M} UTC"


def format_speed(mbps: int) -> str:
    if not mbps or mbps <= 0:
        return "1 Gbps"
    if mbps >= 1000000:
        return f"{mbps // 1000000} Tbps"
    if mbps >= 1000:
        return f"{mbps // 1000} Gbps"
    return f"{mbps} Mbps"


def format_port_label(port: int, proto: int) -> str:
    """"443 / HTTPS", "443 / QUIC (HTTP/3)", or "1434 / TCP": the names the Traffic page uses
    (flow_metrics.service_name)."""
    p = int(port or 0)
    name = service_name(proto, p)
    if name:
        return f"{p} / {name}"
    proto_name = "TCP" if proto == 6 else ("UDP" if proto == 17 else f"Proto {proto}")
    return f"{p} / {proto_name}"


async def get_organization_branding(db: AsyncSession) -> Dict[str, str]:
    """
    Retrieves corporate branding configurations stored in app_settings.
    """
    stmt = select(AppSetting).where(
        AppSetting.setting_key.in_([
            "org:company_name",
            "org:department",
            "org:logo_data",
            "org:report_footer",
        ])
    )
    res = await db.execute(stmt)
    kv = {s.setting_key: s.setting_value for s in res.scalars().all()}

    return {
        "company_name": kv.get("org:company_name", ""),
        "department": kv.get("org:department", ""),
        "logo_data": kv.get("org:logo_data", ""),
        "report_footer": kv.get("org:report_footer", ""),
    }


async def compile_availability_report_data(
    db: AsyncSession,
    endpoint_ids: List[UUID],
    start_dt: datetime,
    end_dt: datetime,
    include_rca: bool = True,
) -> Dict[str, Any]:
    """
    Compiles objective availability and SLA performance telemetry for Template A.

    Per endpoint: uptime and downtime inside the window (monitoring-gap time counted as
    neither), uptime %, outages (consecutive DOWN periods merged), packet loss over all probes
    and probe-weighted RTT. Then the outage ledger, each outage with the RCA snapshot taken
    for it. Fleet uptime and RTT are plain means over the monitored endpoints.
    """
    now_utc = datetime.now(timezone.utc)
    stmt = (
        select(Endpoint)
        .where(
            Endpoint.id.in_(endpoint_ids),
            Endpoint.deleted_at.is_(None),
        )
        .order_by(Endpoint.hostname.asc())
    )
    res = await db.execute(stmt)
    endpoints = res.scalars().all()

    gap_intervals = await get_service_gap_intervals(db, start_dt, end_dt)

    # Query all events overlapping the window for the targeted endpoints
    events_stmt = (
        select(EndpointEvent)
        .where(
            EndpointEvent.endpoint_id.in_(endpoint_ids),
            EndpointEvent.start_time <= end_dt,
            or_(
                EndpointEvent.end_time.is_(None),
                EndpointEvent.end_time >= start_dt,
            ),
        )
        .order_by(EndpointEvent.start_time.asc())
    )
    ev_res = await db.execute(events_stmt)
    all_events = ev_res.scalars().all()
    grouped_events: Dict[UUID, List[EndpointEvent]] = {}
    for ev in all_events:
        grouped_events.setdefault(ev.endpoint_id, []).append(ev)

    gaps = _merge_intervals(gap_intervals)
    endpoint_rows = []
    total_downtime_sec = 0
    total_incidents = 0
    uptime_sum = 0.0
    outage_list: List[Dict[str, Any]] = []

    for ep in endpoints:
        evs = grouped_events.get(ep.id, [])
        created_at = ep.created_at or start_dt
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        eff_start = max(start_dt, created_at)
        eff_end = min(end_dt, now_utc)

        up_sec = down_sec = 0.0
        probes_ok = probes_failed = 0.0
        rtt_weighted = rtt_weight = 0.0
        for ev in evs:
            ev_end = ev.end_time or now_utc
            seg_start, seg_end = max(eff_start, ev.start_time), min(eff_end, ev_end)
            if seg_end <= seg_start:
                continue
            span = (seg_end - seg_start).total_seconds()
            # Time inside a monitoring gap is unknown: neither up nor down (it is also left out
            # of the uptime denominator). DATABASE_DISCONNECT gaps leave events open across them.
            monitored = span - _overlap_seconds(seg_start, seg_end, gaps)
            if ev.operational_state == "UP":
                up_sec += monitored
            elif ev.operational_state == "DOWN":
                down_sec += monitored
            # Probe counts cover the whole event: take the share that falls in the window.
            length = max((ev_end - ev.start_time).total_seconds(), 1.0)
            share = min(1.0, span / length)
            ok = float(ev.success_count or 0) * share
            probes_ok += ok
            probes_failed += float(ev.failed_count or 0) * share
            if ev.avg_rtt_ms is not None and ok > 0:
                rtt_weighted += float(ev.avg_rtt_ms) * ok
                rtt_weight += ok

        outages = _group_outages(evs, now_utc)
        op_state = evs[-1].operational_state if evs else ("UP" if ep.endpoint_status == "ACTIVE" else "DOWN")
        unknown_sec = calculate_device_gap_seconds(eff_start, eff_end, gaps)
        uptime_pct = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=start_dt,
            end_time=end_dt,
            now_utc=now_utc,
            up_events_count=len(evs),
            unknown_seconds=unknown_sec,
            gap_intervals=gaps,
            uptime_seconds=int(round(up_sec)),
        )
        probes = probes_ok + probes_failed
        loss_pct: Optional[float] = round(100.0 * probes_failed / probes, 2) if probes > 0 else None
        avg_rtt: Optional[float] = round(rtt_weighted / rtt_weight, 2) if rtt_weight > 0 else None

        # PASSIVE means ICMP monitoring is off, whatever the device_role (matches the endpoint list).
        if ep.monitoring_enabled is False:
            op_state = "PASSIVE"
            uptime_pct = 100.0
            outages = []
            down_sec = 0.0
            loss_pct = None
            avg_rtt = None

        down_sec_int = int(round(down_sec))
        total_downtime_sec += down_sec_int
        total_incidents += len(outages)
        if ep.monitoring_enabled is not False:
            uptime_sum += uptime_pct
        for outage in outages:
            outage_list.append({**outage, "endpoint_id": ep.id, "hostname": ep.hostname, "ip_address": ep.ip_address})

        endpoint_rows.append({
            "id": ep.id,
            "hostname": ep.hostname,
            "ip_address": ep.ip_address,
            "device_type": ep.device_type,
            "operational_state": op_state,
            "monitoring_enabled": ep.monitoring_enabled,
            "uptime_percentage": uptime_pct,
            "incident_count": len(outages),
            "formatted_downtime": format_duration(down_sec_int),
            "downtime_seconds": down_sec_int,
            "packet_loss_pct": loss_pct,
            "avg_rtt_ms": avg_rtt,
        })

    # Root-cause snapshots, each matched to the outage whose period contains it.
    rca_by_endpoint: Dict[UUID, List[Any]] = {}
    if include_rca and outage_list:
        earliest = min(o["start"] for o in outage_list) - _RCA_LEAD
        rca_stmt = (
            select(EndpointRCAIncident)
            .where(
                EndpointRCAIncident.endpoint_id.in_(endpoint_ids),
                EndpointRCAIncident.incident_timestamp >= earliest,
                EndpointRCAIncident.incident_timestamp <= end_dt,
            )
            .order_by(EndpointRCAIncident.incident_timestamp.asc())
        )
        rca_res = await db.execute(rca_stmt)
        for rca in rca_res.scalars().all():
            rca_by_endpoint.setdefault(rca.endpoint_id, []).append(rca)

    outage_list.sort(key=lambda o: o["start"], reverse=True)
    incident_rows = []
    for outage in outage_list:
        rca_summary = None
        candidates = rca_by_endpoint.get(outage["endpoint_id"], [])
        for rca in candidates:
            if outage["start"] - _RCA_LEAD <= rca.incident_timestamp <= (outage["end"] or now_utc):
                rca_summary = rca.rca_summary
                candidates.remove(rca)
                break
        incident_rows.append({
            "incident_id": outage["incident_id"],
            "hostname": outage["hostname"],
            "ip_address": outage["ip_address"],
            "start_time": outage["start"].strftime("%Y-%m-%d %H:%M:%S"),
            "resolved_time": outage["end"].strftime("%Y-%m-%d %H:%M:%S") if outage["end"] else "Active / Ongoing",
            "duration": format_duration(outage["duration_seconds"]),
            "trigger_reason": "Ping packet loss exceeded failure threshold",
            "rca_summary": rca_summary,
        })

    monitored_rows = [
        r for r in endpoint_rows
        if r.get("monitoring_enabled", True) is not False
    ]
    num_monitored = len(monitored_rows)
    mean_uptime = round(uptime_sum / num_monitored, 3) if num_monitored else 100.0
    rtt_rows = [r["avg_rtt_ms"] for r in monitored_rows if r["avg_rtt_ms"] is not None]
    avg_rtt_group = round(sum(rtt_rows) / len(rtt_rows), 2) if rtt_rows else None

    return {
        "endpoints": endpoint_rows,
        # The ledger lists the most recent outages; incidents_total says how many there were.
        "incidents": incident_rows[:INCIDENT_LEDGER_LIMIT],
        "incidents_total": len(incident_rows),
        "summary": {
            "mean_uptime": f"{mean_uptime:.3f}",
            "total_incidents": total_incidents,
            "total_downtime": format_duration(total_downtime_sec),
            "avg_rtt": f"{avg_rtt_group:.2f}" if avg_rtt_group is not None else "—",
        },
    }


INCIDENT_LEDGER_LIMIT = 100
# An RCA trace runs once an outage is confirmed; allow for clock skew before the recorded start.
_RCA_LEAD = timedelta(minutes=5)


# Shared with the endpoint list (services/uptime_calculator.merge_intervals).
_merge_intervals = merge_intervals


def _overlap_seconds(start: datetime, end: datetime, intervals: List[Tuple[datetime, datetime]]) -> float:
    return sum(
        max(0.0, (min(end, g_end) - max(start, g_start)).total_seconds()) for g_start, g_end in intervals
    )


def _group_outages(evs: List[Any], now_utc: datetime) -> List[Dict[str, Any]]:
    """An endpoint's outages from its event rows (sorted by start). Consecutive DOWN rows are one
    outage until an UP row ends it: the engine splits a period every 24 h and closes and reopens
    events around a monitoring gap, but the device stayed down. A split row keeps the outage's
    real start in state_since."""
    outages: List[Dict[str, Any]] = []
    current: Optional[List[Any]] = None
    for ev in evs:
        if ev.operational_state == "DOWN":
            if current is None:
                current = [ev]
                outages.append({"rows": current})
            else:
                current.append(ev)
        else:
            current = None
    result = []
    for outage in outages:
        first, last = outage["rows"][0], outage["rows"][-1]
        start = first.start_time
        since = getattr(first, "state_since", None)
        if first.is_split_event and since is not None and since < start:
            start = since
        end = last.end_time
        result.append({
            "incident_id": str(first.id)[:8].upper(),
            "start": start,
            "end": end,
            "duration_seconds": max(0, int(((end or now_utc) - start).total_seconds())),
        })
    return result


# flow_interface_minute_rollups is kept 7 days. A report reaching further back reads the hourly
# rollup (kept 365 days), which also stores each hour's busiest minute, so peaks stay per-minute.
_INTERFACE_MINUTE_RETENTION = timedelta(days=7)


async def _interface_totals(
    db: AsyncSession,
    exporter_ids: List[UUID],
    start_dt: datetime,
    end_dt: datetime,
) -> List[Any]:
    """(exporter_id, interface_idx, in_bytes, out_bytes, busiest-minute in, busiest-minute out)
    per interface over [start_dt, end_dt].

    Reading only the minute table, a 30-day report summed the last week of traffic and then
    averaged it over 30 days. On the hourly tier an hour that straddles the start or end counts
    whole, which matters little at that length.
    """
    if not exporter_ids:
        return []
    hourly = start_dt < datetime.now(timezone.utc) - _INTERFACE_MINUTE_RETENTION
    if not hourly:
        peak_in, peak_out, start = "in_bytes", "out_bytes", "{t}.bucket >= :start"
    else:
        peak_in, peak_out, start = (
            "peak_in_minute_bytes", "peak_out_minute_bytes",
            "{t}.bucket > CAST(:start AS timestamptz) - INTERVAL '1 hour'",
        )
    # One bind parameter per exporter: list parameters are not passed through as arrays.
    params: Dict[str, Any] = {f"exporter{i}": str(e) for i, e in enumerate(exporter_ids)}
    exporters = ", ".join(f"CAST(:{name} AS uuid)" for name in params)
    # SNMP counters where the interface has them, flows otherwise (v3.3.6).
    from app.services import interface_traffic
    source = interface_traffic.source_for(hourly, f"{{t}}.exporter_id IN ({exporters}) AND {start} AND {{t}}.bucket <= :end")
    res = await db.execute(
        text(f"""
            SELECT exporter_id, interface_idx,
                   SUM(in_bytes) AS tot_in_bytes, SUM(out_bytes) AS tot_out_bytes,
                   MAX({peak_in}) AS max_in_bytes, MAX({peak_out}) AS max_out_bytes
            FROM {source} t
            GROUP BY exporter_id, interface_idx
            ORDER BY exporter_id, interface_idx
        """),
        {**params, "start": start_dt, "end": end_dt},
    )
    return res.all()


# How long the tables behind the bandwidth report keep their data (migrations 0021 and 0016).
_CONVERSATION_ROLLUP_RETENTION = timedelta(days=90)
_INTERFACE_HOURLY_RETENTION = timedelta(days=365)


def flow_coverage_notes(start_dt: datetime, now: Optional[datetime] = None) -> List[str]:
    """What a range reaching past the flow tables' retention leaves out, for the report to say.
    Custom ranges can span 730 days, and the sections would otherwise look like a quiet period."""
    now = now or datetime.now(timezone.utc)
    notes = []
    for kept, what in (
        (_CONVERSATION_ROLLUP_RETENTION, "Top conversations, QoS, anomalies and traffic zones"),
        (_INTERFACE_HOURLY_RETENTION, "Router and interface totals"),
    ):
        if start_dt < now - kept:
            notes.append(f"{what} cover {(now - kept):%Y-%m-%d} onward: this data is kept {kept.days} days.")
    return notes


async def compile_bandwidth_report_data(
    db: AsyncSession,
    exporter_ids: List[UUID],
    start_dt: datetime,
    end_dt: datetime,
) -> Dict[str, Any]:
    """
    Compiles multi-router flow bandwidth, interface utilization, and top talkers for Template B.
    """
    stmt = (
        select(Endpoint)
        .where(
            Endpoint.id.in_(exporter_ids),
            Endpoint.deleted_at.is_(None),
        )
        .order_by(Endpoint.hostname.asc())
    )
    res = await db.execute(stmt)
    routers = res.scalars().all()
    router_map = {r.id: r for r in routers}
    # Interface names and speeds as shown elsewhere: manual, else SNMP's (v3.3.6).
    from app.services import snmp_inventory
    router_aliases = await snmp_inventory.merged_for(
        db, {r.id: getattr(r, "flow_interface_aliases", {}) or {} for r in routers})

    # 1. Interface Metrics Query
    raw_interfaces = await _interface_totals(db, exporter_ids, start_dt, end_dt)

    total_window_sec = max(60, int((end_dt - start_dt).total_seconds()))

    interface_rows = []
    exporter_stats = {r.id: {"in_bytes": 0, "out_bytes": 0, "peak_bps": 0, "if_count": 0} for r in routers}
    # Network edge: traffic on WAN-role interfaces, counted once however many routers it crossed.
    wan = {"count": 0, "in_bytes": 0, "out_bytes": 0}

    for exp_id, if_idx, in_b, out_b, max_in_b, max_out_b in raw_interfaces:
        router = router_map.get(exp_id)
        if not router:
            continue

        in_bytes = int(in_b or 0)
        out_bytes = int(out_b or 0)
        peak_b = max(int(max_in_b or 0), int(max_out_b or 0))
        peak_rate_bps = bits_per_second(peak_b, 60.0)  # minute rollup
        avg_in_bps = bits_per_second(in_bytes, total_window_sec)
        avg_out_bps = bits_per_second(out_bytes, total_window_sec)

        aliases = router_aliases.get(str(exp_id), {})
        alias_entry = aliases.get(str(if_idx), {})
        if_name, speed_mbps = interface_alias(alias_entry, "(no alias)")
        speed_configured = configured_speed_mbps(alias_entry) is not None
        # Without a configured link speed utilization would be measured against a guess.
        util_pct = utilization_pct(peak_rate_bps, speed_mbps) if speed_configured else None
        is_wan = interface_role(alias_entry) == ROLE_WAN
        if is_wan:
            wan["count"] += 1
            wan["in_bytes"] += in_bytes
            wan["out_bytes"] += out_bytes

        exporter_stats[exp_id]["in_bytes"] += in_bytes
        exporter_stats[exp_id]["out_bytes"] += out_bytes
        exporter_stats[exp_id]["peak_bps"] = max(exporter_stats[exp_id]["peak_bps"], peak_rate_bps)
        exporter_stats[exp_id]["if_count"] += 1

        interface_rows.append({
            "router_hostname": router.hostname,
            "interface_idx": if_idx,
            "interface_name": if_name,
            "speed_mbps": speed_mbps,
            "formatted_speed": format_speed(speed_mbps) if speed_configured else "Not set",
            "speed_configured": speed_configured,
            "formatted_in_bps": format_bps(avg_in_bps),
            "formatted_out_bps": format_bps(avg_out_bps),
            "formatted_peak_bps": format_bps(peak_rate_bps),
            "utilization_pct": util_pct,
            "is_wan": is_wan,
        })

    exporter_rows = []
    for r in routers:
        stats = exporter_stats.get(r.id, {"in_bytes": 0, "out_bytes": 0, "peak_bps": 0, "if_count": 0})
        mean_in_rate = bits_per_second(stats["in_bytes"], total_window_sec)
        exporter_rows.append({
            "hostname": r.hostname,
            "primary_ip": r.ip_address,
            "active_interfaces_count": stats["if_count"],
            "formatted_ingress_bytes": format_bytes(stats["in_bytes"]),
            "formatted_egress_bytes": format_bytes(stats["out_bytes"]),
            "formatted_ingress_bps": format_bps(mean_in_rate),
            "formatted_peak_bps": format_bps(stats["peak_bps"]),
        })

    # Conversations, QoS and zones read the same rows as the Traffic page
    # (conversations.traffic_breakdown): each conversation counted once, however many of the
    # selected routers reported it. Over a long range they read the hourly conversation tier
    # (kept 90 days), where an hour straddling the end counts whole.
    conv_where, conv_params = "FALSE", {"start": start_dt, "end": end_dt}
    if exporter_ids:
        exporter_params = {f"exp{i}": str(e) for i, e in enumerate(exporter_ids)}
        conv_where = (
            f"{conversation_queries.START_CLAUSE} AND bucket <= :end AND exporter_id IN "
            f"({', '.join(f'CAST(:{k} AS uuid)' for k in exporter_params)})"
        )
        conv_params.update(exporter_params)
    breakdown = {"applications": [], "qos": [], "zones": {}, "total_bytes": 0}
    raw_convs: List[Dict[str, Any]] = []
    seen_by: Dict[tuple, list] = {}
    try:
        breakdown = await conversation_queries.traffic_breakdown(db, conv_where, dict(conv_params))
        page = await conversation_queries.top_conversation_rows(db, conv_where, dict(conv_params), limit=10)
        raw_convs = page
        seen_by = await conversation_queries.conversation_exporters(
            db, conv_where, dict(conv_params),
            [(c["src_ip"], c["dst_ip"], c["protocol"], c["dst_port"]) for c in raw_convs],
        )
    except Exception as exc:
        logger.debug("Failed or skipped the conversation breakdown in report compiler: %s", exc)

    # 2. Top conversations. Each one's share is of all traffic the selected routers saw in
    # the window (not of the top ten, which would overstate every share).
    total_transit_bytes = breakdown["total_bytes"] or sum(int(c["total_bytes"] or 0) for c in raw_convs) or 1
    conversation_rows = []
    for c in raw_convs:
        c_bytes = int(c["total_bytes"] or 0)
        key = (str(c["src_ip"]).split("/")[0], str(c["dst_ip"]).split("/")[0], int(c["protocol"]), int(c["dst_port"]))
        names = sorted(router_map[e].hostname for e in seen_by.get(key, []) if e in router_map)
        conversation_rows.append({
            "src_display": str(c["src_ip"]).split("/")[0] or "Unknown",
            "dst_display": str(c["dst_ip"]).split("/")[0] or "Unknown",
            "port_label": format_port_label(c["dst_port"], c["protocol"]),
            # Every selected router that saw it; the bytes are counted once.
            "router_name": ", ".join(names) or "Unknown",
            "formatted_bytes": format_bytes(c_bytes),
            "pct_of_transit": (c_bytes / total_transit_bytes) * 100.0,
        })

    # 3. DSCP QoS distribution (RFC 2474 / RFC 3246): dscp = (tos & 0xFF) >> 2.
    qos_rows = []
    for item in dscp_breakdown(breakdown["qos"]):
        qos_rows.append({
            "dscp": item["dscp"],
            "dscp_label": item["dscp_label"],
            "total_bytes": item["total_bytes"],
            "formatted_bytes": format_bytes(item["total_bytes"]),
            "total_packets": item["total_packets"],
            "formatted_packets": f"{item['total_packets']:,}",
            "flow_count": item["flow_count"],
            "formatted_flows": f"{item['flow_count']:,}",
            "percentage": item["percentage"],
        })

    # 4. Security Anomaly Telemetry (RFC 793 TCP Control Bits Analysis)
    # Same per-conversation rule as the dashboard: a conversation is a SYN scan / probe when the
    # OR of its TCP flags over the period is SYN without ACK, and a reset when RST without FIN.
    syn_scan_count = syn_scan_bytes = rst_burst_count = rst_burst_bytes = 0
    syn_flows = rst_flows = 0
    if exporter_ids:
        exporter_params = {f"exp{i}": str(e) for i, e in enumerate(exporter_ids)}
        where_sql = (
            f"exporter_id IN ({', '.join(f'CAST(:{k} AS uuid)' for k in exporter_params)}) "
            "AND bucket >= :start AND bucket <= :end"
        )
        try:
            summary = await conversation_queries.anomaly_summary(
                db, where_sql, {**exporter_params, "start": start_dt, "end": end_dt}
            )
            # Counts are conversations (source, destination, port) matching the rule; flows are
            # the individual flow records within them.
            syn_scan_count, syn_flows, syn_scan_bytes = (
                summary["syn_conversations"], summary["syn_flows"], summary["syn_bytes"]
            )
            rst_burst_count, rst_flows, rst_burst_bytes = (
                summary["rst_conversations"], summary["rst_flows"], summary["rst_bytes"]
            )
        except Exception as exc:
            logger.debug("Failed or skipped Anomaly rollup in report compiler: %s", exc)

    total_anomaly_count = syn_scan_count + rst_burst_count
    anomaly_summary = {
        "total_count": total_anomaly_count,
        "syn_scan_count": syn_scan_count,
        "syn_scan_flows": f"{syn_flows:,}",
        "syn_scan_bytes": format_bytes(syn_scan_bytes),
        "rst_burst_count": rst_burst_count,
        "rst_burst_flows": f"{rst_flows:,}",
        "rst_burst_bytes": format_bytes(rst_burst_bytes),
        "total_flows": f"{syn_flows + rst_flows:,}",
        "has_anomalies": total_anomaly_count > 0,
    }

    # 5. Traffic zones, by the Traffic page's rule (flow_metrics.zone_sql): internal means a
    # monitored endpoint or a private address.
    zone_desc = {
        ZONE_EAST_WEST: "Internal Mesh / Host-to-Host",
        ZONE_NORTH_SOUTH_EGRESS: "Internal to External Transit",
        ZONE_NORTH_SOUTH_INGRESS: "External to Internal Transit",
        ZONE_EXTERNAL_TRANSIT: "Perimeter Cross-Link / Transit",
    }
    zone_sums = {name: breakdown["zones"].get(name, {"bytes": 0, "flows": 0}) for name in zone_desc}
    total_zone_bytes = sum(z["bytes"] for z in zone_sums.values())
    zone_rows = [
        {
            "zone": z_name,
            "description": zone_desc[z_name],
            "total_bytes": z_data["bytes"],
            "formatted_bytes": format_bytes(z_data["bytes"]),
            "flow_count": z_data["flows"],
            "formatted_flows": f"{z_data['flows']:,}",
            "percentage": (z_data["bytes"] / total_zone_bytes * 100.0) if total_zone_bytes > 0 else 0.0,
        }
        for z_name, z_data in zone_sums.items()
    ]

    wan_edge = {
        "interfaces_count": wan["count"],
        "formatted_in_bytes": format_bytes(wan["in_bytes"]),
        "formatted_out_bytes": format_bytes(wan["out_bytes"]),
        "formatted_in_bps": format_bps(bits_per_second(wan["in_bytes"], total_window_sec)),
        "formatted_out_bps": format_bps(bits_per_second(wan["out_bytes"], total_window_sec)),
    }

    return {
        "exporters": exporter_rows,
        # Selected devices that actually exported flows in the window (a master report passes
        # every selected endpoint here, most of which export nothing).
        "active_exporters": sum(1 for stats in exporter_stats.values() if stats["if_count"] > 0),
        "interfaces": interface_rows,
        "wan_edge": wan_edge,
        "conversations": conversation_rows,
        "qos_classes": qos_rows,
        "anomalies": anomaly_summary,
        "traffic_zones": zone_rows,
        "coverage_notes": flow_coverage_notes(start_dt),
    }


async def compile_master_report_data(
    db: AsyncSession,
    endpoint_ids: List[UUID],
    start_dt: datetime,
    end_dt: datetime,
    include_rca: bool = True,
) -> Dict[str, Any]:
    """
    Compiles combined Master Operational Audit Dossier for Template C.
    """
    avail_data = await compile_availability_report_data(db, endpoint_ids, start_dt, end_dt, include_rca=include_rca)
    bandwidth_data = await compile_bandwidth_report_data(db, endpoint_ids, start_dt, end_dt)

    return {
        "endpoints": avail_data["endpoints"],
        "incidents": avail_data["incidents"],
        "incidents_total": avail_data.get("incidents_total", len(avail_data["incidents"])),
        "summary": avail_data["summary"],
        "exporters": bandwidth_data["exporters"],
        "active_exporters": bandwidth_data.get("active_exporters", len(bandwidth_data["exporters"])),
        "interfaces": bandwidth_data["interfaces"],
        "wan_edge": bandwidth_data.get("wan_edge"),
        "conversations": bandwidth_data["conversations"],
        "qos_classes": bandwidth_data.get("qos_classes", []),
        "anomalies": bandwidth_data.get("anomalies", {}),
        "traffic_zones": bandwidth_data.get("traffic_zones", []),
        "coverage_notes": bandwidth_data.get("coverage_notes", []),
    }
