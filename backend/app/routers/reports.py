"""Report routes: the Reports page's fleet summary, per-endpoint uptime, incidents and events,
the CSV telemetry export, and the PDF reports.

The PDF numbers come from services/report_compiler.py; the page's numbers are computed here.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta, timezone
from math import ceil
from typing import Any, List, Optional, Union
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query
from app.core.asgi import StreamingResponse
from app.core.schema import BaseModel, ConfigDict, Field
from app.core.orm import and_, or_, select, text
import hashlib
import os

from app.core.orm import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.repositories.endpoint_repo import EndpointRepository
from app.repositories.report_repo import ReportRepository
from app.routers.auth import get_current_user, get_current_user_sse
from app.schemas import (
    APIResponse,
    EventRecord,
    FleetEndpointSummary,
    FleetSummaryResponse,
    PaginationMeta,
    UptimeReport,
)
from app.core.pdf import render_pdf_async
from app.services.report_compiler import (
    _merge_intervals,
    _overlap_seconds,
    compile_availability_report_data,
    compile_bandwidth_report_data,
    compile_master_report_data,
    format_period,
    get_organization_branding,
)
from app.core.template import TemplateEnvironment
from app.services.uptime_calculator import (
    calculate_device_gap_seconds,
    calculate_uptime_denominator_and_percentage,
    get_service_gap_intervals,
)

logger = logging.getLogger(__name__)

REPORTS_TEMPLATES_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "templates", "reports")
)
jinja_env = TemplateEnvironment(
    search_path=REPORTS_TEMPLATES_DIR,
    autoescape=True,
)

router = APIRouter(prefix="/reports", tags=["reports"])


def parse_datetime_param(val: str, is_end: bool = False) -> datetime:
    """A query date as an aware UTC datetime. A full timestamp without a zone is taken as UTC;
    a bare date (YYYY-MM-DD) means the whole UTC day, so as an end date it is 23:59:59."""
    try:
        val_str = str(val).strip()
        if "T" in val_str or " " in val_str:
            clean_val = val_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_val)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        else:
            d = date.fromisoformat(val_str)
            if is_end:
                return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc)
            return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    except (ValueError, AttributeError, TypeError) as e:
        logger.error(f"Failed to parse datetime parameter '{val}': {e}")
        raise HTTPException(
            status_code=400, detail=f"Invalid ISO 8601 date format: {val}"
        )


def _validate_date_range(
    start_date: Union[date, datetime],
    end_date: Union[date, datetime],
) -> None:
    if start_date > end_date:
        raise HTTPException(
            status_code=400,
            detail="start_date must be before or equal to end_date.",
        )
    # Two years at most: bounds how much history one request can make the database scan.
    diff_days = (end_date - start_date).total_seconds() / 86400.0
    if diff_days > 730:
        raise HTTPException(
            status_code=400,
            detail="Date range cannot exceed 730 days.",
        )


@router.get("/fleet-summary", response_model=APIResponse)
async def get_fleet_summary(
    start_date: str = Query(...),
    end_date: str = Query(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_dt = parse_datetime_param(start_date, is_end=False)
    end_dt = parse_datetime_param(end_date, is_end=True)
    _validate_date_range(start_dt, end_dt)

    stmt = (
        select(Endpoint)
        .where(Endpoint.endpoint_status != "DELETED")
        .order_by(Endpoint.hostname.asc())
    )
    res = await db.execute(stmt)
    endpoints = res.scalars().all()

    # Monitoring gaps, merged so overlapping ones are not subtracted twice.
    gap_intervals = await get_service_gap_intervals(db, start_dt, end_dt)
    gaps = _merge_intervals(gap_intervals)

    # Per endpoint, in one pass over endpoint_events: seconds UP and DOWN inside the window (each
    # period clipped to it, an open period running to now, and time inside a monitoring gap left
    # out, as in the PDF report: a database outage leaves the open period running across its
    # gap), and incidents, counted as DOWN periods not preceded by another DOWN one, so 24 h
    # splits of one outage count once. rn = 1 is the latest period, which gives the current state.
    stats_map = {}
    now_utc = datetime.now(timezone.utc)
    cte_query = text("""
        WITH gaps AS (
            SELECT g.s, g.e
            FROM unnest(CAST(:gap_starts AS timestamptz[]), CAST(:gap_ends AS timestamptz[])) AS g(s, e)
        ),
        clipped AS (
            SELECT
                endpoint_id,
                operational_state,
                detailed_state,
                start_time,
                GREATEST(CAST(:start_dt AS timestamptz), start_time) AS seg_start,
                LEAST(CAST(:end_dt AS timestamptz), COALESCE(end_time, NOW())) AS seg_end
            FROM endpoint_events
            WHERE (end_time IS NULL OR end_time >= :start_dt) AND start_time <= :end_dt
        ),
        ranked_events AS (
            SELECT
                endpoint_id,
                operational_state,
                detailed_state,
                CASE WHEN seg_end <= seg_start THEN 0 ELSE GREATEST(0,
                    EXTRACT(EPOCH FROM (seg_end - seg_start))
                    - COALESCE((
                        SELECT SUM(EXTRACT(EPOCH FROM (LEAST(seg_end, g.e) - GREATEST(seg_start, g.s))))
                        FROM gaps g
                        WHERE g.s < seg_end AND g.e > seg_start
                    ), 0)
                ) END::BIGINT AS clamped_duration,
                ROW_NUMBER() OVER (PARTITION BY endpoint_id ORDER BY start_time DESC) AS rn,
                LAG(operational_state) OVER (PARTITION BY endpoint_id ORDER BY start_time ASC) AS prev_state
            FROM clipped
        )
        SELECT
            endpoint_id,
            COALESCE(SUM(CASE WHEN operational_state = 'UP' THEN clamped_duration ELSE 0 END), 0) AS up_seconds,
            COALESCE(SUM(CASE WHEN operational_state = 'DOWN' THEN clamped_duration ELSE 0 END), 0) AS down_seconds,
            COUNT(CASE WHEN operational_state = 'UP' THEN 1 END) AS up_count,
            COUNT(CASE WHEN operational_state = 'DOWN' THEN 1 END) AS down_count,
            COUNT(CASE WHEN operational_state = 'DOWN' AND (prev_state IS NULL OR prev_state != 'DOWN') THEN 1 END) AS incident_count,
            MAX(CASE WHEN rn = 1 THEN operational_state END) AS latest_operational_state,
            MAX(CASE WHEN rn = 1 THEN detailed_state END) AS latest_detailed_state
        FROM ranked_events
        GROUP BY endpoint_id
    """)

    try:
        raw_res = await db.execute(cte_query, {
            "start_dt": start_dt,
            "end_dt": end_dt,
            # Tuples go to asyncpg as PostgreSQL arrays (a list would be sent as JSON).
            "gap_starts": tuple(g_start for g_start, _ in gaps),
            "gap_ends": tuple(g_end for _, g_end in gaps),
        })
        for r in raw_res.mappings():
            stats_map[str(r["endpoint_id"])] = r
    except Exception as e:
        # The same sums in Python, used only if the query above fails.
        logger.warning("CTE fleet summary query failed, using manual event fallback: %s", e)
        ev_stmt = (
            select(EndpointEvent)
            .where(
                (EndpointEvent.end_time == None) | (EndpointEvent.end_time >= start_dt),
                EndpointEvent.start_time <= end_dt,
            )
            .order_by(EndpointEvent.start_time.asc())
        )
        ev_res = await db.execute(ev_stmt)
        all_events = ev_res.scalars().all()
        grouped_events: dict[str, list[EndpointEvent]] = {}
        for ev in all_events:
            grouped_events.setdefault(str(ev.endpoint_id), []).append(ev)
        for ep_key, evs in grouped_events.items():
            up_cnt = sum(1 for ev in evs if ev.operational_state == "UP")
            down_cnt = sum(1 for ev in evs if ev.operational_state == "DOWN")
            up_sec = 0
            down_sec = 0
            inc_cnt = 0
            prev_st = None
            for ev in evs:
                ev_start = ev.start_time
                ev_end = ev.end_time or now_utc
                seg_start, seg_end = max(start_dt, ev_start), min(end_dt, ev_end)
                dur = 0
                if seg_end > seg_start:
                    span = (seg_end - seg_start).total_seconds()
                    dur = max(0, int(round(span - _overlap_seconds(seg_start, seg_end, gaps))))
                if ev.operational_state == "UP":
                    up_sec += dur
                elif ev.operational_state == "DOWN":
                    down_sec += dur
                    if prev_st != "DOWN":
                        inc_cnt += 1
                prev_st = ev.operational_state
            last_ev = evs[-1]
            stats_map[ep_key] = {
                "endpoint_id": ep_key,
                "up_count": up_cnt,
                "down_count": down_cnt,
                "up_seconds": up_sec,
                "down_seconds": down_sec,
                "incident_count": inc_cnt,
                "latest_operational_state": last_ev.operational_state,
                "latest_detailed_state": last_ev.detailed_state,
            }

    endpoint_summaries = []

    for ep in endpoints:
        ep_id_str = str(ep.id)
        ep_stats = stats_map.get(ep_id_str)
        up_count = ep_stats["up_count"] if ep_stats else 0
        down_count = ep_stats["down_count"] if ep_stats else 0
        incident_count = ep_stats["incident_count"] if ep_stats else 0
        op_state = (
            ep_stats["latest_operational_state"]
            if (ep_stats and ep_stats.get("latest_operational_state"))
            else ("UP" if ep.endpoint_status == "ACTIVE" else "DOWN")
        )
        det_state = (
            ep_stats["latest_detailed_state"]
            if (ep_stats and ep_stats.get("latest_detailed_state"))
            else op_state
        )

        created_at = ep.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)

        effective_start = max(start_dt, created_at)
        effective_end = min(end_dt, now_utc)
        total_seconds = max(0, int((effective_end - effective_start).total_seconds()))

        # Time inside monitoring gaps (engine stopped, database lost) is unknown: it is taken out
        # of the uptime denominator.
        unknown_seconds = calculate_device_gap_seconds(
            effective_start, effective_end, gap_intervals
        )
        # The "* 60" fallbacks assume one-minute rows; only reached when the sums are missing.
        up_sec = int(ep_stats["up_seconds"]) if (ep_stats and "up_seconds" in ep_stats) else up_count * 60
        down_sec = int(ep_stats["down_seconds"]) if (ep_stats and "down_seconds" in ep_stats) else down_count * 60
        uptime_percentage = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=start_dt,
            end_time=end_dt,
            now_utc=now_utc,
            up_events_count=up_count,
            unknown_seconds=unknown_seconds,
            gap_intervals=gap_intervals,
            uptime_seconds=up_sec,
        )
        uptime_seconds = up_sec
        downtime_seconds = down_sec
        role = getattr(ep, "device_role", "ACTIVE_HOST")
        # Same rule as the endpoint list: PASSIVE means ICMP monitoring is off, whatever the
        # device_role. A flow exporter with monitoring enabled reports its probed state.
        if ep.monitoring_enabled is False:
            op_state = "PASSIVE"
            det_state = "PASSIVE"
            uptime_percentage = 100.0
            incident_count = 0
            downtime_seconds = 0
            uptime_seconds = 0

        endpoint_summaries.append(
            FleetEndpointSummary(
                id=ep.id,
                hostname=ep.hostname,
                ip_address=str(ep.ip_address),
                device_type=ep.device_type,
                operational_state=op_state,
                detailed_state=det_state,
                monitoring_enabled=ep.monitoring_enabled,
                uptime_percentage=uptime_percentage,
                incident_count=incident_count,
                uptime_seconds=uptime_seconds,
                downtime_seconds=downtime_seconds,
                total_seconds=total_seconds,
                device_role=role,
            )
        )

    active_endpoints_count = sum(
        1 for ep in endpoints
        if ep.monitoring_enabled
    )
    total_endpoints_count = len(endpoints)
    total_incident_count = sum(e.incident_count for e in endpoint_summaries)
    total_downtime_seconds = sum(e.downtime_seconds for e in endpoint_summaries)

    # Fleet SLA: the plain mean of the monitored endpoints' percentages (each endpoint counts the
    # same, however long it existed in the window). The page recomputes it the same way.
    monitored_for_sla = [
        e for e in endpoint_summaries
        if getattr(e, "monitoring_enabled", True) is not False
    ]
    fleet_sla = (
        round(
            sum(e.uptime_percentage for e in monitored_for_sla)
            / len(monitored_for_sla),
            2,
        )
        if monitored_for_sla
        else 100.0
    )

    fleet_summary = FleetSummaryResponse(
        fleet_sla=fleet_sla,
        active_endpoints_count=active_endpoints_count,
        total_endpoints_count=total_endpoints_count,
        total_incident_count=total_incident_count,
        total_downtime_seconds=total_downtime_seconds,
        endpoints=endpoint_summaries,
    )
    return APIResponse.success(data=fleet_summary)


@router.get("/uptime/{endpoint_id}", response_model=APIResponse)
async def get_uptime_report(
    endpoint_id: UUID,
    start_date: str = Query(...),
    end_date: str = Query(...),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_dt = parse_datetime_param(start_date, is_end=False)
    end_dt = parse_datetime_param(end_date, is_end=True)
    _validate_date_range(start_dt, end_dt)

    ep_repo = EndpointRepository(db)
    endpoint = await ep_repo.get_by_id(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    created_at = endpoint.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    else:
        created_at = created_at.astimezone(timezone.utc)

    effective_start = max(start_dt, created_at)
    now_utc = datetime.now(timezone.utc)
    effective_end = min(end_dt, now_utc)
    total_seconds = max(0, int((effective_end - effective_start).total_seconds()))

    gap_intervals = await get_service_gap_intervals(db, start_dt, end_dt)
    gaps = _merge_intervals(gap_intervals)
    unknown_seconds = calculate_device_gap_seconds(
        effective_start, effective_end, gap_intervals
    )
    rep_repo = ReportRepository(db)
    events = await rep_repo.get_uptime_events(endpoint_id, effective_start, end_dt)

    # Time inside a monitoring gap is neither up nor down (as in the PDF report).
    uptime_seconds = 0
    downtime_seconds = 0
    for ev in events:
        ev_start = ev.start_time
        ev_end = ev.end_time or now_utc
        clamped_start = max(start_dt, ev_start)
        clamped_end = min(end_dt, ev_end)
        if clamped_end <= clamped_start:
            continue
        span = (clamped_end - clamped_start).total_seconds()
        dur = max(0, int(round(span - _overlap_seconds(clamped_start, clamped_end, gaps))))
        if ev.operational_state == "UP":
            uptime_seconds += dur
        else:
            downtime_seconds += dur

    uptime_percentage = calculate_uptime_denominator_and_percentage(
        created_at=created_at,
        start_time=start_dt,
        end_time=end_dt,
        now_utc=now_utc,
        up_events_count=uptime_seconds // 60,
        unknown_seconds=unknown_seconds,
        gap_intervals=gap_intervals,
        uptime_seconds=uptime_seconds,
    )

    incident_count = 0
    prev_state = None
    for ev in events:
        if ev.operational_state == "DOWN" and prev_state != "DOWN":
            incident_count += 1
        prev_state = ev.operational_state

    return APIResponse.success(
        data=UptimeReport(
            endpoint_id=endpoint_id,
            period_start=start_dt,
            period_end=end_dt,
            total_seconds=total_seconds,
            uptime_seconds=uptime_seconds,
            downtime_seconds=downtime_seconds,
            unknown_seconds=unknown_seconds,
            uptime_percentage=uptime_percentage,
            incident_count=incident_count,
        )
    )


@router.get("/incidents/{endpoint_id}", response_model=APIResponse)
async def get_incident_report(
    endpoint_id: UUID,
    start_date: str = Query(...),
    end_date: str = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_dt = parse_datetime_param(start_date, is_end=False)
    end_dt = parse_datetime_param(end_date, is_end=True)
    _validate_date_range(start_dt, end_dt)

    ep_repo = EndpointRepository(db)
    endpoint = await ep_repo.get_by_id(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    rep_repo = ReportRepository(db)
    limit = page_size
    offset = (page - 1) * page_size

    incidents, total = await rep_repo.get_incidents(
        endpoint_id=endpoint_id,
        start_dt=start_dt,
        end_dt=end_dt,
        limit=limit,
        offset=offset,
    )
    total_pages = ceil(total / page_size) if total > 0 else 1

    return APIResponse.success(
        data=incidents,
        meta=PaginationMeta(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        ),
    )


@router.get("/events/{endpoint_id}", response_model=APIResponse)
async def get_endpoint_events(
    endpoint_id: UUID,
    start_date: str = Query(...),
    end_date: str = Query(...),
    page: int = Query(default=1, ge=1),
    size: Optional[int] = Query(default=None, ge=1, le=1500),
    page_size: Optional[int] = Query(default=None, ge=1, le=1500),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    start_dt = parse_datetime_param(start_date, is_end=False)
    end_dt = parse_datetime_param(end_date, is_end=True)
    _validate_date_range(start_dt, end_dt)

    effective_size = page_size or size or 100
    rep_repo = ReportRepository(db)
    limit = effective_size
    offset = (page - 1) * effective_size

    event_rows, total = await rep_repo.get_events(
        endpoint_id=endpoint_id,
        start_dt=start_dt,
        end_dt=end_dt,
        limit=limit,
        offset=offset,
    )

    events = [
        EventRecord(
            id=ev.id,
            endpoint_id=ev.endpoint_id,
            operational_state=ev.operational_state,
            detailed_state=ev.detailed_state,
            health_score=float(ev.health_score),
            avg_rtt_ms=(
                float(ev.avg_rtt_ms) if ev.avg_rtt_ms is not None else None
            ),
            is_split_event=ev.is_split_event,
            start_time=ev.start_time,
            end_time=ev.end_time,
            duration_seconds=ev.duration_seconds,
            monitoring_cycle_count=ev.monitoring_cycle_count,
        )
        for ev in event_rows
    ]

    total_pages = ceil(total / effective_size) if total > 0 else 1

    return APIResponse.success(
        data=events,
        meta=PaginationMeta(
            total=total,
            page=page,
            page_size=effective_size,
            total_pages=total_pages,
        ),
    )


# ---------------------------------------------------------------------------
# Batch Telemetry Export Streaming API
# ---------------------------------------------------------------------------
class BatchExportRequest(BaseModel):
    endpoint_ids: List[UUID]
    start_time: datetime
    end_time: datetime
    columns: Optional[List[str]] = None

    model_config = ConfigDict(from_attributes=True)


# CSV injection guard: a cell starting with = + - @ (or a tab or CR) is run as a formula by
# Excel and LibreOffice. A leading apostrophe makes them show it as text instead.
def sanitize_csv_field(val: Any) -> str:
    s = str(val) if val is not None else ""
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{s}"
    return s


# Turns requested columns into CSV headers, always in the standard order. The page sends the
# header names themselves; API callers may use looser names ("rtt", "latency", "loss").
# Matching is by substring and the first match wins. Hostname and IP address are always included.
def resolve_export_columns(requested_columns: Optional[List[str]]) -> List[str]:
    standard_order = [
        "Endpoint_ID",
        "Hostname",
        "IP_Address",
        "Device_Type",
        "Timestamp",
        "Operational_State",
        "Detailed_State",
        "Packet_Success_Rate",
        "Avg_RTT_ms",
    ]
    if not requested_columns:
        return standard_order

    valid_map = {
        "endpoint_id": "Endpoint_ID",
        "hostname": "Hostname",
        "ip_address": "IP_Address",
        "device_type": "Device_Type",
        "timestamp": "Timestamp",
        "operational_state": "Operational_State",
        "detailed_state": "Detailed_State",
        "packet_success_rate": "Packet_Success_Rate",
        "health_score": "Packet_Success_Rate",
        "loss": "Packet_Success_Rate",
        "avg_rtt_ms": "Avg_RTT_ms",
        "rtt": "Avg_RTT_ms",
        "latency": "Avg_RTT_ms",
    }

    normalized_selected = set()
    for col in requested_columns:
        clean = (
            col.lower()
            .strip()
            .replace(" ", "_")
            .replace("-", "_")
            .replace("_(rtt_ms)", "")
            .replace("_(iso_utc)", "")
            .replace("/_packet_loss_%", "")
            .replace("/_loss_%", "")
        )
        matched = None
        for k, target in valid_map.items():
            if k in clean or clean in k:
                matched = target
                break
        if matched:
            normalized_selected.add(matched)
        elif col in standard_order:
            normalized_selected.add(col)

    # Guarantee Hostname and IP_Address are always included
    normalized_selected.add("Hostname")
    normalized_selected.add("IP_Address")

    return [col for col in standard_order if col in normalized_selected]


async def csv_generator(
    endpoint_ids: List[UUID],
    start_time: datetime,
    end_time: datetime,
    columns: Optional[List[str]] = None,
):
    selected_cols = resolve_export_columns(columns)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(selected_cols)
    yield output.getvalue()
    output.seek(0)
    output.truncate(0)

    if not endpoint_ids:
        return

    # Keyset pagination over (start_time, id): each batch resumes after the last row of the one
    # before, so the database never has to skip earlier rows the way OFFSET does, and every
    # batch opens and closes its own short session. Rows are flushed to the client every 500.
    # Events are selected by their start time.
    last_start_time: Optional[datetime] = None
    last_id: Optional[UUID] = None
    limit = 1000
    batch_buffer_size = 500
    buffered_rows = 0

    try:
        while True:
            rows = []
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(
                        EndpointEvent,
                        Endpoint.hostname,
                        Endpoint.ip_address,
                        Endpoint.device_type,
                    )
                    .join(Endpoint, EndpointEvent.endpoint_id == Endpoint.id)
                    .where(
                        EndpointEvent.endpoint_id.in_(endpoint_ids),
                        EndpointEvent.start_time >= start_time,
                        EndpointEvent.start_time <= end_time,
                    )
                )
                if last_start_time is not None and last_id is not None:
                    stmt = stmt.where(
                        or_(
                            EndpointEvent.start_time > last_start_time,
                            and_(
                                EndpointEvent.start_time == last_start_time,
                                EndpointEvent.id > last_id,
                            ),
                        )
                    )
                stmt = stmt.order_by(
                    EndpointEvent.start_time.asc(),
                    EndpointEvent.id.asc(),
                ).limit(limit)

                result = await session.execute(stmt)
                rows = result.all()

            if not rows:
                break

            for row in rows:
                ev = row[0]
                hostname = row[1]
                ip_addr = str(row[2])
                device_type = row[3]

                endpoint_id_str = sanitize_csv_field(str(ev.endpoint_id))
                hostname_str = sanitize_csv_field(hostname)
                ip_str = sanitize_csv_field(ip_addr)
                dev_type_str = sanitize_csv_field(device_type)
                ts_str = sanitize_csv_field(
                    ev.start_time.isoformat().replace("+00:00", "Z")
                    if ev.start_time
                    else ""
                )
                op_state = sanitize_csv_field(ev.operational_state)
                det_state = sanitize_csv_field(ev.detailed_state)
                success_rate = sanitize_csv_field(
                    ("%.2f" % ev.health_score)
                    if ev.health_score is not None
                    else ""
                )
                rtt_val = sanitize_csv_field(
                    ("%.2f" % ev.avg_rtt_ms)
                    if ev.avg_rtt_ms is not None
                    else ""
                )

                row_data_map = {
                    "Endpoint_ID": endpoint_id_str,
                    "Hostname": hostname_str,
                    "IP_Address": ip_str,
                    "Device_Type": dev_type_str,
                    "Timestamp": ts_str,
                    "Operational_State": op_state,
                    "Detailed_State": det_state,
                    "Packet_Success_Rate": success_rate,
                    "Avg_RTT_ms": rtt_val,
                }
                writer.writerow([row_data_map[c] for c in selected_cols])
                buffered_rows += 1

                if buffered_rows >= batch_buffer_size:
                    yield output.getvalue()
                    output.seek(0)
                    output.truncate(0)
                    buffered_rows = 0

            last_ev = rows[-1][0]
            last_start_time = last_ev.start_time
            last_id = last_ev.id

            if len(rows) < limit:
                break

        if buffered_rows > 0:
            remaining = output.getvalue()
            if remaining:
                yield remaining
            output.seek(0)
            output.truncate(0)
            buffered_rows = 0
    finally:
        output.close()


class PdfReportRequest(BaseModel):
    template_type: str = Field(..., description="'availability', 'bandwidth', or 'master'")
    endpoint_ids: List[UUID] = Field(..., min_length=1, description="List of target endpoints or flow exporters")
    start_date: str = Field(..., description="ISO 8601 start timestamp")
    end_date: str = Field(..., description="ISO 8601 end timestamp")
    include_rca: bool = Field(default=True, description="Whether to include Root Cause Analysis in incident ledgers")
    scope_title: Optional[str] = Field(default=None, description="Custom scope header description")

    model_config = ConfigDict(extra="ignore")


@router.post("/pdf")
async def generate_pdf_report(
    request: PdfReportRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Asynchronously compiles network telemetry and renders an immutable, print-ready PDF audit report.
    Permission: any authenticated user (reports are read-only).
    """
    valid_map = {
        "a": "availability",
        "availability": "availability",
        "b": "bandwidth",
        "bandwidth": "bandwidth",
        "c": "master",
        "master": "master",
    }
    raw_type = request.template_type.lower().strip()
    ttype = valid_map.get(raw_type)
    if not ttype:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid template_type '{request.template_type}'. Must be one of ('availability'/'a', 'bandwidth'/'b', 'master'/'c').",
        )

    start_dt = parse_datetime_param(request.start_date, is_end=False)
    end_dt = parse_datetime_param(request.end_date, is_end=True)
    _validate_date_range(start_dt, end_dt)

    org = await get_organization_branding(db)
    now_utc = datetime.now(timezone.utc)
    generated_at_str = now_utc.strftime("%Y-%m-%d %H:%M")
    period_label = format_period(start_dt.astimezone(timezone.utc), end_dt.astimezone(timezone.utc))

    default_scope = f"{len(request.endpoint_ids)} Infrastructure Targets"
    scope_title = request.scope_title or default_scope

    if ttype == "availability":
        data = await compile_availability_report_data(
            db, request.endpoint_ids, start_dt, end_dt, include_rca=request.include_rca
        )
        report_title = "Device Availability & Uptime Performance Report"
        template_name = "template_a_availability.html"
        theme_accent = "#2563eb"
        filename = f"ephemres_availability_report_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pdf"
        context = {
            "org": org,
            "report_title": f"{org.get('company_name', 'Ephemres')} — {report_title}",
            "report_type_label": report_title,
            "period_label": period_label,
            "generated_at": generated_at_str,
            "theme_accent": theme_accent,
            "scope_title": scope_title,
            "endpoints": data["endpoints"],
            "incidents": data["incidents"],
            "incidents_total": data.get("incidents_total", len(data["incidents"])),
            "summary": data["summary"],
        }
    elif ttype == "bandwidth":
        data = await compile_bandwidth_report_data(
            db, request.endpoint_ids, start_dt, end_dt
        )
        report_title = "Network Bandwidth & Flow Telemetry Report"
        template_name = "template_b_bandwidth.html"
        theme_accent = "#059669"
        filename = f"ephemres_bandwidth_report_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pdf"
        context = {
            "org": org,
            "report_title": f"{org.get('company_name', 'Ephemres')} — {report_title}",
            "report_type_label": report_title,
            "period_label": period_label,
            "generated_at": generated_at_str,
            "theme_accent": theme_accent,
            "scope_title": scope_title,
            "exporters": data["exporters"],
            "active_exporters": data.get("active_exporters", len(data["exporters"])),
            "interfaces": data["interfaces"],
            "conversations": data["conversations"],
            "qos_classes": data.get("qos_classes", []),
            "anomalies": data.get("anomalies", {}),
            "traffic_zones": data.get("traffic_zones", []),
            "wan_edge": data.get("wan_edge"),
            "coverage_notes": data.get("coverage_notes", []),
        }
    else:  # master
        data = await compile_master_report_data(
            db, request.endpoint_ids, start_dt, end_dt, include_rca=request.include_rca
        )
        report_title = "Comprehensive Master Operational Audit Dossier"
        template_name = "template_c_master.html"
        theme_accent = "#7c3aed"
        filename = f"ephemres_master_audit_report_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}.pdf"
        context = {
            "org": org,
            "report_title": f"{org.get('company_name', 'Ephemres')} — {report_title}",
            "report_type_label": report_title,
            "period_label": period_label,
            "generated_at": generated_at_str,
            "theme_accent": theme_accent,
            "scope_title": scope_title,
            "endpoints": data["endpoints"],
            "incidents": data["incidents"],
            "incidents_total": data.get("incidents_total", len(data["incidents"])),
            "summary": data["summary"],
            "exporters": data["exporters"],
            "active_exporters": data.get("active_exporters", len(data["exporters"])),
            "interfaces": data["interfaces"],
            "conversations": data["conversations"],
            "qos_classes": data.get("qos_classes", []),
            "anomalies": data.get("anomalies", {}),
            "traffic_zones": data.get("traffic_zones", []),
            "wan_edge": data.get("wan_edge"),
            "coverage_notes": data.get("coverage_notes", []),
        }

    template = jinja_env.get_template(template_name)
    rendered_html = template.render(**context)

    try:
        pdf_bytes = await render_pdf_async(rendered_html, title=report_title)
    except Exception as exc:
        logger.error("Failed to render PDF report: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Report rendering error: {str(exc)}",
        )

    # The SHA-256 goes in the log and a response header, so a PDF shown later as evidence can be
    # checked against the one that was generated.
    sha256_hash = hashlib.sha256(pdf_bytes).hexdigest()
    logger.info(
        "Successfully compiled %s PDF report for %d targets (SHA256: %s, size: %d bytes)",
        ttype,
        len(request.endpoint_ids),
        sha256_hash,
        len(pdf_bytes),
    )

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(pdf_bytes)),
            "X-Report-SHA256": sha256_hash,
        },
    )


telemetry_router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


@telemetry_router.post("/export/batch")
async def batch_export_telemetry(
    request: BatchExportRequest,
    current_user: dict = Depends(get_current_user_sse),
):
    # Any authenticated user may export: telemetry export is read-only. The SSE variant of the
    # auth check releases its database connection at once, since the stream can run for minutes.
    logger.info(
        "Starting batch telemetry CSV streaming export for %d endpoints (user: %s)",
        len(request.endpoint_ids),
        current_user.get("username"),
    )

    generator = csv_generator(
        request.endpoint_ids,
        request.start_time,
        request.end_time,
        request.columns,
    )

    return StreamingResponse(
        generator,
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=batch_telemetry_export.csv"
        },
    )
