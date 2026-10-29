"""Optional one-off correction of the audit log's health and average RTT.

Before v3.3.5 a closed period (endpoint_events row) kept the health and RTT of its last
monitoring cycle instead of its whole period: an UP period that ended in an outage showed 0 %
health and no RTT. v3.3.5 closes periods with the right values; this recomputes older ones
with the same formulas (monitoring.probe_samples):

- health: exact for every period, from the probe counts every version has recorded;
- average RTT: only for periods whose per-cycle probe samples are all still kept (they started
  with v3.3.5 and are kept 30 days). Older periods keep the figure they have.

Periods that are already right are not written, so it is safe to run more than once and while
the services run. Without --apply it only reports what would change.

    sudo /opt/ephemres/deploy/repair-audit-history.sh [--apply] [--since YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import sys
from typing import Any, Dict, List, Optional, Tuple

from app.core.orm import AsyncSession, text
from app.database import AsyncSessionLocal

from monitoring.probe_samples import period_health_sql, period_rtt_sql

BATCH = datetime.timedelta(days=30)

# RTT is only recomputed when the samples reach back to the period's first cycle (stamped in the
# minute before start_time). A period closed by a state change ends where the next period, in
# another state, starts; its last cycle confirmed that change and belongs to the next period
# (closed_by_cycle). UPDATE ... RETURNING: a statement starting with WITH is fetched, so the
# returned rows are its row count.
_CHANGED_PERIODS = f"""
    WITH periods AS (
        SELECT id, start_time, health_score AS old_health, avg_rtt_ms AS old_rtt,
               {period_health_sql()} AS new_health,
               CASE
                   WHEN success_count = 0 THEN NULL
                   WHEN NOT EXISTS (
                       SELECT 1 FROM endpoint_probe_samples first_sample
                       WHERE first_sample.endpoint_id = endpoint_events.endpoint_id
                         AND first_sample.sampled_at <= endpoint_events.start_time
                   ) THEN avg_rtt_ms
                   WHEN EXISTS (
                       SELECT 1 FROM endpoint_events next_period
                       WHERE next_period.endpoint_id = endpoint_events.endpoint_id
                         AND next_period.start_time = endpoint_events.end_time
                         AND next_period.detailed_state <> endpoint_events.detailed_state
                   ) THEN {period_rtt_sql("endpoint_events.end_time", closed_by_cycle=True)}
                   ELSE {period_rtt_sql("endpoint_events.end_time")}
               END AS new_rtt
        FROM endpoint_events
        WHERE end_time IS NOT NULL AND start_time >= :batch_start AND start_time < :batch_end
    ),
    changed AS (
        SELECT * FROM periods
        WHERE new_health IS DISTINCT FROM old_health OR new_rtt IS DISTINCT FROM old_rtt
    )
"""

_COUNT_SQL = _CHANGED_PERIODS + """
    SELECT COUNT(*) FILTER (WHERE new_health IS DISTINCT FROM old_health) AS health,
           COUNT(*) FILTER (WHERE new_rtt IS DISTINCT FROM old_rtt) AS rtt,
           MIN(start_time) AS oldest,
           ARRAY_AGG(start_time) AS starts
    FROM changed
"""

_APPLY_SQL = _CHANGED_PERIODS + """
    UPDATE endpoint_events
    SET health_score = changed.new_health, avg_rtt_ms = changed.new_rtt
    FROM changed
    WHERE endpoint_events.id = changed.id AND endpoint_events.start_time = changed.start_time
    RETURNING endpoint_events.id
"""

# Writing into compressed chunks decompresses them; the default cap would stop a large batch.
_LIFT_DECOMPRESSION_CAP = """
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
            PERFORM set_config('timescaledb.max_tuples_decompressed_per_dml_transaction', '0', true);
        END IF;
    END $$;
"""


async def _batches(db: AsyncSession, since: Optional[datetime.datetime]) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    res = await db.execute(text("SELECT MIN(start_time) FROM endpoint_events WHERE end_time IS NOT NULL"))
    first = res.scalar()
    if first is None:
        return []
    start = max(first, since) if since else first
    end = datetime.datetime.now(datetime.timezone.utc)
    batches = []
    while start < end:
        batches.append((start, min(start + BATCH, end)))
        start += BATCH
    return batches


async def _compressed_ranges(db: AsyncSession) -> List[Tuple[datetime.datetime, datetime.datetime]]:
    res = await db.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"))
    if res.scalar() is None:
        return []
    res = await db.execute(text("""
        SELECT range_start, range_end FROM timescaledb_information.chunks
        WHERE hypertable_name = 'endpoint_events' AND is_compressed
    """))
    return [(r[0], r[1]) for r in res.all()]


async def needed() -> bool:
    """Whether some closed period's health differs from its probe counts (a quick check)."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(text(f"""
            SELECT EXISTS (
                SELECT 1 FROM endpoint_events
                WHERE end_time IS NOT NULL AND health_score IS DISTINCT FROM {period_health_sql()}
            )
        """))
        return bool(res.scalar())


async def check(since: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """What a repair would change; writes nothing."""
    totals: Dict[str, Any] = {"health": 0, "rtt": 0, "periods": 0, "compressed": 0, "oldest": None}
    async with AsyncSessionLocal() as db:
        compressed = await _compressed_ranges(db)
        for batch_start, batch_end in await _batches(db, since):
            res = await db.execute(text(_COUNT_SQL), {"batch_start": batch_start, "batch_end": batch_end})
            row = res.mappings().first()
            starts = row["starts"] or []
            totals["health"] += int(row["health"] or 0)
            totals["rtt"] += int(row["rtt"] or 0)
            totals["periods"] += len(starts)
            totals["compressed"] += sum(1 for s in starts if any(lo <= s < hi for lo, hi in compressed))
            if row["oldest"] is not None and (totals["oldest"] is None or row["oldest"] < totals["oldest"]):
                totals["oldest"] = row["oldest"]
    return totals


async def apply(since: Optional[datetime.datetime] = None, progress=print) -> int:
    """Correct the periods, one 30-day batch per transaction. Returns the number written."""
    written = 0
    async with AsyncSessionLocal() as db:
        batches = await _batches(db, since)
    for batch_start, batch_end in batches:
        async with AsyncSessionLocal() as db:
            await db.execute(text(_LIFT_DECOMPRESSION_CAP))
            res = await db.execute(text(_APPLY_SQL), {"batch_start": batch_start, "batch_end": batch_end})
            await db.commit()
        written += res.rowcount
        progress(f"  {batch_start:%Y-%m-%d} to {batch_end:%Y-%m-%d}: {res.rowcount} periods corrected")
    return written


def _report(totals: Dict[str, Any]) -> str:
    if totals["periods"] == 0:
        return "The audit log's health and latency figures are already correct. Nothing to do."
    lines = [
        "Audit log check (nothing has been changed):",
        f"  Periods whose health figure would be corrected:   {totals['health']:,}",
        f"  Periods whose average latency would be corrected: {totals['rtt']:,}",
        f"  Oldest of these periods starts on:                {totals['oldest']:%Y-%m-%d}",
        "",
        "  Health can be corrected for all of the history. Average latency can only be",
        "  corrected for periods recorded since the upgrade to v3.3.5 (and in the last 30 days);",
        "  older periods keep the figure they have.",
    ]
    if totals["compressed"]:
        lines += [
            "",
            f"  {totals['compressed']:,} of these periods are in compressed storage. Correcting them makes",
            "  the database decompress and rewrite that part of the history: it takes longer, uses",
            "  temporary disk space, and the next compression job compresses it again.",
        ]
    lines += ["", "To correct them, run:  sudo /opt/ephemres/deploy/repair-audit-history.sh --apply"]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="repair-audit-history",
        description="Correct the health and average latency of audit-log periods recorded before v3.3.5.",
    )
    parser.add_argument("--apply", action="store_true", help="write the corrections (default: only report them)")
    parser.add_argument("--since", type=datetime.date.fromisoformat, help="only periods that start on or after this date (YYYY-MM-DD)")
    parser.add_argument("--needed", action="store_true", help="exit 0 when some period needs correcting, 1 otherwise (used by upgrade.sh)")
    args = parser.parse_args(argv)
    if args.needed:
        try:
            return 0 if asyncio.run(needed()) else 1
        except Exception as exc:
            print(f"Audit log check skipped: {exc}", file=sys.stderr)
            return 1
    since = (
        datetime.datetime.combine(args.since, datetime.time(), tzinfo=datetime.timezone.utc) if args.since else None
    )

    totals = asyncio.run(check(since))
    print(_report(totals))
    if not args.apply or totals["periods"] == 0:
        return 0
    print("\nCorrecting periods...")
    try:
        written = asyncio.run(apply(since))
    except Exception as exc:
        print(f"\nThe correction stopped: {exc}", file=sys.stderr)
        print("Batches reported above were saved; running the command again continues from there.", file=sys.stderr)
        return 1
    print(f"\nDone: {written:,} periods corrected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
