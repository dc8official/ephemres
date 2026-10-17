from __future__ import annotations

from app.core.asgi import APIRouter, Depends

from app.database import get_db
from app.routers.auth import get_current_user, require_admin
from app.routers.events import broadcast_sse_event
from app.schemas import APIResponse
from app.services.baseline_route import enqueue_scheduled_discovery
from app.services.topology import topology_manager
from app.core.orm import AsyncSession

router = APIRouter(prefix="/topology", tags=["topology"])


@router.get("", response_model=APIResponse)
@router.get("/", response_model=APIResponse)
async def get_topology(
    current_user: dict = Depends(get_current_user),
):
    """
    Returns the network topology graph directly from in-memory RAM cache in O(1) time
    with zero database queries on read requests.
    """
    graph_data = topology_manager.get_cached_graph()
    return APIResponse.success(data=graph_data)


@router.post("/rebuild", response_model=APIResponse)
async def rebuild_topology(
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Forces an immediate full reconstruction of the in-memory DAG topology graph from PostgreSQL.
    Synchronizes in-memory RAM cache with current baseline routes.
    """
    graph_data = await topology_manager.full_rebuild(db)
    await broadcast_sse_event(
        "TOPOLOGY_UPDATED",
        {"reason": "MANUAL_REBUILD"},
    )
    return APIResponse.success(
        data={
            "message": "Topology graph rebuilt successfully.",
            "node_count": len(graph_data.get("nodes", [])),
            "edge_count": len(graph_data.get("edges", [])),
        }
    )


@router.post("/discover-all", response_model=APIResponse)
async def discover_all_routes(
    current_user: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueues all active UP endpoints for sequential background traceroute discovery (500ms safety delay).
    """
    enqueued_count = await enqueue_scheduled_discovery(db)
    await broadcast_sse_event(
        "TOPOLOGY_UPDATED",
        {"reason": "FLEET_DISCOVERY_STARTED", "enqueued_count": enqueued_count},
    )
    return APIResponse.success(
        data={
            "message": f"Enqueued {enqueued_count} active endpoints for background route discovery.",
            "enqueued_count": enqueued_count,
        }
    )
