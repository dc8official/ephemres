"""Per-cycle probe history (endpoint_probe_samples, migration 0015).

endpoint_events only keeps one row per state period, so it cannot answer "what was the RTT or
loss at 14:05". Each monitoring cycle also writes one sample row here. The write runs in its own
short transaction after the state machine has committed, so a failed sample write (for example
before migration 0015 has run) can never roll back state tracking.
"""
from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from app.core.orm import text
from app.database import AsyncSessionLocal
from monitoring.ping import PingResult

logger = logging.getLogger(__name__)

_INSERT_SAMPLE = text(
    """
    INSERT INTO endpoint_probe_samples (
        endpoint_id, sampled_at, sent, received,
        rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms
    )
    VALUES (
        CAST(:endpoint_id AS uuid), :sampled_at, :sent, :received,
        :rtt_min_ms, :rtt_avg_ms, :rtt_max_ms, :jitter_ms
    )
    ON CONFLICT (endpoint_id, sampled_at) DO NOTHING
    """
)

# Log a failing write once per process instead of once per endpoint per minute.
_write_failure_logged = False


def sample_params(endpoint_id: UUID, sampled_at: datetime, result: PingResult) -> dict:
    return {
        "endpoint_id": str(endpoint_id),
        "sampled_at": sampled_at,
        "sent": result.total_count,
        "received": result.success_count,
        "rtt_min_ms": result.min_rtt_ms,
        "rtt_avg_ms": result.avg_rtt_ms,
        "rtt_max_ms": result.max_rtt_ms,
        "jitter_ms": result.jitter_ms,
    }


def closed_event_stats_sql(
    close_time: str,
    success: str = "success_count",
    failed: str = "failed_count",
    closed_by_cycle: bool = False,
) -> str:
    """SET clauses that give a closing endpoint_events row the health and RTT of its whole period.

    While an event is open its health_score and avg_rtt_ms are the latest cycle's (the dashboard
    shows them as current values), so without this a closed UP period that ended in an outage
    kept the failed last cycle: 0 % health and no RTT, and a DOWN period kept the first good
    probe of the recovery. Health is exact from the accumulated counts; RTT is the probe-weighted
    mean of the period's samples.

    Each endpoint is probed in a fixed slot once a minute and its sample is stamped when the
    cycle starts, while the event time is taken after the ping, up to a cycle later when probes
    time out. So a cycle's sample lies in the minute before its event time: the period's samples
    are those after start_time - 1 minute, up to end_time - 1 minute when the close is a state
    change (`closed_by_cycle`: that confirming cycle belongs to the next event), or up to
    end_time otherwise (engine stop/start, 24 h split). `success` / `failed` are the SQL for the
    final counts, `close_time` the parameter holding end_time.
    """
    return f"""
        health_score = {period_health_sql(success, failed)},
        avg_rtt_ms = {period_rtt_sql(close_time, success, closed_by_cycle)}"""


def period_health_sql(success: str = "success_count", failed: str = "failed_count") -> str:
    """An endpoint_events row's health over its whole period (see closed_event_stats_sql)."""
    return f"""CASE WHEN ({success}) + ({failed}) > 0
            THEN ROUND(100.0 * ({success}) / (({success}) + ({failed})), 2)
            ELSE health_score END"""


def period_rtt_sql(close_time: str, success: str = "success_count", closed_by_cycle: bool = False) -> str:
    """An endpoint_events row's RTT over its whole period, its current value when no sample of
    the period is left (see closed_event_stats_sql)."""
    upper = (
        f"s.sampled_at <= CAST({close_time} AS timestamptz) - INTERVAL '1 minute'"
        if closed_by_cycle
        else f"s.sampled_at < CAST({close_time} AS timestamptz)"
    )
    return f"""CASE WHEN ({success}) = 0 THEN NULL ELSE COALESCE((
            SELECT ROUND((SUM(s.rtt_avg_ms * s.received)
                          / NULLIF(SUM(s.received) FILTER (WHERE s.rtt_avg_ms IS NOT NULL), 0))::numeric, 2)
            FROM endpoint_probe_samples s
            WHERE s.endpoint_id = endpoint_events.endpoint_id
              AND s.sampled_at > endpoint_events.start_time - INTERVAL '1 minute'
              AND {upper}
        ), avg_rtt_ms) END"""


async def record_probe_sample(endpoint_id: UUID, sampled_at: datetime, result: PingResult) -> bool:
    """Store one cycle's result. Returns False (and logs once) when the write fails."""
    global _write_failure_logged
    if result.total_count <= 0:
        return False
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(_INSERT_SAMPLE, sample_params(endpoint_id, sampled_at, result))
            await db.commit()
        _write_failure_logged = False
        return True
    except Exception as exc:
        if not _write_failure_logged:
            logger.warning("Could not record probe sample (is migration 0015 applied?): %s", exc)
            _write_failure_logged = True
        return False
