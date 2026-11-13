"""Endpoint routes: list, detail, probe history, create, update, delete, and route discovery.

Site, location and tags (v3.3.6) are optional free text with autocomplete (GET /suggestions).
Every list here takes the shared filter (?site=&location=&tag=&type=&q=, services/
endpoint_filter.py); GET /match gives just the matching ids, which the dashboard and the map use.
Admins can assign them to many endpoints at once (POST /bulk-update) and rename or merge a value
everywhere (POST /rename); both are audited.

Deleting is soft: the row is marked DELETED and keeps its history. Adding an endpoint with the
IP of a deleted one restores that row (its history comes back) rather than making a new one;
ip_address is unique, so a second row with the same address can't exist anyway. The engine
picks up added, changed or removed endpoints within 30 s on its own.
"""
from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query, Request
from app.core.background import spawn
from app.core.schema import BaseModel, ConfigDict, field_validator
from app.core.orm import AsyncSession

from app.database import get_db
from app.repositories.auth_repo import AuthRepository
from app.repositories.endpoint_repo import EndpointRepository
from app.services import probe_history
from app.services import snmp_credentials
from app.services.endpoint_filter import (
    EndpointFilter,
    filter_from_body,
    filter_from_request,
    matching_ids,
    normalise_label,
    normalise_tag,
    normalise_tags,
    or_empty,
    resolve_scope,
)
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse, PaginationMeta
from app.services.uptime_calculator import (
    calculate_uptime_denominator_and_percentage,
    get_service_gap_intervals,
    get_unknown_seconds_for_period,
    merge_intervals,
)

logger = logging.getLogger(__name__)


async def _bg_run_initial_discovery(endpoint_id: UUID, ip_address: str) -> None:
    """After an endpoint is added or restored: traceroute to it to record its baseline route
    (what root-cause analysis compares against later), then rebuild the topology map."""
    from app.database import AsyncSessionLocal
    from app.services.baseline_route import refresh_baseline_route
    from app.services.topology import topology_manager

    async with AsyncSessionLocal() as bg_db:
        try:
            await refresh_baseline_route(endpoint_id, ip_address, db=bg_db, trigger="added")
            await bg_db.commit()
            await topology_manager.full_rebuild(bg_db)
        except Exception as exc:
            logger.error(
                "Background initial discovery failed for endpoint %s: %s",
                endpoint_id,
                exc,
            )


async def endpoint_filter_dep(request: Request) -> EndpointFilter:
    """The shared filter from the query string, for any route that lists endpoints."""
    return filter_from_request(request)


async def _place_fields(repo: EndpointRepository, request: Any, fields_set: set) -> dict:
    """Site, location and tags from a create or update request, normalised: trimmed, an empty
    value cleared (None), a site or location typed in another case stored in the spelling
    already in use, tags lower case and deduplicated. Only fields the request sent are returned.
    A tag that can't exist answers 400."""
    out: dict = {}
    for column in ("site", "location"):
        if column in fields_set:
            value = normalise_label(getattr(request, column))
            # Empty clears the field; only a real value needs the spelling lookup.
            out[column] = await repo.canonical_label(column, value) if value else None
    if "tags" in fields_set and request.tags is not None:
        try:
            out["tags"] = normalise_tags(request.tags)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return out


class CreateEndpointRequest(BaseModel):
    ip_address: str
    hostname: str
    device_type: str
    site: Optional[str] = None
    location: Optional[str] = None
    tags: Optional[list[str]] = None
    description: Optional[str] = None
    monitoring_enabled: bool = True
    allow_incident_trace: bool = True
    allow_topology_discovery: bool = True
    enable_rca: bool = True
    enable_scheduled_discovery: bool = True
    is_l2_segment: bool = False
    flow_exporter_ips: Optional[list[str]] = None
    flow_interface_aliases: Optional[dict[str, str]] = None
    manual_parent_id: Optional[UUID] = None
    # SNMP credential set (v3.3.6); None: no SNMP.
    snmp_credential_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("ip_address")
    @classmethod
    def validate_ip_format(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("IP address cannot be empty.")
        clean_ip = v.strip()
        try:
            ipaddress.ip_address(clean_ip)
        except ValueError:
            raise ValueError(f"'{clean_ip}' is not a valid IPv4 or IPv6 address.")
        return clean_ip


class UpdateEndpointRequest(BaseModel):
    hostname: Optional[str] = None
    device_type: Optional[str] = None
    # Sent as "" or null: cleared. Left out: unchanged.
    site: Optional[str] = None
    location: Optional[str] = None
    tags: Optional[list[str]] = None
    description: Optional[str] = None
    monitoring_enabled: Optional[bool] = None
    allow_incident_trace: Optional[bool] = None
    allow_topology_discovery: Optional[bool] = None
    enable_rca: Optional[bool] = None
    enable_scheduled_discovery: Optional[bool] = None
    is_l2_segment: Optional[bool] = None
    flow_exporter_ips: Optional[list[str]] = None
    flow_interface_aliases: Optional[dict[str, str]] = None
    manual_parent_id: Optional[UUID] = None
    # Sent as null: SNMP off for this endpoint. Left out: unchanged.
    snmp_credential_id: Optional[UUID] = None
    endpoint_status: Optional[Literal["ACTIVE", "DISABLED"]] = None

    model_config = ConfigDict(from_attributes=True)


router = APIRouter(prefix="/endpoints", tags=["endpoints"])


@router.get("/{id}/traces", response_model=APIResponse)
async def get_endpoint_traces(
    id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = EndpointRepository(db)
    traces = await repo.get_traces(id, limit=10)

    trace_list = []
    for t in traces:
        t_data = t.trace_data
        ts_str = t.timestamp.isoformat() if t.timestamp else None
        trace_list.append({
            "id": str(t.id),
            "endpoint_id": str(t.endpoint_id),
            "timestamp": ts_str,
            "trigger_reason": t.trigger_reason,
            "trace_data": t_data,
        })
    return APIResponse.success(data=trace_list)


@router.get("/{id}/rca", response_model=APIResponse)
async def get_endpoint_rca(
    id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    repo = EndpointRepository(db)
    rca = await repo.get_latest_rca(id)
    if not rca:
        return APIResponse.success(data=None)

    incident_data = {
        "id": str(rca.id),
        "endpoint_id": str(rca.endpoint_id),
        "incident_timestamp": (
            rca.incident_timestamp.isoformat()
            if rca.incident_timestamp
            else None
        ),
        "status_at_execution": rca.status_at_execution,
        "failed_hop_number": rca.failed_hop_number,
        "failed_hop_ip": rca.failed_hop_ip,
        "last_known_good_hop_ip": rca.last_known_good_hop_ip,
        "rca_summary": rca.rca_summary,
        "baseline_snapshot": rca.baseline_snapshot,
        "failure_trace_snapshot": rca.failure_trace_snapshot,
        "flow_evidence": getattr(rca, "flow_evidence", None),
        "is_resolved": rca.is_resolved,
    }
    return APIResponse.success(data=incident_data)


@router.get("/{id}/flow-paths", response_model=APIResponse)
async def get_endpoint_flow_paths(
    id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Where exporters forwarded this endpoint's traffic (last 24 h) and route changes (7 days)."""
    from app.services.flow_paths import endpoint_flow_paths

    return APIResponse.success(data=await endpoint_flow_paths(db, id))


@router.get("/", response_model=APIResponse)
async def list_endpoints(
    status: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=500),
    endpoint_filter: EndpointFilter = Depends(endpoint_filter_dep),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    endpoint_filter = or_empty(endpoint_filter)
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(hours=24)

    # ACTIVE / DISABLED filter on endpoint_status, MONITORED / UNMONITORED on monitoring_enabled.
    ALLOWED_STATUSES = {"ACTIVE", "DISABLED", "MONITORED", "UNMONITORED"}
    clean_status = None
    if status is not None:
        clean_status = status.strip().upper()
        if clean_status not in ALLOWED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status}'. Must be one of: {', '.join(sorted(ALLOWED_STATUSES))}",
            )

    # Paging is optional: with neither page nor page_size every endpoint comes back at once.
    calc_page = page if isinstance(page, int) else None
    calc_page_size = page_size if isinstance(page_size, int) else None
    if calc_page is not None and calc_page_size is None:
        calc_page_size = 50
    elif calc_page_size is not None and calc_page is None:
        calc_page = 1

    # Time inside monitoring gaps is unknown: left out of both the UP seconds and the window.
    gap_intervals = await get_service_gap_intervals(db, since_utc, now_utc)
    repo = EndpointRepository(db)
    rows = await repo.list_with_stats(
        status=clean_status,
        since_utc=since_utc,
        now_utc=now_utc,
        page=calc_page,
        page_size=calc_page_size,
        gaps=merge_intervals(gap_intervals),
        endpoint_filter=endpoint_filter,
    )

    data = []
    for row in rows:
        created_at = row["created_at"]
        uptime_percentage = calculate_uptime_denominator_and_percentage(
            created_at=created_at,
            start_time=since_utc,
            end_time=now_utc,
            now_utc=now_utc,
            up_events_count=row["up_events_count"],
            gap_intervals=gap_intervals,
            uptime_seconds=row.get("uptime_seconds"),
        )
        data.append({
            "id": str(row["id"]),
            "hostname": row["hostname"],
            "ip_address": row["ip_address"],
            "device_type": row["device_type"],
            "site": row.get("site"),
            "location": row["location"],
            "tags": list(row.get("tags") or []),
            "endpoint_status": row["endpoint_status"],
            "monitoring_enabled": row["monitoring_enabled"],
            "allow_incident_trace": row["allow_incident_trace"],
            "allow_topology_discovery": row["allow_topology_discovery"],
            "enable_rca": row["enable_rca"],
            "enable_scheduled_discovery": row["enable_scheduled_discovery"],
            "is_l2_segment": row["is_l2_segment"],
            "manual_parent_id": (
                str(row["manual_parent_id"]) if row["manual_parent_id"] else None
            ),
            "snmp_credential_id": str(row["snmp_credential_id"]) if row.get("snmp_credential_id") else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "current_operational_state": row["current_operational_state"],
            "current_detailed_state": row["current_detailed_state"],
            "current_health_score": row["current_health_score"],
            "avg_rtt_ms": (
                float(row["avg_rtt_ms"]) if row.get("avg_rtt_ms") is not None else None
            ),
            # When the current state began (not the last probe): see EndpointRepository.
            "last_seen": row["last_seen"],
            "uptime_percentage_24h": uptime_percentage,
        })

    if calc_page is not None and calc_page_size is not None:
        total = await repo.count_listed(status=clean_status, endpoint_filter=endpoint_filter)
        meta = PaginationMeta(
            total=total,
            page=calc_page,
            page_size=calc_page_size,
            total_pages=max(1, -(-total // calc_page_size)),
        )
    else:
        meta = PaginationMeta(
            total=len(rows),
            page=1,
            page_size=max(1, len(rows)),
            total_pages=1,
        )
    return APIResponse.success(data=data, meta=meta)


@router.get("/match", response_model=APIResponse)
async def match_endpoints(
    endpoint_filter: EndpointFilter = Depends(endpoint_filter_dep),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ids of the endpoints the filter matches, plus the filter as the server read it and a
    one-line description. The dashboard and the map keep their full endpoint list (for live
    updates and whole-fleet counts) and use this to know which ones are in the filter."""
    endpoint_filter = or_empty(endpoint_filter)
    ids = await matching_ids(db, endpoint_filter)
    return APIResponse.success(data={
        "ids": [str(i) for i in ids],
        "total": len(ids),
        "filter": endpoint_filter.as_dict(),
        "description": endpoint_filter.describe(),
    })


@router.get("/suggestions", response_model=APIResponse)
async def endpoint_suggestions(
    site: Optional[str] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sites, locations, tags and device types in use, each with its endpoint count, for
    autocomplete and the filter bar. With `site`, only the locations used within that site."""
    data = await EndpointRepository(db).label_suggestions(site=normalise_label(site))
    return APIResponse.success(data=data)


class BulkUpdateRequest(BaseModel):
    # Which endpoints: a list, a filter, or both (the listed ones the filter matches).
    endpoint_ids: Optional[list[UUID]] = None
    filter: Optional[dict] = None
    # What to change. A field left out is unchanged; site / location sent as "" or null clear it.
    site: Optional[str] = None
    location: Optional[str] = None
    add_tags: Optional[list[str]] = None
    remove_tags: Optional[list[str]] = None
    # SNMP credential set for all of them (v3.3.6); sent as null: SNMP off. Left out: unchanged.
    snmp_credential_id: Optional[UUID] = None

    model_config = ConfigDict(from_attributes=True)


@router.post("/bulk-update", response_model=APIResponse)
async def bulk_update_endpoints(
    request: BulkUpdateRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Sets site or location, or adds or removes tags, on many endpoints at once (bulk assign in
    the dashboard table). One audit entry records the change and the endpoints it touched."""
    endpoint_filter = filter_from_body(request.filter)
    if not request.endpoint_ids and (endpoint_filter is None or endpoint_filter.is_empty()):
        # An empty filter would mean "every endpoint": too easy to send by mistake.
        raise HTTPException(status_code=400, detail="Choose endpoints or a filter.")
    ids = await resolve_scope(db, endpoint_filter, request.endpoint_ids)

    repo = EndpointRepository(db)
    changes = await _place_fields(repo, request, request.model_fields_set & {"site", "location"})
    try:
        if request.add_tags:
            changes["add_tags"] = normalise_tags(request.add_tags)
        if request.remove_tags:
            changes["remove_tags"] = [t for t in (normalise_tag(x) for x in request.remove_tags) if t]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if "snmp_credential_id" in request.model_fields_set:
        if request.snmp_credential_id:
            await snmp_credentials.require_set(db, request.snmp_credential_id)
        changes["snmp_credential_id"] = str(request.snmp_credential_id) if request.snmp_credential_id else None
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing to change.")

    try:
        changed = await repo.bulk_assign(ids, changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await AuthRepository(db).create_audit_log(
        user_id=_admin_uuid(current_user),
        action="ENDPOINT:BULK_UPDATE",
        target_type="endpoints",
        target_id=FLEET_TARGET_ID,
        details={
            "changes": changes,
            "matched": len(ids),
            "changed": changed,
            "filter": endpoint_filter.as_dict() if endpoint_filter else None,
            "endpoint_ids": [str(i) for i in ids][:500],
        },
    )
    await db.commit()
    return APIResponse.success(data={"matched": len(ids), "changed": changed})


class RenameRequest(BaseModel):
    field: Literal["site", "location", "tag"]
    # Matched case-insensitively. "to" empty removes the value from every endpoint.
    from_value: str
    to_value: Optional[str] = None
    # Location only: rename it within this site (other sites' locations of the same name stay).
    site: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


@router.post("/rename", response_model=APIResponse)
async def rename_endpoint_label(
    request: RenameRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Renames a site, location or tag on every endpoint that has it: fixes a misspelling, or
    merges two spellings into one (rename the wrong one to the right one). Audited."""
    try:
        if request.field == "tag":
            old = normalise_tag(request.from_value)
            new = normalise_tag(request.to_value)
        else:
            old = normalise_label(request.from_value)
            new = normalise_label(request.to_value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not old:
        raise HTTPException(status_code=400, detail="Say which value to rename.")

    repo = EndpointRepository(db)
    if new and request.field != "tag":
        # Merging into an existing value takes that value's spelling.
        existing = await repo.canonical_label(request.field, new)
        if existing.lower() != old.lower():
            new = existing
    changed = await repo.rename_label(
        request.field, old, new,
        within_site=normalise_label(request.site) if request.field == "location" else None,
    )
    await AuthRepository(db).create_audit_log(
        user_id=_admin_uuid(current_user),
        action="ENDPOINT:RENAME_LABEL",
        target_type="endpoints",
        target_id=FLEET_TARGET_ID,
        details={"field": request.field, "from": old, "to": new, "site": request.site, "changed": changed},
    )
    await db.commit()
    return APIResponse.success(data={"changed": changed, "to": new})


# audit_logs.target_id is NOT NULL. Bulk assign and rename act on many endpoints, so they
# record the nil UUID as their target and list the endpoints in the details instead.
FLEET_TARGET_ID = UUID(int=0)


def _admin_uuid(current_user: dict) -> Optional[UUID]:
    try:
        return UUID(str(current_user.get("sub")))
    except (ValueError, TypeError):
        return None


@router.get("/probe-summary", response_model=APIResponse)
async def get_probe_summary(
    hours: int = Query(default=24, ge=1, le=168),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Per-endpoint loss %, mean and p95 RTT, jitter, last probe time and an hourly RTT sparkline
    over the last `hours`, from the per-cycle probe history. Endpoints without samples (passive,
    paused or not yet probed) are absent."""
    return APIResponse.success(data=await probe_history.probe_summary(db, hours=hours))


@router.get("/{endpoint_id}/probe-series", response_model=APIResponse)
async def get_probe_series(
    endpoint_id: UUID,
    start_date: Optional[datetime] = Query(default=None),
    end_date: Optional[datetime] = Query(default=None),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """RTT min/avg/max, jitter and loss over time for one endpoint (default: last 24 hours).

    Resolution follows the span: per cycle up to 2 days, 10-minute buckets up to 8 days, hourly
    beyond that."""
    end = end_date or datetime.now(timezone.utc)
    start = start_date or (end - timedelta(hours=24))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start >= end:
        raise HTTPException(status_code=400, detail="start_date must be before end_date.")
    return APIResponse.success(data=await probe_history.probe_series(db, endpoint_id, start, end))


@router.get("/{endpoint_id}", response_model=APIResponse)
async def get_endpoint(
    endpoint_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(hours=24)

    gap_intervals = await get_service_gap_intervals(db, since_utc, now_utc)
    repo = EndpointRepository(db)
    row = await repo.get_detail_with_stats(
        endpoint_id=endpoint_id,
        since_utc=since_utc,
        now_utc=now_utc,
        gaps=merge_intervals(gap_intervals),
    )
    if not row:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    uptime_percentage = calculate_uptime_denominator_and_percentage(
        created_at=row["created_at"],
        start_time=since_utc,
        end_time=now_utc,
        now_utc=now_utc,
        up_events_count=row["up_events_count"],
        gap_intervals=gap_intervals,
        uptime_seconds=row.get("uptime_seconds"),
    )

    data = {
        "id": str(row["id"]),
        "hostname": row["hostname"],
        "ip_address": row["ip_address"],
        "device_type": row["device_type"],
        "site": row.get("site"),
        "location": row["location"],
        "tags": list(row.get("tags") or []),
        "description": row["description"],
        "monitoring_enabled": row["monitoring_enabled"],
        "device_role": row.get("device_role") or "ACTIVE_HOST",
        "flow_exporter_ips": list(row.get("flow_exporter_ips") or []),
        "flow_interface_aliases": row.get("flow_interface_aliases") or {},
        "allow_incident_trace": row["allow_incident_trace"],
        "allow_topology_discovery": row["allow_topology_discovery"],
        "enable_rca": row["enable_rca"],
        "enable_scheduled_discovery": row["enable_scheduled_discovery"],
        "is_l2_segment": row["is_l2_segment"],
        "manual_parent_id": (
            str(row["manual_parent_id"]) if row["manual_parent_id"] else None
        ),
        "snmp_credential_id": str(row["snmp_credential_id"]) if row.get("snmp_credential_id") else None,
        "endpoint_status": row["endpoint_status"],
        "created_by": str(row["created_by"]) if row["created_by"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "current_operational_state": row["current_operational_state"],
        "current_detailed_state": row["current_detailed_state"],
        "current_health_score": row["current_health_score"],
        "avg_rtt_ms": (
            float(row["avg_rtt_ms"]) if row.get("avg_rtt_ms") is not None else None
        ),
        "last_seen": row["last_seen"],
        "uptime_percentage_24h": uptime_percentage,
    }
    return APIResponse.success(data=data)


@router.post("/", response_model=APIResponse, status_code=201)
async def create_endpoint(
    request: CreateEndpointRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    repo = EndpointRepository(db)
    auth_repo = AuthRepository(db)
    place = await _place_fields(repo, request, {"site", "location", "tags"})

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    # Check for active existing endpoint
    existing_active = await repo.get_by_ip(request.ip_address, include_deleted=False)
    if existing_active:
        raise HTTPException(
            status_code=409,
            detail="An endpoint with this IP address already exists.",
        )

    # Check for soft-deleted endpoint to restore: same row, so its history comes back too.
    existing_deleted = await repo.get_by_ip(request.ip_address, include_deleted=True)
    if existing_deleted and getattr(existing_deleted, "endpoint_status", "") == "DELETED":
        restored = await repo.restore_endpoint(
            endpoint_id=existing_deleted.id,
            hostname=request.hostname,
            device_type=request.device_type,
            site=place.get("site"),
            location=place.get("location"),
            tags=place.get("tags") or [],
            description=request.description,
            monitoring_enabled=request.monitoring_enabled,
            allow_incident_trace=request.allow_incident_trace,
            allow_topology_discovery=request.allow_topology_discovery,
            enable_rca=request.enable_rca,
            enable_scheduled_discovery=request.enable_scheduled_discovery,
            is_l2_segment=request.is_l2_segment,
            manual_parent_id=request.manual_parent_id,
            # An UPDATE writes None as NULL, and both columns are NOT NULL. The add form never
            # sends them, so re-adding a deleted endpoint failed with a 500.
            flow_exporter_ips=request.flow_exporter_ips or [],
            flow_interface_aliases=request.flow_interface_aliases or {},
        )
        await auth_repo.create_audit_log(
            user_id=admin_uuid,
            action="ENDPOINT:RESTORE",
            target_type="endpoints",
            target_id=existing_deleted.id,
            details={
                "ip_address": request.ip_address,
                "hostname": request.hostname,
                "note": "Restored soft-deleted endpoint",
            },
        )
        await db.commit()
        spawn(_bg_run_initial_discovery(existing_deleted.id, request.ip_address), "initial_discovery")
        return APIResponse.success(
            data={
                "id": str(existing_deleted.id),
                "message": "Endpoint restored successfully.",
            }
        )

    new_endpoint = await repo.create_endpoint(
        ip_address=request.ip_address,
        hostname=request.hostname,
        device_type=request.device_type,
        site=place.get("site"),
        location=place.get("location"),
        tags=place.get("tags") or [],
        description=request.description,
        monitoring_enabled=request.monitoring_enabled,
        allow_incident_trace=request.allow_incident_trace,
        allow_topology_discovery=request.allow_topology_discovery,
        enable_rca=request.enable_rca,
        enable_scheduled_discovery=request.enable_scheduled_discovery,
        is_l2_segment=request.is_l2_segment,
        manual_parent_id=request.manual_parent_id,
        flow_exporter_ips=request.flow_exporter_ips,
        flow_interface_aliases=request.flow_interface_aliases,
        endpoint_status="ACTIVE",
        created_by=admin_uuid,
    )

    new_id = getattr(new_endpoint, "id", None)
    if new_id and request.snmp_credential_id:
        # Set after the insert so create_endpoint stays as it is; the set must exist.
        await snmp_credentials.require_set(db, request.snmp_credential_id)
        await repo.update_endpoint(new_id, snmp_credential_id=request.snmp_credential_id)
    if new_id:
        await auth_repo.create_audit_log(
            user_id=admin_uuid,
            action="ENDPOINT:CREATE",
            target_type="endpoints",
            target_id=new_id,
            details={
                "ip_address": request.ip_address,
                "hostname": request.hostname,
            },
        )

    await db.commit()

    if new_id:
        spawn(_bg_run_initial_discovery(new_id, request.ip_address), "initial_discovery")

    return APIResponse.success(
        data={"id": str(new_id), "message": "Endpoint created."},
    )


@router.patch("/{endpoint_id}", response_model=APIResponse)
async def update_endpoint(
    endpoint_id: UUID,
    request: UpdateEndpointRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services.topology import topology_manager

    repo = EndpointRepository(db)
    auth_repo = AuthRepository(db)

    endpoint = await repo.get_by_id(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    # A field left out, or sent as null, is not changed; except manual_parent_id, where an
    # explicit null removes the parent (model_fields_set tells "sent as null" from "left out").
    updates = {}
    audit_details = {}

    if request.hostname is not None:
        updates["hostname"] = request.hostname
        audit_details["hostname"] = request.hostname
    if request.device_type is not None:
        updates["device_type"] = request.device_type
        audit_details["device_type"] = request.device_type
    # Site, location and tags: sent means set (an empty or null value clears site/location).
    place = await _place_fields(repo, request, request.model_fields_set & {"site", "location", "tags"})
    updates.update(place)
    audit_details.update(place)
    if request.description is not None:
        updates["description"] = request.description
        audit_details["description"] = request.description
    if request.monitoring_enabled is not None:
        updates["monitoring_enabled"] = request.monitoring_enabled
        audit_details["monitoring_enabled"] = request.monitoring_enabled
    if request.allow_incident_trace is not None:
        updates["allow_incident_trace"] = request.allow_incident_trace
        audit_details["allow_incident_trace"] = request.allow_incident_trace
    if request.allow_topology_discovery is not None:
        updates["allow_topology_discovery"] = request.allow_topology_discovery
        audit_details["allow_topology_discovery"] = request.allow_topology_discovery
    if request.enable_rca is not None:
        updates["enable_rca"] = request.enable_rca
        audit_details["enable_rca"] = request.enable_rca
    if request.enable_scheduled_discovery is not None:
        updates["enable_scheduled_discovery"] = request.enable_scheduled_discovery
        audit_details["enable_scheduled_discovery"] = request.enable_scheduled_discovery
    if request.is_l2_segment is not None:
        updates["is_l2_segment"] = request.is_l2_segment
        audit_details["is_l2_segment"] = request.is_l2_segment
    if request.flow_exporter_ips is not None:
        updates["flow_exporter_ips"] = request.flow_exporter_ips
        audit_details["flow_exporter_ips"] = request.flow_exporter_ips
    if request.flow_interface_aliases is not None:
        updates["flow_interface_aliases"] = request.flow_interface_aliases
        audit_details["flow_interface_aliases"] = request.flow_interface_aliases
    if request.endpoint_status is not None:
        updates["endpoint_status"] = request.endpoint_status
        audit_details["endpoint_status"] = request.endpoint_status

    if "manual_parent_id" in request.model_fields_set:
        if request.manual_parent_id:
            if str(request.manual_parent_id) == str(endpoint_id):
                raise HTTPException(
                    status_code=400,
                    detail="Endpoint cannot be set as its own parent.",
                )
            if await repo.detect_parent_cycle(endpoint_id, request.manual_parent_id):
                raise HTTPException(
                    status_code=400,
                    detail="Cyclic parent relationship detected.",
                )
        updates["manual_parent_id"] = request.manual_parent_id
        audit_details["manual_parent_id"] = str(request.manual_parent_id) if request.manual_parent_id else None

    if "snmp_credential_id" in request.model_fields_set:
        if request.snmp_credential_id:
            await snmp_credentials.require_set(db, request.snmp_credential_id)
        updates["snmp_credential_id"] = request.snmp_credential_id
        audit_details["snmp_credential_id"] = str(request.snmp_credential_id) if request.snmp_credential_id else None

    if not updates:
        return APIResponse.success(data={"message": "No changes provided."})

    await repo.update_endpoint(endpoint_id, **updates)

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    await auth_repo.create_audit_log(
        user_id=admin_uuid,
        action="ENDPOINT:UPDATE",
        target_type="endpoints",
        target_id=endpoint_id,
        details=audit_details,
    )

    await db.commit()

    # Only this API worker's map is rebuilt here; each process keeps its own (services/topology).
    try:
        await topology_manager.full_rebuild(db)
    except Exception:
        pass

    return APIResponse.success(data={"message": "Endpoint updated."})


@router.delete("/{endpoint_id}", response_model=APIResponse)
async def delete_endpoint(
    endpoint_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services.topology import topology_manager

    repo = EndpointRepository(db)
    auth_repo = AuthRepository(db)

    endpoint = await repo.get_by_id(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    now = datetime.now(timezone.utc)
    await repo.soft_delete_endpoint(endpoint_id, deleted_at=now)

    admin_uuid = None
    if current_user.get("sub"):
        try:
            admin_uuid = UUID(str(current_user.get("sub")))
        except Exception:
            pass

    await auth_repo.create_audit_log(
        user_id=admin_uuid,
        action="ENDPOINT:DELETE",
        target_type="endpoints",
        target_id=endpoint_id,
        details={},
    )

    await db.commit()

    try:
        await topology_manager.full_rebuild(db)
    except Exception:
        pass

    return APIResponse.success(data={"message": "Endpoint deleted."})


@router.post("/{endpoint_id}/refresh-baseline", response_model=APIResponse)
async def trigger_refresh_baseline(
    endpoint_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services.baseline_route import refresh_baseline_route
    from app.services.topology import topology_manager

    repo = EndpointRepository(db)
    endpoint = await repo.get_by_id(endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    # An admin asked for it: a new path counts at once, without the second confirming trace.
    res = await refresh_baseline_route(
        endpoint_id, str(endpoint.ip_address), db=db, trigger="manual", confirm_now=True
    )
    await db.commit()

    try:
        hops = res.get("hops", []) if isinstance(res, dict) else []
        await topology_manager.update_endpoint_path(endpoint_id, hops)
    except Exception:
        pass

    return APIResponse.success(
        data={
            "message": "Route discovery completed and baseline refreshed.",
            "route": res,
        }
    )


# --- normal and current route (v3.3.6 build plan 6.4) ----------------------------------------


@router.get("/{endpoint_id}/route", response_model=APIResponse)
async def get_endpoint_route(
    endpoint_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The endpoint's current path (what root-cause analysis compares with), its normal path,
    whether it is on the normal path, a pending path waiting for confirmation, flapping, and the
    latest route-change log entries. `data` is null while it has no path yet."""
    from app.services.route_paths import path_status

    return APIResponse.success(data=await path_status(db, endpoint_id))


class NormalPathRequest(BaseModel):
    # True: the current path becomes normal and stays so until unpinned. False: it becomes
    # normal now, and later paths take over after 24 hours as usual.
    pinned: bool = False

    model_config = ConfigDict(from_attributes=True)


@router.post("/{endpoint_id}/route/normal", response_model=APIResponse)
async def make_current_path_normal(
    endpoint_id: UUID,
    request: NormalPathRequest,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin: the current path becomes the normal one now (optionally pinned). Logged in the
    route-change log and the audit log."""
    from app.services.route_paths import set_normal_path

    if not await set_normal_path(db, endpoint_id, pinned=request.pinned):
        raise HTTPException(status_code=404, detail="This endpoint has no recorded path yet.")
    await AuthRepository(db).create_audit_log(
        user_id=_admin_uuid(current_user),
        action="ENDPOINT:SET_NORMAL_PATH",
        target_type="endpoints",
        target_id=endpoint_id,
        details={"pinned": request.pinned},
    )
    await db.commit()
    # The map draws the normal path faded while an endpoint is off it: redraw everywhere.
    from app.services.baseline_route import _announce_topology_change

    await _announce_topology_change("NORMAL_PATH_SET", endpoint_id)
    return APIResponse.success(data={"message": "Normal path set.", "pinned": request.pinned})


@router.delete("/{endpoint_id}/route/pin", response_model=APIResponse)
async def unpin_normal_path_route(
    endpoint_id: UUID,
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin: the normal path is no longer pinned; it follows the current path after 24 hours."""
    from app.services.route_paths import unpin_normal_path

    if not await unpin_normal_path(db, endpoint_id):
        raise HTTPException(status_code=404, detail="The normal path of this endpoint is not pinned.")
    await AuthRepository(db).create_audit_log(
        user_id=_admin_uuid(current_user),
        action="ENDPOINT:UNPIN_NORMAL_PATH",
        target_type="endpoints",
        target_id=endpoint_id,
        details={},
    )
    await db.commit()
    return APIResponse.success(data={"message": "Normal path unpinned."})
