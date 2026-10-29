from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID
from app.core.orm import text
from app.core.orm import AsyncSession

from monitoring.probe_samples import closed_event_stats_sql

logger = logging.getLogger(__name__)

async def open_monitoring_gap(
    db: AsyncSession,
    event_type: str,
    description: Optional[str] = None,
) -> UUID:
    gap_start = datetime.now(timezone.utc)
    window_start = gap_start - timedelta(days=7)
    
    query_insert = text("""
        INSERT INTO monitoring_service_events (
            event_type,
            description,
            start_time
        ) VALUES (
            :event_type,
            :description,
            :start_time
        ) RETURNING id
    """)
    insert_result = await db.execute(
        query_insert,
        {
            "event_type": event_type,
            "description": description,
            "start_time": gap_start,
        }
    )
    row = insert_result.fetchone()
    gap_id = row.id if row else None
    
    query_update = text("""
        UPDATE endpoint_events
        SET
            end_time = :gap_time,
            duration_seconds = EXTRACT(
                EPOCH FROM (:gap_time - start_time)
            )::BIGINT,
    """ + closed_event_stats_sql(":gap_time") + """
        WHERE end_time IS NULL
          AND start_time >= :window_start
    """)
    update_result = await db.execute(
        query_update,
        {
            "gap_time": gap_start,
            "window_start": window_start,
        }
    )
    closed_count = update_result.rowcount
    
    logger.info("Monitoring gap opened (type=%s): %s endpoint events closed at %s", event_type, closed_count, gap_start)
    
    return UUID(str(gap_id))

async def close_monitoring_gap(
    db: AsyncSession,
    gap_id: UUID,
) -> None:
    end_time = datetime.now(timezone.utc)
    
    query = text("""
        UPDATE monitoring_service_events
        SET
            end_time = :end_time,
            duration_seconds = EXTRACT(
                EPOCH FROM (:end_time - start_time)
            )::BIGINT
        WHERE id = CAST(:gap_id AS uuid)
    """)
    await db.execute(
        query,
        {
            "end_time": end_time,
            "gap_id": str(gap_id),
        }
    )
    
    logger.info("Monitoring gap %s closed at %s", gap_id, end_time)

# Periods whose last cycle is this much older than the newest one belong to endpoints that
# were no longer probed (paused, deleted) before the stop; they do not start the gap.
STALE_CYCLE = timedelta(minutes=5)

# How long after startup every endpoint has run its first cycle: up to a minute to its slot
# plus the ping cycle (about 42 s at worst). The gap is closed after this.
STARTUP_SETTLE_SECONDS = 150

ENGINE_GAP_TYPE = "SERVICE_RESTART"  # allowed by ck_monitoring_events_type

# The last cycle of an open period: every cycle sets duration_seconds to its event time.
_LAST_CYCLE = "(endpoint_events.start_time + COALESCE(endpoint_events.duration_seconds, 0) * INTERVAL '1 second')"


async def _lift_decompression_cap(db: AsyncSession) -> None:
    # Safely bypass TimescaleDB tuple decompression limit for this startup transaction
    try:
        await db.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
                    PERFORM set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', '0', true);
                END IF;
            END $$;
        """))
    except Exception as exc:
        logger.debug("TimescaleDB decompression limit override skipped: %s", exc)


async def resolve_startup_state(db: AsyncSession) -> Optional[UUID]:
    """Close the periods the engine left open when it stopped, and record the time it was not
    running as a monitoring gap, which availability (SLA) figures leave out.

    Each open period ends at its own last cycle, so the downtime is not credited to whatever
    state the endpoint was in. The gap runs from the earliest of those last cycles until the
    first cycles after this start: close_startup_gap() ends it once they have run. Returns the
    gap's id, or None when nothing was left open (first start, or nothing monitored).
    """
    await _lift_decompression_cap(db)
    now = datetime.now(timezone.utc)

    # A gap still open here was left by a start that stopped before closing it: monitoring
    # resumed at the first period opened after it began.
    query_select = text("""
        SELECT id, start_time, event_type
        FROM monitoring_service_events
        WHERE end_time IS NULL
        ORDER BY start_time DESC
        LIMIT 1
    """)
    row = (await db.execute(query_select)).fetchone()
    if row:
        logger.warning(
            "Startup: found unresolved monitoring gap from %s (type=%s). Closing.",
            row.start_time,
            row.event_type,
        )
        await db.execute(
            text("""
                UPDATE monitoring_service_events
                SET end_time = COALESCE(
                        (SELECT MIN(start_time) FROM endpoint_events WHERE start_time > :gap_start),
                        :now),
                    duration_seconds = EXTRACT(EPOCH FROM (COALESCE(
                        (SELECT MIN(start_time) FROM endpoint_events WHERE start_time > :gap_start),
                        :now) - start_time))::BIGINT
                WHERE id = CAST(:gap_id AS uuid)
            """),
            {"gap_start": row.start_time, "now": now, "gap_id": str(row.id)},
        )

    # Bound cleanup to 7-day uncompressed window to enable TimescaleDB hypertable chunk pruning
    window_start = now - timedelta(days=7)

    cycles = (await db.execute(
        text(f"""
            SELECT MAX({_LAST_CYCLE}) AS newest, COUNT(*) AS open_count
            FROM endpoint_events
            WHERE end_time IS NULL AND start_time >= :window_start
        """),
        {"window_start": window_start},
    )).fetchone()
    if cycles is None or cycles.newest is None:
        logger.info("Startup: no open endpoint events found in active window. Clean state.")
        return None

    gap_start = (await db.execute(
        text(f"""
            SELECT MIN({_LAST_CYCLE}) FROM endpoint_events
            WHERE end_time IS NULL AND start_time >= :window_start AND {_LAST_CYCLE} >= :recent
        """),
        {"window_start": window_start, "recent": cycles.newest - STALE_CYCLE},
    )).scalar()

    query_update = text(f"""
        UPDATE endpoint_events
        SET
            end_time = {_LAST_CYCLE},
    """ + closed_event_stats_sql(_LAST_CYCLE) + """
        WHERE end_time IS NULL
          AND start_time >= :window_start
    """)
    update_result = await db.execute(query_update, {"window_start": window_start})

    gap_row = (await db.execute(
        text("""
            INSERT INTO monitoring_service_events (event_type, description, start_time)
            VALUES (:event_type, :description, :start_time)
            RETURNING id
        """),
        {
            "event_type": ENGINE_GAP_TYPE,
            "description": "Monitoring engine was not running (stopped, restarted or upgraded)",
            "start_time": gap_start,
        },
    )).fetchone()

    logger.warning(
        "Startup cleanup: closed %d open endpoint events at their last cycle; engine not running since %s.",
        update_result.rowcount,
        gap_start,
    )
    return UUID(str(gap_row.id))


async def close_startup_gap(db: AsyncSession, gap_id: UUID, started_at: datetime) -> None:
    """End the gap recorded at startup where monitoring resumed: at the last period opened by
    the first round of cycles after `started_at` (call it STARTUP_SETTLE_SECONDS later)."""
    await db.execute(
        text("""
            UPDATE monitoring_service_events
            SET end_time = GREATEST(start_time, COALESCE(
                    (SELECT MAX(start_time) FROM endpoint_events WHERE start_time >= :started_at),
                    :now)),
                duration_seconds = EXTRACT(EPOCH FROM (GREATEST(start_time, COALESCE(
                    (SELECT MAX(start_time) FROM endpoint_events WHERE start_time >= :started_at),
                    :now)) - start_time))::BIGINT
            WHERE id = CAST(:gap_id AS uuid) AND end_time IS NULL
        """),
        {"started_at": started_at, "now": datetime.now(timezone.utc), "gap_id": str(gap_id)},
    )
    logger.info("Monitoring gap %s closed: first cycles after startup have run.", gap_id)

async def get_active_gap(
    db: AsyncSession,
) -> Optional[tuple[UUID, datetime]]:
    query = text("""
        SELECT id, start_time
        FROM monitoring_service_events
        WHERE end_time IS NULL
        ORDER BY start_time DESC
        LIMIT 1
    """)
    result = await db.execute(query)
    row = result.fetchone()
    
    if row:
        return (UUID(str(row.id)), row.start_time)
    return None
