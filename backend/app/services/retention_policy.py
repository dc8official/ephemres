"""Ephemres Network Monitoring Platform — Telemetry Retention Policy Sync.

Aligns the TimescaleDB retention policy on endpoint_events with
monitoring.retention_days from config.toml. Migration 0010 installs the
initial 730-day policy; this keeps it in step when operators change the value.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.core.orm import text

logger = logging.getLogger(__name__)

EVENTS_TABLE = "endpoint_events"


async def _current_drop_after(db: Any) -> Optional[str]:
    res = await db.execute(
        text(
            "SELECT config->>'drop_after' AS drop_after "
            "FROM timescaledb_information.jobs "
            "WHERE proc_name = 'policy_retention' AND hypertable_name = :table "
            "LIMIT 1"
        ),
        {"table": EVENTS_TABLE},
    )
    row = res.fetchone()
    if row is None:
        return None
    return getattr(row, "drop_after", None) if not isinstance(row, (tuple, list)) else row[0]


async def sync_event_retention_policy(db: Any, retention_days: int) -> bool:
    """Replaces the endpoint_events retention policy when it differs. Returns True if changed."""
    ext = await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))
    if ext.fetchone() is None:
        logger.debug("TimescaleDB not installed; skipping endpoint_events retention policy sync.")
        return False

    desired = f"{int(retention_days)} days"
    current = await _current_drop_after(db)
    if current == desired:
        return False

    await db.execute(text(f"SELECT remove_retention_policy('{EVENTS_TABLE}', if_exists => TRUE)"))
    await db.execute(
        text(
            # Bound as text: asyncpg only encodes timedelta for INTERVAL parameters.
            f"SELECT add_retention_policy('{EVENTS_TABLE}', CAST(CAST(:drop_after AS TEXT) AS INTERVAL), if_not_exists => TRUE)"
        ),
        {"drop_after": desired},
    )
    await db.commit()
    logger.info(
        "Applied monitoring.retention_days: %s retention changed from %s to %s.",
        EVENTS_TABLE,
        current or "none",
        desired,
    )
    return True
