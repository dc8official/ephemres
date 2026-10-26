from __future__ import annotations

import asyncio
import ipaddress
import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from app.core.asgi import APIRouter, Depends, HTTPException, Query
from app.core.schema import BaseModel, ConfigDict, field_validator
from app.core.orm import AsyncSession

from app.database import get_db
from app.repositories.auth_repo import AuthRepository
from app.repositories.endpoint_repo import EndpointRepository
from app.routers.auth import get_current_user, require_admin
from app.schemas import APIResponse, PaginationMeta
from app.services.uptime_calculator import (
    calculate_uptime_denominator_and_percentage,
    get_service_gap_intervals,
    get_unknown_seconds_for_period,
)

logger = logging.getLogger(__name__)


async def _bg_run_initial_discovery(endpoint_id: UUID, ip_address: str) -> None:
    from app.database import AsyncSessionLocal
    from app.services.baseline_route import refresh_baseline_route
    from app.services.topology import topology_manager

    async with AsyncSessionLocal() as bg_db:
        try:
            await refresh_baseline_route(endpoint_id, ip_address, db=bg_db)
            await bg_db.commit()
            await topology_manager.full_rebuild(bg_db)
        except Exception as exc:
            logger.error(
                "Background initial discovery failed for endpoint %s: %s",
                endpoint_id,
                exc,
            )


class CreateEndpointRequest(BaseModel):
    ip_address: str
    hostname: str
    device_type: str
    location: Optional[str] = None
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
    location: Optional[str] = None
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
        "is_resolved": rca.is_resolved,
    }
    return APIResponse.success(data=incident_data)


@router.get("/", response_model=APIResponse)
async def list_endpoints(
    status: Optional[str] = Query(default=None),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(hours=24)

    ALLOWED_STATUSES = {"ACTIVE", "DISABLED", "MONITORED", "UNMONITORED"}
    clean_status = None
    if status is not None:
        clean_status = status.strip().upper()
        if clean_status not in ALLOWED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status filter '{status}'. Must be one of: {', '.join(sorted(ALLOWED_STATUSES))}",
            )

    calc_page = page if isinstance(page, int) else None
    calc_page_size = page_size if isinstance(page_size, int) else None
    if calc_page is not None and calc_page_size is None:
        calc_page_size = 50
    elif calc_page_size is not None and calc_page is None:
        calc_page = 1

    repo = EndpointRepository(db)
    rows = await repo.list_with_stats(
        status=clean_status,
        since_utc=since_utc,
        now_utc=now_utc,
        page=calc_page,
        page_size=calc_page_size,
    )
    gap_intervals = await get_service_gap_intervals(db, since_utc, now_utc)

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
            "location": row["location"],
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
        })

    if calc_page is not None and calc_page_size is not None:
        total = await repo.count_listed(status=clean_status)
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


@router.get("/{endpoint_id}", response_model=APIResponse)
async def get_endpoint(
    endpoint_id: UUID,
    current_user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now_utc = datetime.now(timezone.utc)
    since_utc = now_utc - timedelta(hours=24)

    repo = EndpointRepository(db)
    row = await repo.get_detail_with_stats(
        endpoint_id=endpoint_id,
        since_utc=since_utc,
        now_utc=now_utc,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    gap_intervals = await get_service_gap_intervals(db, since_utc, now_utc)
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
        "location": row["location"],
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

    # Check for soft-deleted endpoint to restore
    existing_deleted = await repo.get_by_ip(request.ip_address, include_deleted=True)
    if existing_deleted and getattr(existing_deleted, "endpoint_status", "") == "DELETED":
        restored = await repo.restore_endpoint(
            endpoint_id=existing_deleted.id,
            hostname=request.hostname,
            device_type=request.device_type,
            location=request.location,
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
        asyncio.create_task(
            _bg_run_initial_discovery(existing_deleted.id, request.ip_address)
        )
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
        location=request.location,
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
        asyncio.create_task(
            _bg_run_initial_discovery(new_id, request.ip_address)
        )

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

    updates = {}
    audit_details = {}

    if request.hostname is not None:
        updates["hostname"] = request.hostname
        audit_details["hostname"] = request.hostname
    if request.device_type is not None:
        updates["device_type"] = request.device_type
        audit_details["device_type"] = request.device_type
    if request.location is not None:
        updates["location"] = request.location
        audit_details["location"] = request.location
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

    if request.manual_parent_id is not None:
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

    res = await refresh_baseline_route(
        endpoint_id, str(endpoint.ip_address), db=db
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
