from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence
from uuid import UUID

from app.core.orm import case, func, or_, select, update as sa_update
from app.core.orm import AsyncSession

from app.models.diagnostic_trace import EndpointDiagnosticTrace
from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.rca_incident import EndpointRCAIncident
from app.repositories.base import BaseRepository


class EndpointRepository(BaseRepository[Endpoint]):
    """
    Repository for Endpoint querying, creation, updating, and diagnostic relations.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Endpoint, session)

    async def get_by_id(
        self,
        endpoint_id: UUID,
        include_deleted: bool = False,
    ) -> Optional[Endpoint]:
        """Fetch endpoint by ID, optionally filtering out soft-deleted records."""
        stmt = select(Endpoint).where(Endpoint.id == endpoint_id)
        if not include_deleted:
            stmt = stmt.where(Endpoint.endpoint_status != "DELETED")
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_ip(
        self,
        ip_address: str,
        include_deleted: bool = False,
    ) -> Optional[Endpoint]:
        """Fetch endpoint by IP address."""
        stmt = select(Endpoint).where(Endpoint.ip_address == ip_address)
        if not include_deleted:
            stmt = stmt.where(Endpoint.endpoint_status != "DELETED")
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    @staticmethod
    def _list_filters(status: Optional[str]) -> list[Any]:
        """Filters shared by list_with_stats and count_listed: hide deleted rows, apply `status`.

        ACTIVE / DISABLED match endpoint_status; MONITORED / UNMONITORED match monitoring_enabled.
        """
        filters: list[Any] = [Endpoint.endpoint_status != "DELETED"]
        if status == "MONITORED":
            filters.append(Endpoint.monitoring_enabled == True)  # noqa: E712
        elif status == "UNMONITORED":
            filters.append(Endpoint.monitoring_enabled == False)  # noqa: E712
        elif status is not None:
            filters.append(Endpoint.endpoint_status == status)
        return filters

    async def count_listed(self, status: Optional[str] = None) -> int:
        """Total endpoints matching the list filters, ignoring pagination."""
        stmt = select(func.count(Endpoint.id)).where(*self._list_filters(status))
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def list_with_stats(
        self,
        status: Optional[str] = None,
        since_utc: Optional[datetime] = None,
        now_utc: Optional[datetime] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> Sequence[dict[str, Any]]:
        """
        List all active endpoints joined with latest operational state and 24h UP event counts.
        Supports pagination via page and page_size.
        """
        if now_utc is None:
            now_utc = datetime.now()
        if since_utc is None:
            since_utc = now_utc

        latest_event_sub = (
            select(
                EndpointEvent.endpoint_id,
                EndpointEvent.operational_state.label("current_operational_state"),
                EndpointEvent.detailed_state.label("current_detailed_state"),
                EndpointEvent.health_score.label("current_health_score"),
                EndpointEvent.avg_rtt_ms.label("avg_rtt_ms"),
                EndpointEvent.start_time.label("last_seen"),
            )
            .distinct(EndpointEvent.endpoint_id)
            .order_by(EndpointEvent.endpoint_id, EndpointEvent.start_time.desc())
            .subquery("latest_events")
        )

        clamped_start = func.greatest(EndpointEvent.start_time, since_utc)
        clamped_end = func.least(func.coalesce(EndpointEvent.end_time, now_utc), now_utc)
        duration_expr = func.greatest(0, func.extract("epoch", clamped_end - clamped_start))

        up_counts_sub = (
            select(
                EndpointEvent.endpoint_id,
                func.count().label("up_events_count"),
                func.coalesce(func.sum(duration_expr), 0).label("uptime_seconds"),
            )
            .where(
                EndpointEvent.operational_state == "UP",
                EndpointEvent.start_time < now_utc,
                or_(EndpointEvent.end_time.is_(None), EndpointEvent.end_time > since_utc),
            )
            .group_by(EndpointEvent.endpoint_id)
            .subquery("up_counts")
        )

        stmt = (
            select(
                Endpoint,
                case(
                    (Endpoint.monitoring_enabled.is_(False), "PASSIVE"),
                    else_=func.coalesce(latest_event_sub.c.current_operational_state, "DOWN"),
                ).label("current_operational_state"),
                case(
                    (Endpoint.monitoring_enabled.is_(False), "PASSIVE"),
                    else_=func.coalesce(latest_event_sub.c.current_detailed_state, "DOWN"),
                ).label("current_detailed_state"),
                case(
                    (Endpoint.monitoring_enabled.is_(False), 100.0),
                    else_=func.coalesce(latest_event_sub.c.current_health_score, 0.0),
                ).label("current_health_score"),
                latest_event_sub.c.avg_rtt_ms,
                latest_event_sub.c.last_seen,
                func.coalesce(up_counts_sub.c.up_events_count, 0).label(
                    "up_events_count"
                ),
                func.coalesce(up_counts_sub.c.uptime_seconds, 0).label(
                    "uptime_seconds"
                ),
            )
            .outerjoin(
                latest_event_sub, Endpoint.id == latest_event_sub.c.endpoint_id
            )
            .outerjoin(
                up_counts_sub, Endpoint.id == up_counts_sub.c.endpoint_id
            )
            .where(*self._list_filters(status))
            .order_by(Endpoint.hostname.asc())
        )

        if isinstance(page, int) and isinstance(page_size, int):
            offset = (page - 1) * page_size
            stmt = stmt.offset(offset).limit(page_size)

        result = await self.session.execute(stmt)
        rows = result.all()

        data = []
        for row in rows:
            ep = row[0]
            data.append({
                "id": ep.id,
                "hostname": ep.hostname,
                "ip_address": str(ep.ip_address),
                "device_type": ep.device_type,
                "location": ep.location,
                "description": ep.description,
                "endpoint_status": ep.endpoint_status,
                "monitoring_enabled": ep.monitoring_enabled,
                "device_role": getattr(ep, "device_role", "ACTIVE_HOST"),
                "allow_incident_trace": ep.allow_incident_trace,
                "allow_topology_discovery": ep.allow_topology_discovery,
                "enable_rca": ep.enable_rca,
                "enable_scheduled_discovery": ep.enable_scheduled_discovery,
                "is_l2_segment": ep.is_l2_segment,
                "flow_exporter_ips": [str(ip) for ip in (ep.flow_exporter_ips or [])],
                "flow_interface_aliases": getattr(ep, "flow_interface_aliases", {}) or {},
                "manual_parent_id": ep.manual_parent_id,
                "created_by": ep.created_by,
                "created_at": ep.created_at,
                "updated_at": ep.updated_at,
                "current_operational_state": row.current_operational_state,
                "current_detailed_state": row.current_detailed_state,
                "current_health_score": float(row.current_health_score),
                "avg_rtt_ms": float(row.avg_rtt_ms) if row.avg_rtt_ms is not None else None,
                "last_seen": row.last_seen,
                "uptime_seconds": int(row.uptime_seconds),
                "up_events_count": int(row.up_events_count),
            })
        return data

    async def get_detail_with_stats(
        self,
        endpoint_id: UUID,
        since_utc: datetime,
        now_utc: datetime,
    ) -> Optional[dict[str, Any]]:
        """Fetch a single endpoint joined with latest operational state and 24h UP event counts."""
        latest_event_sub = (
            select(
                EndpointEvent.endpoint_id,
                EndpointEvent.operational_state.label("current_operational_state"),
                EndpointEvent.detailed_state.label("current_detailed_state"),
                EndpointEvent.health_score.label("current_health_score"),
                EndpointEvent.avg_rtt_ms.label("avg_rtt_ms"),
                EndpointEvent.start_time.label("last_seen"),
            )
            .where(EndpointEvent.endpoint_id == endpoint_id)
            .order_by(EndpointEvent.start_time.desc())
            .limit(1)
            .subquery("latest_event")
        )

        clamped_start = func.greatest(EndpointEvent.start_time, since_utc)
        clamped_end = func.least(func.coalesce(EndpointEvent.end_time, now_utc), now_utc)
        duration_expr = func.greatest(0, func.extract("epoch", clamped_end - clamped_start))

        up_count_sub = (
            select(func.count().label("up_events_count"))
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.operational_state == "UP",
                EndpointEvent.start_time < now_utc,
                or_(EndpointEvent.end_time.is_(None), EndpointEvent.end_time > since_utc),
            )
            .scalar_subquery()
        )

        up_seconds_sub = (
            select(func.coalesce(func.sum(duration_expr), 0).label("uptime_seconds"))
            .where(
                EndpointEvent.endpoint_id == endpoint_id,
                EndpointEvent.operational_state == "UP",
                EndpointEvent.start_time < now_utc,
                or_(EndpointEvent.end_time.is_(None), EndpointEvent.end_time > since_utc),
            )
            .scalar_subquery()
        )

        stmt = (
            select(
                Endpoint,
                case(
                    (Endpoint.monitoring_enabled.is_(False), "PASSIVE"),
                    else_=func.coalesce(latest_event_sub.c.current_operational_state, "DOWN"),
                ).label("current_operational_state"),
                case(
                    (Endpoint.monitoring_enabled.is_(False), "PASSIVE"),
                    else_=func.coalesce(latest_event_sub.c.current_detailed_state, "DOWN"),
                ).label("current_detailed_state"),
                case(
                    (Endpoint.monitoring_enabled.is_(False), 100.0),
                    else_=func.coalesce(latest_event_sub.c.current_health_score, 0.0),
                ).label("current_health_score"),
                latest_event_sub.c.avg_rtt_ms,
                latest_event_sub.c.last_seen,
                func.coalesce(up_count_sub, 0).label("up_events_count"),
                func.coalesce(up_seconds_sub, 0).label("uptime_seconds"),
            )
            .outerjoin(
                latest_event_sub,
                Endpoint.id == latest_event_sub.c.endpoint_id,
            )
            .where(
                Endpoint.id == endpoint_id,
                Endpoint.endpoint_status != "DELETED",
            )
        )

        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return None

        ep = row[0]
        return {
            "id": ep.id,
            "hostname": ep.hostname,
            "ip_address": str(ep.ip_address),
            "device_type": ep.device_type,
            "location": ep.location,
            "description": ep.description,
            "endpoint_status": ep.endpoint_status,
            "monitoring_enabled": ep.monitoring_enabled,
            "device_role": getattr(ep, "device_role", "ACTIVE_HOST"),
            "allow_incident_trace": ep.allow_incident_trace,
            "allow_topology_discovery": ep.allow_topology_discovery,
            "enable_rca": ep.enable_rca,
            "enable_scheduled_discovery": ep.enable_scheduled_discovery,
            "is_l2_segment": ep.is_l2_segment,
            "flow_exporter_ips": [str(ip) for ip in (ep.flow_exporter_ips or [])],
            "flow_interface_aliases": getattr(ep, "flow_interface_aliases", {}) or {},
            "manual_parent_id": ep.manual_parent_id,
            "created_by": ep.created_by,
            "created_at": ep.created_at,
            "updated_at": ep.updated_at,
            "current_operational_state": row.current_operational_state,
            "current_detailed_state": row.current_detailed_state,
            "current_health_score": float(row.current_health_score),
            "avg_rtt_ms": float(row.avg_rtt_ms) if row.avg_rtt_ms is not None else None,
            "last_seen": row.last_seen,
            "uptime_seconds": int(row.uptime_seconds),
            "up_events_count": int(row.up_events_count),
        }

    async def create_endpoint(self, **data: Any) -> Endpoint:
        """Create a new endpoint entity."""
        endpoint = Endpoint(**data)
        self.session.add(endpoint)
        await self.session.flush()
        return endpoint

    async def restore_endpoint(
        self,
        endpoint_id: UUID,
        **updates: Any,
    ) -> Optional[Endpoint]:
        """Restore a soft-deleted endpoint with updated configuration."""
        updates["endpoint_status"] = "ACTIVE"
        updates["deleted_at"] = None
        updates["updated_at"] = func.now()
        stmt = (
            sa_update(Endpoint)
            .where(Endpoint.id == endpoint_id)
            .values(**updates)
            .returning(Endpoint)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_endpoint(
        self,
        endpoint_id: UUID,
        **updates: Any,
    ) -> Optional[Endpoint]:
        """Dynamically update endpoint attributes using SQLAlchemy update()."""
        if not updates:
            return await self.get_by_id(endpoint_id)
        if "updated_at" not in updates:
            updates["updated_at"] = func.now()
        stmt = (
            sa_update(Endpoint)
            .where(
                Endpoint.id == endpoint_id,
                Endpoint.endpoint_status != "DELETED",
            )
            .values(**updates)
            .returning(Endpoint)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def soft_delete_endpoint(
        self,
        endpoint_id: UUID,
        deleted_at: Optional[datetime] = None,
    ) -> Optional[Endpoint]:
        """Mark an endpoint as soft-deleted."""
        if deleted_at is None:
            deleted_at = datetime.now()
        stmt = (
            sa_update(Endpoint)
            .where(
                Endpoint.id == endpoint_id,
                Endpoint.endpoint_status != "DELETED",
            )
            .values(
                endpoint_status="DELETED",
                deleted_at=deleted_at,
                updated_at=deleted_at,
            )
            .returning(Endpoint)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def detect_parent_cycle(
        self,
        endpoint_id: UUID,
        parent_id: Optional[UUID],
    ) -> bool:
        """
        Check whether linking endpoint_id to parent_id would create a cyclic dependency.
        Returns True if a cycle is detected, False otherwise.
        """
        if not parent_id:
            return False
        if str(endpoint_id) == str(parent_id):
            return True

        curr: Optional[UUID] = parent_id
        visited: set[str] = {str(endpoint_id)}

        while curr:
            curr_str = str(curr)
            if curr_str in visited:
                return True
            visited.add(curr_str)

            stmt = select(Endpoint.manual_parent_id).where(Endpoint.id == curr)
            res = await self.session.execute(stmt)
            curr = res.scalar_one_or_none()

        return False

    async def get_traces(
        self,
        endpoint_id: UUID,
        limit: int = 10,
    ) -> Sequence[EndpointDiagnosticTrace]:
        """Fetch historical diagnostic traces for an endpoint."""
        stmt = (
            select(EndpointDiagnosticTrace)
            .where(EndpointDiagnosticTrace.endpoint_id == endpoint_id)
            .order_by(EndpointDiagnosticTrace.timestamp.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_latest_rca(
        self,
        endpoint_id: UUID,
    ) -> Optional[EndpointRCAIncident]:
        """Fetch the most recent RCA incident for an endpoint."""
        stmt = (
            select(EndpointRCAIncident)
            .where(EndpointRCAIncident.endpoint_id == endpoint_id)
            .order_by(EndpointRCAIncident.incident_timestamp.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
