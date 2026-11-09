"""Each endpoint's normal round-trip time, for telling a slow endpoint from a healthy one.

For every endpoint and hour of the week (Monday 00:00 to Sunday 23:00, 168 slots) the cache
holds the mean and standard deviation of RTT from node_historical_baselines. The engine passes
the current slot's figures to classify_ping_result, which marks an endpoint that answers every
ping UP-UNSTABLE when its RTT is above mean + 3 x deviation.

Without history there is no baseline (None) and RTT is not judged: during an endpoint's first
7 days and for any empty slot. Loss still decides UP, UP-UNSTABLE and DOWN as usual. (A fixed
50 ms +/- 15 ms default used to apply here, which marked healthy distant sites UP-UNSTABLE.)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple, Union
from uuid import UUID

from app.core.orm import text
from app.core.orm import AsyncSession

logger = logging.getLogger(__name__)

# DEFAULT_MEAN_MS is no longer used as a fallback (no baseline until real history); kept for
# imports. DEFAULT_STDDEV_MS still replaces a zero spread in real history.
DEFAULT_MEAN_MS: float = 50.0
DEFAULT_STDDEV_MS: float = 15.0
DEFAULT_K_MULTIPLIER: float = 3.0
GRACE_PERIOD_DAYS: int = 7


class BaselineCache:
    """
    In-memory baseline lookup cache storing historical latency statistics per endpoint.

    Uses a compacted, flattened, fixed-size 1D array of length 168 (7 days x 24 hours).

    Storage Structure:
        Dict[UUID, List[Optional[Tuple[float, float, int]]]]
        holding (mean, stddev, sample_count)

    Index Calculation Formula:
        Index = (day_of_week * 24) + hour_of_day
        where day_of_week is 0 (Monday) through 6 (Sunday), and hour_of_day is 0 through 23.
    """

    def __init__(self) -> None:
        self._cache: Dict[UUID, List[Optional[Tuple[float, float, int]]]] = {}
        self._onboarded_dates: Dict[UUID, datetime] = {}
        self._lock = asyncio.Lock()

    async def refresh_from_db(self, db: AsyncSession) -> None:
        """
        Asynchronously queries node_historical_baselines continuous aggregate
        and endpoints table to populate the 1D array baseline lookup map.
        """
        try:
            # Step 1: Query endpoints creation time to enforce grace period rules
            endpoints_query = text("SELECT id, created_at FROM endpoints WHERE endpoint_status != 'DELETED'")
            ep_result = await db.execute(endpoints_query)
            onboarded: Dict[UUID, datetime] = {}
            for row in ep_result.fetchall():
                onboarded[UUID(str(row.id))] = row.created_at

            # Step 2: Query continuous aggregate view node_historical_baselines
            baseline_query = text("""
                SELECT
                    endpoint_id,
                    day_of_week,
                    hour_of_day,
                    historical_mean,
                    historical_stddev
                FROM node_historical_baselines
            """)
            bl_result = await db.execute(baseline_query)

            new_cache: Dict[UUID, List[Optional[Tuple[float, float, int]]]] = {}
            for row in bl_result.fetchall():
                ep_id = UUID(str(row.endpoint_id))
                pg_dow = int(row.day_of_week)
                hour = int(row.hour_of_day)

                # Map PostgreSQL DOW (0=Sunday, 1=Monday...6=Saturday) to 0=Monday..6=Sunday
                dow = (pg_dow - 1) % 7 if (0 <= pg_dow <= 6) else 0

                idx = (dow * 24) + hour
                if not (0 <= idx < 168):
                    continue

                # An hour with no replies has no mean: leave its slot empty (no baseline).
                if row.historical_mean is None:
                    continue
                mean = float(row.historical_mean)
                stddev = float(row.historical_stddev) if row.historical_stddev is not None else DEFAULT_STDDEV_MS
                sample_count = int(getattr(row, "sample_count", 100)) if getattr(row, "sample_count", None) is not None else 100

                if ep_id not in new_cache:
                    new_cache[ep_id] = [None] * 168

                new_cache[ep_id][idx] = (mean, stddev, sample_count)

            async with self._lock:
                self._cache = new_cache
                self._onboarded_dates = onboarded

            logger.info(
                "BaselineCache updated successfully: %d endpoints cached (1D 168-element arrays).",
                len(new_cache),
            )

        except Exception as e:
            logger.error(
                "Failed to refresh baseline cache from database: %s: %s",
                type(e).__name__,
                e,
            )

    def get_baseline(
        self,
        endpoint_id: UUID,
        day_of_week: Optional[Union[int, datetime]] = None,
        hour_of_day: Optional[int] = None,
    ) -> Optional[Tuple[float, float, int]]:
        """
        Retrieves the (mean, stddev, sample_count) baseline for an endpoint.

        Can be called with:
          - (endpoint_id, day_of_week: int, hour_of_day: int)
          - (endpoint_id, dt: datetime)
          - (endpoint_id)  [uses current UTC datetime]

        Index Calculation = (day_of_week * 24) + hour_of_day (0=Monday..6=Sunday).

        No baseline (None) until there is real history, so latency is not judged at all:
        1. during the endpoint's first 7 days (grace period);
        2. when the hour-of-week slot has no data.
        With history, a zero spread falls back to DEFAULT_STDDEV_MS.
        """
        now_utc = datetime.now(timezone.utc)

        # Fallback 1: Grace period check (< 7 days of onboarding)
        created_at = self._onboarded_dates.get(endpoint_id)
        if created_at is not None:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age_days = (now_utc - created_at).total_seconds() / 86400.0
            if age_days < GRACE_PERIOD_DAYS:
                return None

        if isinstance(day_of_week, datetime):
            dt = day_of_week
            dow = dt.weekday()
            hour = dt.hour
        elif isinstance(day_of_week, int) and hour_of_day is not None:
            dow = day_of_week
            hour = hour_of_day
        else:
            dt = now_utc
            dow = dt.weekday()
            hour = dt.hour

        if not (0 <= dow <= 6 and 0 <= hour <= 23):
            return None

        index = (dow * 24) + hour

        endpoint_arr = self._cache.get(endpoint_id)
        if not endpoint_arr or index >= len(endpoint_arr):
            return None

        entry = endpoint_arr[index]
        if entry is None:
            return None

        mean, stddev, count = entry
        effective_stddev = stddev if (stddev is not None and stddev > 0) else DEFAULT_STDDEV_MS

        return (mean, effective_stddev, count)


# Global singleton instance of BaselineCache
baseline_cache = BaselineCache()


def calculate_z_score(
    latency_ms: float,
    mean_ms: float,
    stddev_ms: float,
) -> float:
    """
    Computes the Z-score for a given latency reading.
    Guarantees DivisionByZero protection when stddev <= 0 or None.
    """
    safe_stddev = stddev_ms if (stddev_ms is not None and stddev_ms > 0) else DEFAULT_STDDEV_MS
    return (latency_ms - mean_ms) / safe_stddev


def is_latency_degraded(
    latency_ms: Optional[float],
    mean_ms: float,
    stddev_ms: float,
    k: float = DEFAULT_K_MULTIPLIER,
) -> bool:
    """
    Evaluates whether a latency reading exceeds the dynamic baseline threshold (mean + k * stddev).
    Returns False if latency_ms is None (e.g. timeout / dropped packet).
    """
    if latency_ms is None:
        return False

    safe_stddev = stddev_ms if (stddev_ms is not None and stddev_ms > 0) else DEFAULT_STDDEV_MS
    threshold = mean_ms + (k * safe_stddev)
    return latency_ms > threshold


async def start_baseline_refresh_task(
    db_session_factory,
    interval_seconds: int = 3600,
) -> asyncio.Task:
    """
    Spawns an async background loop to periodically refresh baseline_cache hourly.
    """

    async def _refresh_loop():
        while True:
            try:
                async with db_session_factory() as db:
                    await baseline_cache.refresh_from_db(db)
            except Exception as e:
                logger.error("Error in baseline refresh background loop: %s", e)
            await asyncio.sleep(interval_seconds)

    task = asyncio.create_task(_refresh_loop())
    return task
