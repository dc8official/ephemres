from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.orm import and_, desc, func, or_, select
from app.core.orm import AsyncSession

from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.flow_rollup import FlowHourlyRollup, FlowInterfaceMinuteRollup, FlowMinuteRollup
from app.models.rca_incident import EndpointRCAIncident
from app.models.system_setting import AppSetting
from app.repositories.endpoint_repo import EndpointRepository
from app.schemas.bandwidth import format_dscp_label
from app.services.uptime_calculator import (
    calculate_device_gap_seconds,
    calculate_uptime_denominator_and_percentage,
    get_service_gap_intervals,
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
    if not seconds or seconds <= 0:
        return "0s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def format_speed(mbps: int) -> str:
    if not mbps or mbps <= 0:
        return "1 Gbps"
    if mbps >= 1000000:
        return f"{mbps // 1000000} Tbps"
    if mbps >= 1000:
        return f"{mbps // 1000} Gbps"
    return f"{mbps} Mbps"


def format_port_label(port: int, proto: int) -> str:
    p = int(port or 0)
    proto_name = "TCP" if proto == 6 else ("UDP" if proto == 17 else f"Proto {proto}")
    if p == 443:
        return "443 / HTTPS"
    if p == 80:
        return "80 / HTTP"
    if p == 53:
        return "53 / DNS"
    if p == 22:
        return "22 / SSH"
    if p == 123:
        return "123 / NTP"
    if p == 161:
        return "161 / SNMP"
    if p == 2049:
        return "2049 / NFS"
    if p in (2055, 4739):
        return f"{p} / Flow"
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

    endpoint_rows = []
    total_downtime_sec = 0
    total_incidents = 0
    uptime_sum = 0.0

    for ep in endpoints:
        evs = grouped_events.get(ep.id, [])
        up_count = sum(1 for ev in evs if ev.operational_state == "UP")
        down_count = sum(1 for ev in evs if ev.operational_state == "DOWN")
        inc_count = down_count
        down_sec = 0
        up_sec = 0

        for ev in evs:
            ev_start = ev.start_time
            ev_end = ev.end_time or now_utc
            dur = max(0, int((min(end_dt, ev_end) - max(start_dt, ev_start)).total_seconds()))
            if dur == 0 and getattr(ev, "duration_seconds", 0) > 0:
                dur = int(ev.duration_seconds)
            if ev.operational_state == "UP":
                up_sec += dur
            elif ev.operational_state == "DOWN":
                down_sec += dur

        op_state = evs[-1].operational_state if evs else ("UP" if ep.endpoint_status == "ACTIVE" else "DOWN")
        
        created_at = ep.created_at or start_dt
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)

        eff_start = max(start_dt, created_at)
        eff_end = min(end_dt, now_utc)

        unknown_sec = calculate_device_gap_seconds(eff_start, eff_end, gap_intervals)

        uptime_pct = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=start_dt,
            end_time=end_dt,
            now_utc=now_utc,
            up_events_count=up_count,
            unknown_seconds=unknown_sec,
            gap_intervals=gap_intervals,
            uptime_seconds=up_sec,
        )

        role = getattr(ep, "device_role", "ACTIVE_HOST")
        if role == "FLOW_EXPORTER":
            op_state = "PASSIVE"
            uptime_pct = 100.0
            inc_count = 0
            down_sec = 0

        rtt_vals = [float(ev.avg_rtt_ms) for ev in evs if ev.avg_rtt_ms is not None]
        avg_rtt = round(sum(rtt_vals) / len(rtt_vals), 2) if rtt_vals else 0.0

        scores = [float(ev.health_score) for ev in evs if ev.health_score is not None]
        loss_pct = round(100.0 - (sum(scores) / len(scores)), 2) if scores else 0.0
        if loss_pct < 0.0:
            loss_pct = 0.0

        total_downtime_sec += down_sec
        total_incidents += inc_count
        uptime_sum += uptime_pct

        endpoint_rows.append({
            "id": ep.id,
            "hostname": ep.hostname,
            "ip_address": ep.ip_address,
            "device_type": ep.device_type,
            "operational_state": op_state,
            "uptime_percentage": uptime_pct,
            "incident_count": inc_count,
            "formatted_downtime": format_duration(down_sec),
            "downtime_seconds": down_sec,
            "packet_loss_pct": loss_pct,
            "avg_rtt_ms": avg_rtt,
        })

    # Query Recorded Outage Incidents
    incidents_stmt = (
        select(EndpointEvent, Endpoint.hostname, Endpoint.ip_address)
        .join(Endpoint, EndpointEvent.endpoint_id == Endpoint.id)
        .where(
            EndpointEvent.endpoint_id.in_(endpoint_ids),
            EndpointEvent.start_time >= start_dt,
            EndpointEvent.start_time <= end_dt,
            EndpointEvent.operational_state == "DOWN",
        )
        .order_by(EndpointEvent.start_time.desc())
        .limit(50)
    )
    inc_res = await db.execute(incidents_stmt)
    raw_incidents = inc_res.all()

    # If RCA requested, fetch root cause snapshots
    rca_map = {}
    if include_rca and endpoint_ids:
        rca_stmt = (
            select(EndpointRCAIncident)
            .where(
                EndpointRCAIncident.endpoint_id.in_(endpoint_ids),
                EndpointRCAIncident.incident_timestamp >= start_dt,
                EndpointRCAIncident.incident_timestamp <= end_dt,
            )
        )
        rca_res = await db.execute(rca_stmt)
        for rca in rca_res.scalars().all():
            rca_map[rca.endpoint_id] = rca.rca_summary

    incident_rows = []
    for ev, hname, ip in raw_incidents:
        dur = int(ev.duration_seconds or 0)
        resolved_str = ev.end_time.strftime("%Y-%m-%d %H:%M:%S") if ev.end_time else "Active / Ongoing"
        start_str = ev.start_time.strftime("%Y-%m-%d %H:%M:%S")
        inc_id = str(ev.id)[:8].upper()

        trigger = getattr(ev, "trigger_reason", None) or "Ping packet loss exceeded failure threshold"
        rca_summary = rca_map.get(ev.endpoint_id)

        incident_rows.append({
            "incident_id": inc_id,
            "hostname": hname,
            "ip_address": ip,
            "start_time": start_str,
            "resolved_time": resolved_str,
            "duration": format_duration(dur),
            "trigger_reason": trigger,
            "rca_summary": rca_summary,
        })

    num_eps = len(endpoints) or 1
    mean_uptime = round(uptime_sum / num_eps, 3)
    avg_rtt_group = round(sum(r["avg_rtt_ms"] for r in endpoint_rows) / num_eps, 2)

    return {
        "endpoints": endpoint_rows,
        "incidents": incident_rows,
        "summary": {
            "mean_uptime": f"{mean_uptime:.3f}",
            "total_incidents": total_incidents,
            "total_downtime": format_duration(total_downtime_sec),
            "avg_rtt": f"{avg_rtt_group:.2f}",
        },
    }


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

    # 1. Interface Metrics Query
    if_stmt = (
        select(
            FlowInterfaceMinuteRollup.exporter_id,
            FlowInterfaceMinuteRollup.interface_idx,
            func.sum(FlowInterfaceMinuteRollup.in_bytes).label("tot_in_bytes"),
            func.sum(FlowInterfaceMinuteRollup.out_bytes).label("tot_out_bytes"),
            func.max(FlowInterfaceMinuteRollup.in_bytes).label("max_in_bytes"),
            func.max(FlowInterfaceMinuteRollup.out_bytes).label("max_out_bytes"),
        )
        .where(
            FlowInterfaceMinuteRollup.exporter_id.in_(exporter_ids),
            FlowInterfaceMinuteRollup.bucket >= start_dt,
            FlowInterfaceMinuteRollup.bucket <= end_dt,
        )
        .group_by(
            FlowInterfaceMinuteRollup.exporter_id,
            FlowInterfaceMinuteRollup.interface_idx,
        )
        .order_by(FlowInterfaceMinuteRollup.exporter_id, FlowInterfaceMinuteRollup.interface_idx)
    )
    if_res = await db.execute(if_stmt)
    raw_interfaces = if_res.all()

    total_window_sec = max(60, int((end_dt - start_dt).total_seconds()))

    interface_rows = []
    exporter_stats = {r.id: {"in_bytes": 0, "out_bytes": 0, "peak_bps": 0, "if_count": 0} for r in routers}

    for exp_id, if_idx, in_b, out_b, max_in_b, max_out_b in raw_interfaces:
        router = router_map.get(exp_id)
        if not router:
            continue

        in_bytes = int(in_b or 0)
        out_bytes = int(out_b or 0)
        peak_b = max(int(max_in_b or 0), int(max_out_b or 0))
        peak_rate_bps = (peak_b * 8.0) / 60.0  # minute rollup
        avg_in_bps = (in_bytes * 8.0) / total_window_sec
        avg_out_bps = (out_bytes * 8.0) / total_window_sec

        aliases = getattr(router, "flow_interface_aliases", {}) or {}
        alias_entry = aliases.get(str(if_idx), {})
        if isinstance(alias_entry, str):
            if_name = alias_entry
            speed_mbps = 1000
        elif isinstance(alias_entry, dict):
            if_name = alias_entry.get("name", f"Interface #{if_idx}")
            speed_mbps = int(alias_entry.get("speed_mbps", 1000) or 1000)
        else:
            if_name = f"Interface #{if_idx}"
            speed_mbps = 1000

        speed_bps = speed_mbps * 1e6
        util_pct = min(100.0, (peak_rate_bps / speed_bps * 100.0) if speed_bps > 0 else 0.0)

        exporter_stats[exp_id]["in_bytes"] += in_bytes
        exporter_stats[exp_id]["out_bytes"] += out_bytes
        exporter_stats[exp_id]["peak_bps"] = max(exporter_stats[exp_id]["peak_bps"], peak_rate_bps)
        exporter_stats[exp_id]["if_count"] += 1

        interface_rows.append({
            "router_hostname": router.hostname,
            "interface_idx": if_idx,
            "interface_name": if_name,
            "speed_mbps": speed_mbps,
            "formatted_speed": format_speed(speed_mbps),
            "formatted_in_bps": format_bps(avg_in_bps),
            "formatted_out_bps": format_bps(avg_out_bps),
            "formatted_peak_bps": format_bps(peak_rate_bps),
            "utilization_pct": util_pct,
        })

    exporter_rows = []
    for r in routers:
        stats = exporter_stats.get(r.id, {"in_bytes": 0, "out_bytes": 0, "peak_bps": 0, "if_count": 0})
        mean_in_rate = (stats["in_bytes"] * 8.0) / total_window_sec
        exporter_rows.append({
            "hostname": r.hostname,
            "primary_ip": r.ip_address,
            "active_interfaces_count": stats["if_count"],
            "formatted_ingress_bytes": format_bytes(stats["in_bytes"]),
            "formatted_egress_bytes": format_bytes(stats["out_bytes"]),
            "formatted_ingress_bps": format_bps(mean_in_rate),
            "formatted_peak_bps": format_bps(stats["peak_bps"]),
        })

    # 2. Top Forensic Conversations
    conv_stmt = (
        select(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.dst_port,
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.exporter_id,
            func.sum(FlowMinuteRollup.bytes).label("total_bytes"),
        )
        .where(
            FlowMinuteRollup.exporter_id.in_(exporter_ids),
            FlowMinuteRollup.bucket >= start_dt,
            FlowMinuteRollup.bucket <= end_dt,
        )
        .group_by(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.dst_port,
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.exporter_id,
        )
        .order_by(desc("total_bytes"))
        .limit(10)
    )
    conv_res = await db.execute(conv_stmt)
    raw_convs = conv_res.all()

    total_transit_bytes = sum(int(c[5] or 0) for c in raw_convs) or 1
    conversation_rows = []
    for src_ip, dst_ip, port, proto, exp_id, b in raw_convs:
        c_bytes = int(b or 0)
        pct = (c_bytes / total_transit_bytes) * 100.0
        r_name = router_map.get(exp_id).hostname if exp_id in router_map else "Unknown"

        conversation_rows.append({
            "src_display": src_ip or "Unknown",
            "dst_display": dst_ip or "Unknown",
            "port_label": format_port_label(port, proto),
            "router_name": r_name,
            "formatted_bytes": format_bytes(c_bytes),
            "pct_of_transit": pct,
        })

    # 3. Fleet-Wide DSCP QoS Distribution (RFC 2474 / RFC 3246)
    # Extracts the 6-bit DiffServ Codepoint (DSCP) from the 8-bit IPv4 ToS / IPv6 Traffic Class
    # byte via: dscp = (tos & 0xFF) >> 2. Aggregates byte volume, packet volume, and flow counts
    # across standard QoS traffic classes (Best Effort CS0, Assured Forwarding AF*, Voice EF).
    qos_stmt = (
        select(
            FlowMinuteRollup.tos,
            func.coalesce(func.sum(FlowMinuteRollup.bytes), 0).label("total_bytes"),
            func.coalesce(func.sum(FlowMinuteRollup.packets), 0).label("total_packets"),
            func.coalesce(func.sum(FlowMinuteRollup.flow_count), 0).label("flow_count"),
        )
        .where(
            FlowMinuteRollup.exporter_id.in_(exporter_ids),
            FlowMinuteRollup.bucket >= start_dt,
            FlowMinuteRollup.bucket <= end_dt,
        )
        .group_by(FlowMinuteRollup.tos)
    )
    qos_rows = []
    try:
        qos_res = await db.execute(qos_stmt)
        raw_qos = qos_res.all() if qos_res else []
        dscp_buckets: Dict[int, Dict[str, Any]] = {}
        total_qos_bytes = 0
        for q_row in raw_qos:
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

        sorted_dscp = sorted(dscp_buckets.values(), key=lambda x: x["total_bytes"], reverse=True)
        for item in sorted_dscp:
            pct = (item["total_bytes"] / total_qos_bytes * 100.0) if total_qos_bytes > 0 else 0.0
            qos_rows.append({
                "dscp": item["dscp"],
                "dscp_label": item["dscp_label"],
                "total_bytes": item["total_bytes"],
                "formatted_bytes": format_bytes(item["total_bytes"]),
                "total_packets": item["total_packets"],
                "formatted_packets": f"{item['total_packets']:,}",
                "flow_count": item["flow_count"],
                "formatted_flows": f"{item['flow_count']:,}",
                "percentage": pct,
            })
    except Exception as exc:
        logger.debug("Failed or skipped QoS rollup in report compiler: %s", exc)

    # 4. Security Anomaly Telemetry (RFC 793 TCP Control Bits Analysis)
    # Identifies forensic security incidents from flow control flags:
    # - SYN Scan / Probe: SYN flag (0x02) asserted WITHOUT ACK (0x10), characteristic of half-open port scanning.
    # - TCP RST Burst: RST flag (0x04) asserted WITHOUT FIN (0x01), characteristic of refused/rejected connections.
    anomaly_stmt = (
        select(
            FlowMinuteRollup.protocol,
            FlowMinuteRollup.tcp_flags,
            func.coalesce(func.sum(FlowMinuteRollup.flow_count), 0).label("flow_count"),
            func.coalesce(func.sum(FlowMinuteRollup.bytes), 0).label("total_bytes"),
        )
        .where(
            FlowMinuteRollup.exporter_id.in_(exporter_ids),
            FlowMinuteRollup.bucket >= start_dt,
            FlowMinuteRollup.bucket <= end_dt,
            FlowMinuteRollup.protocol == 6,
        )
        .group_by(FlowMinuteRollup.protocol, FlowMinuteRollup.tcp_flags)
    )
    syn_scan_count = 0
    syn_scan_bytes = 0
    rst_burst_count = 0
    rst_burst_bytes = 0
    try:
        anomaly_res = await db.execute(anomaly_stmt)
        raw_anomalies = anomaly_res.all() if anomaly_res else []
        for a_row in raw_anomalies:
            if len(a_row) < 3:
                continue
            flags = int(a_row[1] or 0)
            cnt = int(a_row[2] or 0)
            b_val = int(a_row[3] or 0) if len(a_row) > 3 else 0

            # SYN ONLY (0x02 & ~0x10): Half-open connection initiation without completion
            if (flags & 0x02) and not (flags & 0x10):
                syn_scan_count += cnt
                syn_scan_bytes += b_val
            # TCP RST (0x04 & ~0x01): Connection reset without graceful close
            elif (flags & 0x04) and not (flags & 0x01):
                rst_burst_count += cnt
                rst_burst_bytes += b_val
    except Exception as exc:
        logger.debug("Failed or skipped Anomaly rollup in report compiler: %s", exc)

    total_anomaly_count = syn_scan_count + rst_burst_count
    anomaly_summary = {
        "total_count": total_anomaly_count,
        "syn_scan_count": syn_scan_count,
        "syn_scan_bytes": format_bytes(syn_scan_bytes),
        "rst_burst_count": rst_burst_count,
        "rst_burst_bytes": format_bytes(rst_burst_bytes),
        "has_anomalies": total_anomaly_count > 0,
    }

    # 5. Traffic Zone Breakdown (East-West vs North-South Perimeter)
    # Categorizes flows into perimeter boundary crossings versus internal lateral movement:
    # - East-West: Both source and destination reside within private subnets (RFC 1918) or managed endpoints.
    # - North-South Ingress: External internet sources entering internal monitored infrastructure.
    # - North-South Egress: Internal monitored endpoints communicating out to public internet destinations.
    # - External / Transit: Multi-hop traffic between external peers traversing edge routers.
    zone_stmt = (
        select(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.src_endpoint_id,
            FlowMinuteRollup.dst_endpoint_id,
            func.coalesce(func.sum(FlowMinuteRollup.bytes), 0).label("total_bytes"),
            func.coalesce(func.sum(FlowMinuteRollup.flow_count), 0).label("flow_count"),
        )
        .where(
            FlowMinuteRollup.exporter_id.in_(exporter_ids),
            FlowMinuteRollup.bucket >= start_dt,
            FlowMinuteRollup.bucket <= end_dt,
        )
        .group_by(
            FlowMinuteRollup.src_ip,
            FlowMinuteRollup.dst_ip,
            FlowMinuteRollup.src_endpoint_id,
            FlowMinuteRollup.dst_endpoint_id,
        )
    )
    zone_sums = {
        "East-West (Internal)": {"desc": "Internal Mesh / Host-to-Host", "bytes": 0, "flows": 0},
        "North-South Egress": {"desc": "Internal to External Transit", "bytes": 0, "flows": 0},
        "North-South Ingress": {"desc": "External to Internal Transit", "bytes": 0, "flows": 0},
        "External / Transit": {"desc": "Perimeter Cross-Link / Transit", "bytes": 0, "flows": 0},
    }

    def _is_internal(ip_val: str, ep_val: Optional[Any]) -> bool:
        if ep_val is not None:
            return True
        try:
            return ipaddress.ip_address(ip_val).is_private
        except Exception:
            return False

    zone_rows = []
    try:
        zone_res = await db.execute(zone_stmt)
        raw_zones = zone_res.all() if zone_res else []
        total_zone_bytes = 0
        for z_row in raw_zones:
            if len(z_row) < 5:
                continue
            s_ip = str(z_row[0] or "")
            d_ip = str(z_row[1] or "")
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
            total_zone_bytes += z_b

        for z_name, z_data in zone_sums.items():
            pct = (z_data["bytes"] / total_zone_bytes * 100.0) if total_zone_bytes > 0 else 0.0
            zone_rows.append({
                "zone": z_name,
                "description": z_data["desc"],
                "total_bytes": z_data["bytes"],
                "formatted_bytes": format_bytes(z_data["bytes"]),
                "flow_count": z_data["flows"],
                "formatted_flows": f"{z_data['flows']:,}",
                "percentage": pct,
            })
    except Exception as exc:
        logger.debug("Failed or skipped Zone rollup in report compiler: %s", exc)
        for z_name, z_data in zone_sums.items():
            zone_rows.append({
                "zone": z_name,
                "description": z_data["desc"],
                "total_bytes": 0,
                "formatted_bytes": "0 B",
                "flow_count": 0,
                "formatted_flows": "0",
                "percentage": 0.0,
            })

    return {
        "exporters": exporter_rows,
        "interfaces": interface_rows,
        "conversations": conversation_rows,
        "qos_classes": qos_rows,
        "anomalies": anomaly_summary,
        "traffic_zones": zone_rows,
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
        "summary": avail_data["summary"],
        "exporters": bandwidth_data["exporters"],
        "interfaces": bandwidth_data["interfaces"],
        "conversations": bandwidth_data["conversations"],
        "qos_classes": bandwidth_data.get("qos_classes", []),
        "anomalies": bandwidth_data.get("anomalies", {}),
        "traffic_zones": bandwidth_data.get("traffic_zones", []),
    }
