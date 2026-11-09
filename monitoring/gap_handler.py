"""Monitoring gaps: times when the engine was not monitoring (stopped, restarting, or unable to
write to the database).

Gaps are rows in monitoring_service_events. Availability (SLA) figures leave them out instead of
counting them as up or down. At startup resolve_startup_state closes the periods the last run left
open and records the gap; close_startup_gap ends it once monitoring has resumed.
record_lost_cycles_gap records the cycles dropped when a database outage outlasted the engine's
in-memory backlog (monitoring/cycle_store).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID
from app.core.orm import text
from app.core.orm import AsyncSession

from monitoring.probe_samples import closed_event_stats_sql

logger = logging.getLogger(__name__)


async def record_lost_cycles_gap(db: AsyncSession, start: datetime, end: datetime) -> None:
    """Probe results dropped because a database outage outlasted the engine's backlog: nothing
    is known about this period, so availability figures leave it out."""
    await db.execute(
        text("""
            INSERT INTO monitoring_service_events (event_type, start_time, end_time, duration_seconds)
            VALUES ('DATABASE_DISCONNECT', :start, :end, EXTRACT(EPOCH FROM (CAST(:end AS timestamptz) - CAST(:start AS timestamptz)))::BIGINT)
        """),
        {"start": start, "end": end},
    )
    logger.warning("Monitoring gap recorded: database outage outlasted the probe backlog (%s to %s)", start, end)


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
    # TimescaleDB aborts a transaction that decompresses more rows than
    # timescaledb.max_tuples_decompressed_per_dml_transaction allows. The startup cleanup can touch
    # compressed chunks, so lift the limit (0 = none) for this transaction only: set_config's
    # third argument, true, makes the setting local to it. Skipped without TimescaleDB.
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
    gap's id, or None when there is no gap (first start, or nothing monitored).
    """
    await _lift_decompression_cap(db)
    now = datetime.now(timezone.utc)

    # A gap still open here was left by a start that stopped before closing it. Only a cycle run
    # after that start shows monitoring resumed: periods from before it can begin after the
    # gap does, since the gap starts at the earliest endpoint's last cycle. With no such cycle
    # the engine has not been monitoring since the gap began, so the same gap goes on.
    query_select = text("""
        SELECT id, start_time, event_type, created_at
        FROM monitoring_service_events
        WHERE end_time IS NULL
        ORDER BY start_time DESC
        LIMIT 1
    """)
    row = (await db.execute(query_select)).fetchone()
    continued_gap: Optional[UUID] = None
    if row:
        resumed = (await db.execute(
            text("""
                SELECT MAX(start_time) FROM endpoint_events
                WHERE start_time >= :started AND start_time <= :settled
            """),
            {"started": row.created_at, "settled": row.created_at + timedelta(seconds=STARTUP_SETTLE_SECONDS)},
        )).scalar()
        if resumed is None:
            continued_gap = UUID(str(row.id))
            logger.warning(
                "Startup: monitoring has not run since the gap of %s (type=%s) began. It stays open.",
                row.start_time,
                row.event_type,
            )
        else:
            logger.warning(
                "Startup: closing the unresolved monitoring gap from %s (type=%s) where monitoring resumed.",
                row.start_time,
                row.event_type,
            )
            await db.execute(
                text("""
                    UPDATE monitoring_service_events
                    SET end_time = GREATEST(start_time, :resumed),
                        duration_seconds = EXTRACT(EPOCH FROM (GREATEST(start_time, :resumed) - start_time))::BIGINT
                    WHERE id = CAST(:gap_id AS uuid)
                """),
                {"resumed": resumed, "gap_id": str(row.id)},
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
        logger.info("Startup: no open endpoint events found in active window.")
        return continued_gap

    # The gap starts at the earliest last cycle among the endpoints still being probed when the
    # engine stopped.
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

    if continued_gap is not None:
        return continued_gap

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

