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
