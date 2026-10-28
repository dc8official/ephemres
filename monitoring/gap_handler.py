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

async def resolve_startup_state(db: AsyncSession) -> None:
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

    query_select = text("""
        SELECT id, start_time, event_type
        FROM monitoring_service_events
        WHERE end_time IS NULL
        ORDER BY start_time DESC
        LIMIT 1
    """)
    select_result = await db.execute(query_select)
    row = select_result.fetchone()

    if row:
        logger.warning(
            "Startup: found unresolved monitoring gap from %s "
            "(type=%s). Closing.",
            row.start_time,
            row.event_type,
        )
        gap_close_time = row.start_time
        await close_monitoring_gap(db, UUID(str(row.id)))
    else:
        gap_close_time = datetime.now(timezone.utc)

    # Bound cleanup to 7-day uncompressed window to enable TimescaleDB hypertable chunk pruning
    window_start = gap_close_time - timedelta(days=7)

    query_update = text("""
        UPDATE endpoint_events
        SET
            end_time = :close_time,
            duration_seconds = EXTRACT(
                EPOCH FROM (:close_time - start_time)
            )::BIGINT,
    """ + closed_event_stats_sql(":close_time") + """
        WHERE end_time IS NULL
          AND start_time >= :window_start
    """)
    update_result = await db.execute(
        query_update,
        {
            "close_time": gap_close_time,
            "window_start": window_start,
        },
    )
    closed_count = update_result.rowcount

    if closed_count > 0:
        logger.warning(
            "Startup cleanup: closed %d orphaned open "
            "endpoint events at %s (window: >= %s).",
            closed_count,
            gap_close_time,
            window_start,
        )
    else:
        logger.info(
            "Startup: no open endpoint events found in active window. "
            "Clean state."
        )

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
