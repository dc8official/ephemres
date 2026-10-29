from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple
from app.core.orm import text
from app.core.orm import AsyncSession
from app.services.timezone_utils import get_local_timezone


def calculate_device_gap_seconds(
    device_start: datetime,
    device_end: datetime,
    gap_intervals: List[Tuple[datetime, datetime]],
) -> int:
    """
    Computes device-specific downtime gap seconds by intersecting the device's
    operational lifespan [device_start, device_end] with service gap intervals.
    """
    local_tz = get_local_timezone()
    if device_start.tzinfo is None:
        device_start = device_start.replace(tzinfo=local_tz)
    else:
        device_start = device_start.astimezone(local_tz)

    if device_end.tzinfo is None:
        device_end = device_end.replace(tzinfo=local_tz)
    else:
        device_end = device_end.astimezone(local_tz)

    total_gap_seconds = 0
    for g_start, g_end in gap_intervals:
        if g_start.tzinfo is None:
            g_start = g_start.replace(tzinfo=local_tz)
        else:
            g_start = g_start.astimezone(local_tz)

        if g_end.tzinfo is None:
            g_end = g_end.replace(tzinfo=local_tz)
        else:
            g_end = g_end.astimezone(local_tz)

        overlap_start = max(g_start, device_start)
        overlap_end = min(g_end, device_end)
        if overlap_end > overlap_start:
            total_gap_seconds += int((overlap_end - overlap_start).total_seconds())

    return total_gap_seconds


def calculate_uptime_denominator_and_percentage(
    created_at: datetime,
    start_time: datetime,
    end_time: datetime,
    now_utc: datetime,
    up_events_count: int = 0,
    unknown_seconds: int = 0,
    gap_intervals: Optional[List[Tuple[datetime, datetime]]] = None,
    uptime_seconds: Optional[int] = None,
) -> float:
    """
    Calculates uptime availability percentage with SLA precision.
    If gap_intervals is provided, computes exact device-specific intersection
    against downtime gap intervals.
    """
    local_tz = get_local_timezone()
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=local_tz)
    else:
        created_at = created_at.astimezone(local_tz)

    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=local_tz)
    else:
        start_time = start_time.astimezone(local_tz)

    if end_time.tzinfo is None:
        end_time = end_time.replace(tzinfo=local_tz)
    else:
        end_time = end_time.astimezone(local_tz)

    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=local_tz)
    else:
        now_utc = now_utc.astimezone(local_tz)

    # Calculate true operational lifespan within the queried block
    effective_start = max(start_time, created_at)
    effective_end = min(end_time, now_utc)

    total_seconds = max(0, int((effective_end - effective_start).total_seconds()))

    if gap_intervals is not None:
        device_gap_sec = calculate_device_gap_seconds(
            effective_start, effective_end, gap_intervals
        )
    else:
        device_gap_sec = unknown_seconds

    denominator = total_seconds - device_gap_sec

    if denominator <= 0:
        return 100.0  # Return 100% availability if no elapsing time exists yet

    if uptime_seconds is None:
        uptime_seconds = up_events_count * 60
    percentage = (uptime_seconds / denominator) * 100.0
    return max(0.0, min(100.0, round(percentage, 2)))


async def get_service_gap_intervals(
    db: AsyncSession,
    start_dt: datetime,
    end_dt: datetime,
) -> List[Tuple[datetime, datetime]]:
    """
    Retrieves all service downtime gap intervals overlapping with [start_dt, end_dt].
    Returns list of (gap_start, gap_end) with timezone awareness.
    """
    local_tz = get_local_timezone()
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=local_tz)
    else:
        start_dt = start_dt.astimezone(local_tz)

    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=local_tz)
    else:
        end_dt = end_dt.astimezone(local_tz)

    try:
        query_gaps = text("""
            SELECT start_time, end_time
            FROM monitoring_service_events
            WHERE start_time < :end_dt
              AND (end_time > :start_dt OR end_time IS NULL)
            ORDER BY start_time ASC
        """)
        result_gaps = await db.execute(query_gaps, {
            "start_dt": start_dt,
            "end_dt": end_dt,
        })
        gap_rows = result_gaps.fetchall()
        intervals: List[Tuple[datetime, datetime]] = []
        for row in gap_rows:
            r_start = row.start_time
            if r_start.tzinfo is None:
                r_start = r_start.replace(tzinfo=local_tz)
            else:
                r_start = r_start.astimezone(local_tz)

            r_end = row.end_time
            if r_end is not None:
                if r_end.tzinfo is None:
                    r_end = r_end.replace(tzinfo=local_tz)
                else:
                    r_end = r_end.astimezone(local_tz)
            else:
                r_end = end_dt

            # Clamp to [start_dt, end_dt]
            g_start = max(r_start, start_dt)
            g_end = min(r_end, end_dt)
            if g_end > g_start:
                # Rows are ordered by start: merge overlaps so no second is left out twice.
                if intervals and g_start <= intervals[-1][1]:
                    intervals[-1] = (intervals[-1][0], max(intervals[-1][1], g_end))
                else:
                    intervals.append((g_start, g_end))
        return intervals
    except Exception:
        return []


async def get_unknown_seconds_for_period(
    db: AsyncSession,
    effective_start: datetime,
    period_end: datetime,
) -> int:
    """Calculates total seconds during which monitoring service was unavailable."""
    intervals = await get_service_gap_intervals(db, effective_start, period_end)
    return calculate_device_gap_seconds(effective_start, period_end, intervals)
