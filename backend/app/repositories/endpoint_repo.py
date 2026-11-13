from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple
from uuid import UUID

from app.core.orm import case, func, or_, select, text, update as sa_update
from app.core.orm import AsyncSession

from app.models.diagnostic_trace import EndpointDiagnosticTrace
from app.models.endpoint import Endpoint
from app.models.endpoint_event import EndpointEvent
from app.models.rca_incident import EndpointRCAIncident
from app.repositories.base import BaseRepository
from app.services.endpoint_filter import EndpointFilter, MAX_TAGS_PER_ENDPOINT, conditions as filter_conditions


def _uptime_seconds_expr(
    clamped_start: Any, clamped_end: Any, gaps: Optional[List[Tuple[datetime, datetime]]]
) -> Any:
    """Seconds of one period inside the window, minus its overlap with each monitoring gap.

    A database outage leaves the open period running across its gap; that time is unknown, not
    up (as on the Reports page and in the PDF). `gaps` must be merged (merge_intervals) so no
    second is taken off twice; there are only a handful in a 24 h window."""
    seconds = func.extract("epoch", clamped_end - clamped_start)
    for g_start, g_end in gaps or []:
        seconds = seconds - func.greatest(
            0, func.extract("epoch", func.least(clamped_end, g_end) - func.greatest(clamped_start, g_start))
        )
    return func.greatest(0, seconds)


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
    def _list_filters(status: Optional[str], endpoint_filter: Optional[EndpointFilter] = None) -> list[Any]:
        """Filters shared by list_with_stats and count_listed: hide deleted rows, apply the shared
        endpoint filter (services/endpoint_filter.py) and `status`.

        ACTIVE / DISABLED match endpoint_status; MONITORED / UNMONITORED match monitoring_enabled.
        """
        filters: list[Any] = filter_conditions(endpoint_filter or EndpointFilter())
        if status == "MONITORED":
            filters.append(Endpoint.monitoring_enabled == True)  # noqa: E712
        elif status == "UNMONITORED":
            filters.append(Endpoint.monitoring_enabled == False)  # noqa: E712
        elif status is not None:
            filters.append(Endpoint.endpoint_status == status)
        return filters

    async def count_listed(
        self, status: Optional[str] = None, endpoint_filter: Optional[EndpointFilter] = None
    ) -> int:
        """Total endpoints matching the list filters, ignoring pagination."""
        stmt = select(func.count(Endpoint.id)).where(*self._list_filters(status, endpoint_filter))
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def list_with_stats(
        self,
        status: Optional[str] = None,
        since_utc: Optional[datetime] = None,
        now_utc: Optional[datetime] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
        gaps: Optional[List[Tuple[datetime, datetime]]] = None,
        endpoint_filter: Optional[EndpointFilter] = None,
    ) -> Sequence[dict[str, Any]]:
        """
        List all active endpoints joined with latest operational state and 24h UP event counts.
        Supports pagination via page and page_size.

        Current state is the endpoint's latest event row. An endpoint with no events yet shows as
        DOWN, and one with monitoring off as PASSIVE. uptime_seconds is UP time in the window
        without the time inside the merged monitoring `gaps`, which the uptime % also leaves out
        of its denominator.
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
                # When the current state began: a 24-hour split or a restart continues it.
                func.coalesce(EndpointEvent.state_since, EndpointEvent.start_time).label("last_seen"),
            )
            .distinct(EndpointEvent.endpoint_id)
            .order_by(EndpointEvent.endpoint_id, EndpointEvent.start_time.desc())
            .subquery("latest_events")
        )

        clamped_start = func.greatest(EndpointEvent.start_time, since_utc)
        clamped_end = func.least(func.coalesce(EndpointEvent.end_time, now_utc), now_utc)
        duration_expr = _uptime_seconds_expr(clamped_start, clamped_end, gaps)

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
            .where(*self._list_filters(status, endpoint_filter))
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
                "site": getattr(ep, "site", None),
                "location": ep.location,
                "tags": list(getattr(ep, "tags", None) or []),
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
                "snmp_credential_id": getattr(ep, "snmp_credential_id", None),
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
        gaps: Optional[List[Tuple[datetime, datetime]]] = None,
    ) -> Optional[dict[str, Any]]:
        """Fetch a single endpoint joined with latest operational state and 24h UP event counts.
        Monitoring-gap time is left out of uptime_seconds, as in list_with_stats."""
        latest_event_sub = (
            select(
                EndpointEvent.endpoint_id,
                EndpointEvent.operational_state.label("current_operational_state"),
                EndpointEvent.detailed_state.label("current_detailed_state"),
                EndpointEvent.health_score.label("current_health_score"),
                EndpointEvent.avg_rtt_ms.label("avg_rtt_ms"),
                # When the current state began: a 24-hour split or a restart continues it.
                func.coalesce(EndpointEvent.state_since, EndpointEvent.start_time).label("last_seen"),
            )
            .where(EndpointEvent.endpoint_id == endpoint_id)
            .order_by(EndpointEvent.start_time.desc())
            .limit(1)
            .subquery("latest_event")
        )

        clamped_start = func.greatest(EndpointEvent.start_time, since_utc)
        clamped_end = func.least(func.coalesce(EndpointEvent.end_time, now_utc), now_utc)
        duration_expr = _uptime_seconds_expr(clamped_start, clamped_end, gaps)

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
            "site": getattr(ep, "site", None),
            "location": ep.location,
            "tags": list(getattr(ep, "tags", None) or []),
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
            "snmp_credential_id": getattr(ep, "snmp_credential_id", None),
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

    # --- site, location and tags (v3.3.6) -------------------------------------------------

    async def canonical_label(self, column: str, value: Optional[str]) -> Optional[str]:
        """The spelling already in use for `value` in `column` ("site" or "location"), or the
        value itself. Typing "abuja" where "Abuja" exists stores "Abuja", so case alone never
        splits one site into two. The spelling most endpoints already use wins."""
        if value is None or column not in ("site", "location"):
            return value
        res = await self.session.execute(
            text(f"""
                SELECT mode() WITHIN GROUP (ORDER BY {column}) FROM endpoints
                WHERE lower({column}) = lower(CAST(:v AS text)) AND endpoint_status != 'DELETED'
            """),
            {"v": value},
        )
        existing = res.scalar()
        return existing or value

    async def label_suggestions(self, site: Optional[str] = None) -> dict[str, list[dict[str, Any]]]:
        """Values in use, with how many endpoints carry each, for autocomplete and the filter bar:
        sites, locations (only those within `site` when given), tags and device types. One
        spelling per value (the most common), as matching ignores case."""
        site_clause = "AND lower(site) = lower(CAST(:site AS text))" if site else ""
        params = {"site": site} if site else {}

        async def grouped(column: str, extra: str = "") -> list[dict[str, Any]]:
            res = await self.session.execute(
                text(f"""
                    SELECT mode() WITHIN GROUP (ORDER BY {column}) AS value, COUNT(*) AS n
                    FROM endpoints
                    WHERE endpoint_status != 'DELETED' AND {column} IS NOT NULL AND {column} <> '' {extra}
                    GROUP BY lower({column})
                    ORDER BY lower(mode() WITHIN GROUP (ORDER BY {column}))
                """),
                params if extra else {},
            )
            return [{"value": r[0], "count": int(r[1])} for r in res.fetchall()]

        tags_res = await self.session.execute(
            text("""
                SELECT t AS value, COUNT(*) AS n
                FROM endpoints, unnest(tags) AS t
                WHERE endpoint_status != 'DELETED'
                GROUP BY t ORDER BY t
            """)
        )
        return {
            "sites": await grouped("site"),
            "locations": await grouped("location", site_clause),
            "tags": [{"value": r[0], "count": int(r[1])} for r in tags_res.fetchall()],
            "device_types": await grouped("device_type"),
        }

    async def bulk_assign(
        self,
        endpoint_ids: Sequence[UUID],
        changes: dict[str, Any],
    ) -> int:
        """Applies `changes` to the given endpoints: "site" / "location" (a value, or None to
        clear), "add_tags" / "remove_tags" (normalised lists), "snmp_credential_id" (a set's
        id, or None for SNMP off; the caller checks the set exists). Returns how many endpoints
        changed. Raises ValueError when adding tags would take an endpoint past the limit, before
        anything is written."""
        # text() parameters: a tuple goes to PostgreSQL as an array, a list as JSON
        # (core/orm.prepare_bind_param), so arrays are passed as tuples here.
        ids = tuple(endpoint_ids)
        if not ids:
            return 0
        set_parts = []
        params: dict[str, Any] = {"ids": ids}
        for column in ("site", "location"):
            if column in changes:
                set_parts.append(f"{column} = CAST(:{column} AS varchar)")
                params[column] = changes[column]
        if "snmp_credential_id" in changes:
            set_parts.append("snmp_credential_id = CAST(:snmp_credential_id AS uuid)")
            cred = changes["snmp_credential_id"]
            params["snmp_credential_id"] = str(cred) if cred else None

        add = list(changes.get("add_tags") or [])
        remove = set(changes.get("remove_tags") or [])
        new_tags: dict[str, list[str]] = {}
        if add or remove:
            res = await self.session.execute(
                text("SELECT id, tags FROM endpoints WHERE id = ANY(CAST(:ids AS uuid[])) AND endpoint_status != 'DELETED'"),
                {"ids": ids},
            )
            for row_id, tags in res.fetchall():
                current = list(tags or [])
                updated = [t for t in current if t not in remove]
                updated += [t for t in add if t not in updated and t not in remove]
                if len(updated) > MAX_TAGS_PER_ENDPOINT:
                    raise ValueError(f"An endpoint can have at most {MAX_TAGS_PER_ENDPOINT} tags.")
                if updated != current:
                    new_tags[str(row_id)] = updated

        touched: set[str] = set()
        if set_parts:
            res = await self.session.execute(
                text(f"""
                    UPDATE endpoints SET {", ".join(set_parts)}, updated_at = NOW()
                    WHERE id = ANY(CAST(:ids AS uuid[])) AND endpoint_status != 'DELETED'
                    RETURNING id
                """),
                params,
            )
            touched.update(str(r[0]) for r in res.fetchall())
        # Per endpoint: fleets are small, and each keeps its own tag order.
        for row_id, tags in new_tags.items():
            await self.session.execute(
                text("UPDATE endpoints SET tags = CAST(:tags AS text[]), updated_at = NOW() WHERE id = CAST(:id AS uuid)"),
                {"tags": tuple(tags), "id": row_id},
            )
            touched.add(row_id)
        return len(touched)

    async def rename_label(
        self, field: str, old: str, new: Optional[str], within_site: Optional[str] = None
    ) -> int:
        """Renames a site, location or tag on every endpoint carrying it (case-insensitive match),
        which also merges two spellings into one. `new` None removes it. A location can be
        renamed within one site only. Returns how many endpoints changed."""
        if field in ("site", "location"):
            site_clause = ""
            params: dict[str, Any] = {"old": old, "new": new}
            if field == "location" and within_site:
                site_clause = "AND lower(site) = lower(CAST(:site AS text))"
                params["site"] = within_site
            res = await self.session.execute(
                text(f"""
                    UPDATE endpoints SET {field} = CAST(:new AS varchar), updated_at = NOW()
                    WHERE lower({field}) = lower(CAST(:old AS text)) AND endpoint_status != 'DELETED'
                      {site_clause}
                    RETURNING id
                """),
                params,
            )
            return len(res.fetchall())

        # Tags: replace in place, keeping order, without duplicating a tag the endpoint has.
        res = await self.session.execute(
            text("""
                SELECT id, tags FROM endpoints
                WHERE CAST(:old AS text) = ANY(tags) AND endpoint_status != 'DELETED'
            """),
            {"old": old},
        )
        changed = 0
        for row_id, tags in res.fetchall():
            updated: list[str] = []
            for t in tags or []:
                t = (new if t == old else t)
                if t and t not in updated:
                    updated.append(t)
            await self.session.execute(
                text("UPDATE endpoints SET tags = CAST(:tags AS text[]), updated_at = NOW() WHERE id = CAST(:id AS uuid)"),
                {"tags": tuple(updated), "id": row_id},
            )
            changed += 1
        return changed

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
        """Update the given columns of a non-deleted endpoint (app.core.orm update()), setting
        updated_at unless it is passed."""
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

        Walks up from parent_id through each manual_parent_id; reaching endpoint_id (or any
        endpoint twice) means a loop.
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
