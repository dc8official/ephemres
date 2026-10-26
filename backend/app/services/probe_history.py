"""Read side of the per-cycle probe history (endpoint_probe_samples / endpoint_probe_hourly).

Series pick their resolution from the requested span so a chart never carries more than a few
hundred points per endpoint:
- up to 2 days: one point per monitoring cycle (raw samples, ~1 minute);
- up to 8 days: 10-minute buckets over the raw samples;
- longer: the hourly continuous aggregate (raw samples are only kept 30 days).
"""
from __future__ import annotations

import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.orm import AsyncSession, text

RAW_MAX_SPAN = datetime.timedelta(days=2)
TEN_MINUTE_MAX_SPAN = datetime.timedelta(days=8)

# Reply-weighted mean RTT, matching how endpoint_probe_hourly aggregates.
_WEIGHTED_RTT = (
    "SUM(rtt_avg_ms * received) / NULLIF(SUM(received) FILTER (WHERE rtt_avg_ms IS NOT NULL), 0)"
)


def _loss_pct(sent: Optional[int], received: Optional[int]) -> Optional[float]:
    if not sent:
        return None
    return round((1.0 - (received or 0) / sent) * 100.0, 2)


def _num(value: Any, digits: int = 3) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _point(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": row["bucket"].astimezone(datetime.timezone.utc).isoformat(),
        "sample_count": int(row["sample_count"] or 0),
        "sent": int(row["sent"] or 0),
        "received": int(row["received"] or 0),
        "loss_pct": _loss_pct(row["sent"], row["received"]),
        "rtt_min_ms": _num(row["rtt_min_ms"]),
        "rtt_avg_ms": _num(row["rtt_avg_ms"]),
        "rtt_max_ms": _num(row["rtt_max_ms"]),
        "jitter_ms": _num(row["jitter_ms"]),
    }


async def probe_series(
    db: AsyncSession, endpoint_id: UUID, start: datetime.datetime, end: datetime.datetime
) -> Dict[str, Any]:
    span = end - start
    params = {"endpoint_id": str(endpoint_id), "start": start, "end": end}
    if span <= RAW_MAX_SPAN:
        bucket_seconds, source = 60, "endpoint_probe_samples"
        sql = """
            SELECT sampled_at AS bucket, 1 AS sample_count, sent, received,
                   rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms
            FROM endpoint_probe_samples
            WHERE endpoint_id = CAST(:endpoint_id AS uuid) AND sampled_at >= :start AND sampled_at < :end
            ORDER BY sampled_at
        """
    elif span <= TEN_MINUTE_MAX_SPAN:
        bucket_seconds, source = 600, "endpoint_probe_samples"
        sql = f"""
            SELECT to_timestamp(floor(extract(epoch FROM sampled_at) / 600) * 600) AS bucket,
                   COUNT(*) AS sample_count, SUM(sent) AS sent, SUM(received) AS received,
                   MIN(rtt_min_ms) AS rtt_min_ms, {_WEIGHTED_RTT} AS rtt_avg_ms,
                   MAX(rtt_max_ms) AS rtt_max_ms, AVG(jitter_ms) AS jitter_ms
            FROM endpoint_probe_samples
            WHERE endpoint_id = CAST(:endpoint_id AS uuid) AND sampled_at >= :start AND sampled_at < :end
            GROUP BY 1
            ORDER BY 1
        """
    else:
        bucket_seconds, source = 3600, "endpoint_probe_hourly"
        sql = """
            SELECT bucket, sample_count, sent, received, rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms
            FROM endpoint_probe_hourly
            WHERE endpoint_id = CAST(:endpoint_id AS uuid) AND bucket >= :start AND bucket < :end
            ORDER BY bucket
        """
    res = await db.execute(text(sql), params)
    points = [_point(dict(r)) for r in res.mappings()]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bucket_seconds": bucket_seconds,
        "source": source,
        "points": points,
    }


async def probe_summary(db: AsyncSession, hours: int = 24) -> List[Dict[str, Any]]:
    """Per-endpoint loss, RTT and jitter over the last `hours`, with an hourly RTT sparkline."""
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=hours)

    stats_res = await db.execute(
        text(f"""
            SELECT endpoint_id,
                   COUNT(*) AS sample_count,
                   SUM(sent) AS sent,
                   SUM(received) AS received,
                   {_WEIGHTED_RTT} AS rtt_avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY rtt_avg_ms) AS rtt_p95_ms,
                   AVG(jitter_ms) AS jitter_ms,
                   MAX(sampled_at) AS last_probe_at
            FROM endpoint_probe_samples
            WHERE sampled_at >= :since
            GROUP BY endpoint_id
        """),
        {"since": since},
    )
    stats = {str(r["endpoint_id"]): dict(r) for r in stats_res.mappings()}

    # Sparkline: one slot per hour, oldest first; None where the hour has no replies.
    first_hour = (now - datetime.timedelta(hours=hours - 1)).replace(minute=0, second=0, microsecond=0)
    hourly_res = await db.execute(
        text("""
            SELECT endpoint_id, bucket, rtt_avg_ms
            FROM endpoint_probe_hourly
            WHERE bucket >= :first_hour
        """),
        {"first_hour": first_hour},
    )
    sparklines: Dict[str, List[Optional[float]]] = {}
    for r in hourly_res.mappings():
        slot = int((r["bucket"] - first_hour).total_seconds() // 3600)
        if 0 <= slot < hours:
            line = sparklines.setdefault(str(r["endpoint_id"]), [None] * hours)
            line[slot] = _num(r["rtt_avg_ms"], 2)

    summary = []
    for endpoint_id, row in stats.items():
        last = row["last_probe_at"]
        summary.append({
            "endpoint_id": endpoint_id,
            "sample_count": int(row["sample_count"] or 0),
            "loss_pct": _loss_pct(row["sent"], row["received"]),
            "rtt_avg_ms": _num(row["rtt_avg_ms"], 2),
            "rtt_p95_ms": _num(row["rtt_p95_ms"], 2),
            "jitter_ms": _num(row["jitter_ms"], 2),
            "last_probe_at": last.astimezone(datetime.timezone.utc).isoformat() if last else None,
            "rtt_sparkline": sparklines.get(endpoint_id, [None] * hours),
        })
    return summary
