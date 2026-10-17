from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence
from uuid import UUID

from app.core.orm import case, func, or_, select
from app.core.orm import AsyncSession

from app.models.endpoint_event import EndpointEvent
from app.schemas.reports import IncidentRecord
from app.services.uptime_calculator import get_unknown_seconds_for_period


class ReportRepository:
    """
    Repository for telemetry, uptime calculation, and paginated incident reporting.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_uptime_events(
        self,
        endpoint_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
    ) -> Sequence[EndpointEvent]:
        """Fetch all raw events for uptime calculation within the time window."""
        stmt = (
            select(EndpointEvent)
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.start_time <= end_dt,
                or_(
                    EndpointEvent.end_time.is_(None),
                    EndpointEvent.end_time >= start_dt,
                ),
            )
            .order_by(EndpointEvent.start_time.asc())
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_unknown_seconds(
        self,
        start_dt: datetime,
        end_dt: datetime,
    ) -> int:
        """Calculate total seconds during which monitoring was unavailable."""
        return await get_unknown_seconds_for_period(self.session, start_dt, end_dt)

    async def get_incidents(
        self,
        endpoint_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[IncidentRecord], int]:
        """
        Retrieve paginated incident logs using database-level window functions and LIMIT/OFFSET.
        Groups contiguous DOWN states into distinct incidents.
        """
        # Step 1: Detect group boundary transitions with LAG()
        lag_state = func.lag(EndpointEvent.operational_state).over(
            order_by=EndpointEvent.start_time.asc()
        )
        is_new_incident = case(
            (lag_state == EndpointEvent.operational_state, 0),
            else_=1,
        )

        down_events_cte = (
            select(
                EndpointEvent.endpoint_id,
                EndpointEvent.operational_state,
                EndpointEvent.detailed_state,
                EndpointEvent.start_time,
                EndpointEvent.end_time,
                is_new_incident.label("is_new_group"),
            )
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.start_time >= start_dt,
                EndpointEvent.start_time <= end_dt,
            )
            .cte("down_events_cte")
        )

        # Step 2: Form cumulative group IDs for consecutive events
        group_id = func.sum(down_events_cte.c.is_new_group).over(
            order_by=down_events_cte.c.start_time.asc()
        )

        grouped_cte = (
            select(
                down_events_cte.c.endpoint_id,
                down_events_cte.c.operational_state,
                down_events_cte.c.detailed_state,
                down_events_cte.c.start_time,
                down_events_cte.c.end_time,
                group_id.label("grp"),
            )
            .where(down_events_cte.c.operational_state == "DOWN")
            .cte("grouped_cte")
        )

        # Step 3: Determine peak severity state
        peak_case = case(
            (func.bool_or(grouped_cte.c.detailed_state == "DOWN"), "DOWN"),
            else_="DOWN-UNSTABLE",
        )

        incidents_agg_cte = (
            select(
                grouped_cte.c.endpoint_id,
                func.min(grouped_cte.c.start_time).label("incident_start"),
                func.max(grouped_cte.c.end_time).label("incident_end"),
                (func.count() * 60).label("duration_seconds"),
                func.count().label("contributing_event_count"),
                peak_case.label("peak_detailed_state"),
            )
            .group_by(grouped_cte.c.grp, grouped_cte.c.endpoint_id)
            .cte("incidents_agg_cte")
        )

        # Total incident count
        count_stmt = select(func.count()).select_from(incidents_agg_cte)
        try:
            total_count = (await self.session.execute(count_stmt)).scalar() or 0

            # Paginated query with SQL LIMIT and OFFSET
            paginated_stmt = (
                select(incidents_agg_cte)
                .order_by(incidents_agg_cte.c.incident_start.asc())
                .limit(limit)
                .offset(offset)
            )
            paginated_result = await self.session.execute(paginated_stmt)
            rows = paginated_result.all()

            records = [
                IncidentRecord(
                    endpoint_id=row.endpoint_id,
                    incident_start=row.incident_start,
                    incident_end=row.incident_end,
                    duration_seconds=int(row.duration_seconds),
                    peak_detailed_state=row.peak_detailed_state,
                    contributing_event_count=int(row.contributing_event_count),
                )
                for row in rows
            ]
            return records, total_count
        except Exception:
            # Fallback for environments lacking window function support
            raw_events = await self.get_uptime_events(endpoint_id, start_dt, end_dt)
            incidents = []
            curr = None
            for ev in raw_events:
                if ev.operational_state == "DOWN":
                    if curr is None:
                        curr = {
                            "start": ev.start_time,
                            "end": ev.end_time,
                            "peak": ev.detailed_state,
                            "count": 1,
                        }
                    else:
                        curr["end"] = ev.end_time
                        curr["count"] += 1
                        if ev.detailed_state == "DOWN":
                            curr["peak"] = "DOWN"
                else:
                    if curr is not None:
                        incidents.append(curr)
                        curr = None
            if curr is not None:
                incidents.append(curr)

            total = len(incidents)
            paged = incidents[offset : offset + limit]
            recs = [
                IncidentRecord(
                    endpoint_id=endpoint_id,
                    incident_start=inc["start"],
                    incident_end=inc["end"],
                    duration_seconds=inc["count"] * 60,
                    peak_detailed_state=inc["peak"],
                    contributing_event_count=inc["count"],
                )
                for inc in paged
            ]
            return recs, total

    async def get_events(
        self,
        endpoint_id: UUID,
        start_dt: datetime,
        end_dt: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[EndpointEvent], int]:
        """
        Fetch paginated transition events for an endpoint using database-level LIMIT and OFFSET.
        """
        count_stmt = (
            select(func.count())
            .select_from(EndpointEvent)
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.start_time >= start_dt,
                EndpointEvent.start_time <= end_dt,
            )
        )
        total_count = (await self.session.execute(count_stmt)).scalar() or 0

        stmt = (
            select(EndpointEvent)
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.start_time >= start_dt,
                EndpointEvent.start_time <= end_dt,
            )
            .order_by(EndpointEvent.start_time.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        events = result.scalars().all()
        return events, total_count
