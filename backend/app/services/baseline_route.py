"""Baseline routes: the traceroute path to each endpoint while it is healthy.

Root-cause analysis compares a failure traceroute with this path. It is traced when an
endpoint is added, on demand (Refresh baseline), after a recovery, when a transit device or the
reply TTL suggests the route moved, and by a rolling schedule (every endpoint every N hours,
spread out, since v3.3.6; before, all at midnight) for endpoints that are UP and have scheduled
discovery on. The API worker holding the scheduler lock (services/scheduler_leader) queues the
scheduled traces; they run a few at a time (start_midnight_discovery_worker). Each path is also
given a boundary tier (classify_boundary_tier), and the gateway's MAC is checked for an
HSRP/VRRP/GLBP failover.

Since v3.3.6 a trace no longer simply overwrites the baseline: services/route_paths.record_trace
decides. The stored "hops" are the CURRENT path; a different path counts only when a second
trace at least 2 minutes later agrees (the discovery worker queues that confirming trace
itself), and a separate NORMAL path follows after 24 hours. See route_paths for the rules.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import struct
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from app.core.orm import text
from app.core.orm import AsyncSession

from app.database import AsyncSessionLocal
from app.services.diagnostics import run_throttled_traceroute
from app.core.netlink import get_local_interface_networks
from app.services.timezone_utils import get_local_timezone

logger = logging.getLogger(__name__)

# Queue of route traces to run, one at a time: (endpoint id, address) or (endpoint id, address,
# trigger), the trigger being one of services.route_paths.TRIGGERS ("scheduled" when missing).
discovery_route_queue: asyncio.Queue[tuple] = asyncio.Queue()
discovery_in_progress: Set[UUID] = set()

# In-memory Gateway MAC cache for detecting FHRP failover / MAC drift
gateway_mac_cache: Dict[str, str] = {}

FHRP_PATTERNS = [
    ("HSRP_V1", re.compile(r"^00:00:0c:07:ac:([0-9a-f]{2})$", re.IGNORECASE)),
    ("HSRP_V2", re.compile(r"^00:00:0c:9f:f([0-9a-f]):([0-9a-f]{2})$", re.IGNORECASE)),
    ("VRRP_IPV4", re.compile(r"^00:00:5e:00:01:([0-9a-f]{2})$", re.IGNORECASE)),
    ("VRRP_IPV6", re.compile(r"^00:00:5e:00:02:([0-9a-f]{2})$", re.IGNORECASE)),
    ("GLBP", re.compile(r"^00:07:b4:([0-9a-f]{2}):([0-9a-f]{2}):([0-9a-f]{2})$", re.IGNORECASE)),
]


def get_default_gateway_ip() -> Optional[str]:
    """
    Parses /proc/net/route to capture the system host Default Gateway IP address.
    """
    try:
        with open("/proc/net/route", "r") as f:
            for line in f:
                fields = line.strip().split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    gateway_hex = fields[2]
                    if gateway_hex != "00000000":
                        ip_int = int(gateway_hex, 16)
                        ip_bytes = socket.inet_ntoa(struct.pack("<I", ip_int))
                        return ip_bytes
    except Exception:
        pass
    return None


def get_arp_table() -> Dict[str, str]:
    """
    Parses /proc/net/arp to inspect local Layer 2 IP to MAC address mappings.
    Returns:
        Dict[ip_address, mac_address_lowercase]
    """
    arp_map: Dict[str, str] = {}
    try:
        with open("/proc/net/arp", "r") as f:
            lines = f.readlines()
            for line in lines[1:]:
                parts = line.strip().split()
                if len(parts) >= 4:
                    ip_addr = parts[0]
                    flags = parts[2]
                    mac_addr = parts[3].lower()
                    if flags != "0x0" and mac_addr != "00:00:00:00:00:00":
                        arp_map[ip_addr] = mac_addr
    except Exception:
        pass
    return arp_map


def get_fhrp_type(mac_address: str) -> Optional[str]:
    """
    Inspects MAC address against FHRP Virtual Router MAC regex patterns:
    - HSRP v1: ^00:00:0c:07:ac:([0-9a-f]{2})$
    - HSRP v2: ^00:00:0c:9f:f([0-9a-f]):([0-9a-f]{2})$
    - VRRP IPv4: ^00:00:5e:00:01:([0-9a-f]{2})$
    - VRRP IPv6: ^00:00:5e:00:02:([0-9a-f]{2})$
    - GLBP: ^00:07:b4:([0-9a-f]{2}):([0-9a-f]{2}):([0-9a-f]{2})$
    """
    if not mac_address:
        return None
    mac_clean = mac_address.strip().lower()
    for name, pattern in FHRP_PATTERNS:
        if pattern.match(mac_clean):
            return name
    return None


def classify_boundary_tier(
    target_ip_str: str,
    total_hops: int = 1,
) -> Tuple[str, bool, Optional[str], Optional[str], Optional[str]]:
    """
    4-Tier Network Boundary Classifier:
    - Tier 1: L2_LOCAL_HOST (Resolved in local ARP cache or loopback/local subnet)
    - Tier 2: L2_L3_GATEWAY_FHRP (Resolved to HSRP/VRRP/GLBP Virtual MAC)
    - Tier 3: L2_L3_GATEWAY_DEFAULT (Matches host server's default gateway)
    - Tier 4: L3_ROUTED_TRANSIT (Remote routed destination across WAN/routers)

    Returns:
        Tuple[tier_name, is_l2, default_gateway_ip, mac_address, fhrp_type]
    """
    try:
        target = ipaddress.ip_address(target_ip_str)
        if target.is_loopback:
            return ("L2_LOCAL_HOST", True, None, None, None)
    except Exception:
        return ("L3_ROUTED_TRANSIT", False, None, None, None)

    default_gw = get_default_gateway_ip()
    arp_table = get_arp_table()
    mac_addr = arp_table.get(target_ip_str)
    fhrp_type = get_fhrp_type(mac_addr) if mac_addr else None

    # Tier 2: FHRP Virtual Gateway MAC
    if fhrp_type:
        return ("L2_L3_GATEWAY_FHRP", True, default_gw, mac_addr, fhrp_type)

    # Tier 3: Default Gateway IP
    if default_gw and target_ip_str == default_gw:
        return ("L2_L3_GATEWAY_DEFAULT", True, default_gw, mac_addr, None)

    # Tier 1: Resolved in ARP cache or local subnet match
    if mac_addr or is_local_subnet(target_ip_str):
        return ("L2_LOCAL_HOST", True, default_gw, mac_addr, None)

    # Tier 4: Remote L3 WAN / Routed Destination
    if total_hops == 1 and not target.is_global:
        return ("L2_LOCAL_HOST", True, default_gw, mac_addr, None)

    return ("L3_ROUTED_TRANSIT", False, default_gw, mac_addr, None)


def is_local_subnet(target_ip_str: str) -> bool:
    """
    Determines if target IP resides in the local subnet/VLAN of the monitoring server.
    """
    try:
        target = ipaddress.ip_address(target_ip_str.strip())
        if target.is_loopback:
            return True
        for net in get_local_interface_networks():
            if target.version == net.version and target in net:
                return True
    except Exception:
        pass
    return False


async def refresh_baseline_route(
    endpoint_id: UUID,
    target_ip: str,
    db: Optional[AsyncSession] = None,
    trigger: str = "scheduled",
    confirm_now: bool = False,
) -> Dict[str, Any]:
    """
    Traces the route to the endpoint, records whether it sits on the local Layer 2 segment, and
    hands the hops to route_paths.record_trace, which updates the current path (or holds a new
    one as pending until a second trace confirms it). `trigger` is stored in the route-change
    log; `confirm_now` (an admin's Refresh) skips the confirmation. The returned "hops" are the
    current path afterwards; "path_outcome" and "confirm_after" say whether to trace again.
    """
    from app.services.route_paths import record_trace

    trace_res = await run_throttled_traceroute(target_ip)
    raw_hops: List[Dict[str, Any]] = trace_res.get("hops", [])

    # Format hops into structured JSON array
    formatted_hops: List[Dict[str, Any]] = []
    for h in raw_hops:
        formatted_hops.append({
            "hop": h.get("hop"),
            "ip": h.get("ip"),
            "rtt_ms": h.get("rtt_ms"),
        })

    total_hops = len(formatted_hops)

    # Execute 4-Tier Boundary Classification
    tier, is_l2, default_gw, mac_addr, fhrp_type = classify_boundary_tier(target_ip, total_hops)

    # Check for FHRP Gateway MAC drift / failover
    old_mac = gateway_mac_cache.get(target_ip)
    if mac_addr:
        if old_mac and old_mac != mac_addr:
            logger.warning(
                "Gateway MAC drift / failover detected for target %s (FHRP Protocol: %s)",
                target_ip,
                fhrp_type or "Standard",
            )
        gateway_mac_cache[target_ip] = mac_addr

    update_l2_sql = text("""
        UPDATE endpoints
        SET is_l2_segment = :is_l2_segment,
            updated_at = NOW()
        WHERE id = CAST(:endpoint_id AS uuid)
    """)

    outcome: Dict[str, Any] = {}

    async def _execute_queries(session: AsyncSession):
        nonlocal outcome
        outcome = await record_trace(
            session, endpoint_id, formatted_hops, trigger, confirm_now=confirm_now
        )
        await session.execute(
            update_l2_sql,
            {
                "endpoint_id": str(endpoint_id),
                "is_l2_segment": is_l2,
            },
        )

    if db is not None:
        await _execute_queries(db)
    else:
        async with AsyncSessionLocal() as session:
            await _execute_queries(session)
            await session.commit()

    current_hops = outcome.get("hops", formatted_hops)
    logger.info(
        "Traced route to endpoint %s (%s): %s, %d hops, is_l2_segment=%s, tier=%s, fhrp=%s, trigger=%s",
        endpoint_id,
        target_ip,
        outcome.get("outcome"),
        len(current_hops),
        is_l2,
        tier,
        fhrp_type,
        trigger,
    )

    return {
        "endpoint_id": str(endpoint_id),
        "target_ip": target_ip,
        "total_hops": len(current_hops),
        "hops": current_hops,
        "path_outcome": outcome.get("outcome"),
        "confirm_after": outcome.get("confirm_after"),
        "is_l2_segment": is_l2,
        "boundary_tier": tier,
        "mac_address": mac_addr,
        "fhrp_type": fhrp_type,
    }


async def enqueue_scheduled_discovery(db: AsyncSession) -> int:
    """
    Enqueues active 'UP' endpoints with `enable_scheduled_discovery == True` for baseline discovery.
    """
    query = text("""
        SELECT e.id, host(e.ip_address) AS ip_address
        FROM endpoints e
        LEFT JOIN LATERAL (
            SELECT operational_state
            FROM endpoint_events
            WHERE endpoint_id = e.id
            ORDER BY start_time DESC
            LIMIT 1
        ) ev ON TRUE
        WHERE e.endpoint_status != 'DELETED'
          AND e.enable_scheduled_discovery = TRUE
          AND COALESCE(ev.operational_state, 'UP') = 'UP'
    """)
    result = await db.execute(query)
    rows = result.fetchall()
    count = 0
    for r in rows:
        target_uuid = UUID(str(r.id))
        if target_uuid not in discovery_in_progress:
            discovery_in_progress.add(target_uuid)
            await discovery_route_queue.put((target_uuid, str(r.ip_address)))
            count += 1
    logger.info("Enqueued %d endpoints for scheduled baseline discovery.", count)
    return count


# Confirming traces waiting for their delay, held so they aren't garbage-collected.
_confirm_tasks: Set[asyncio.Task] = set()


def schedule_confirmation(endpoint_id: UUID, ip_address: str, delay: Optional[float]) -> None:
    """Queues a second trace once the confirmation delay has passed (route_paths.CONFIRM_AFTER),
    so a path seen once becomes current only if it is still there. Skipped when a trace of the
    endpoint is already waiting or running."""
    from app.services.route_paths import CONFIRM_AFTER

    wait = float(delay or CONFIRM_AFTER.total_seconds()) + 5.0

    async def _later() -> None:
        await asyncio.sleep(wait)
        if endpoint_id in discovery_in_progress:
            return
        discovery_in_progress.add(endpoint_id)
        await discovery_route_queue.put((endpoint_id, ip_address, "confirm"))

    task = asyncio.create_task(_later())
    _confirm_tasks.add(task)
    task.add_done_callback(_confirm_tasks.discard)


# --- discovery worker (v3.3.6: parallel, rolling schedule) ------------------------------------

# How often the rolling schedule looks for endpoints due a re-trace.
ROLLING_TICK_SECONDS = 300
# Pacing between starting two traces, as the serial worker always had.
START_GAP_SECONDS = 0.5


async def _routers_on_path(db: AsyncSession, endpoint_id: UUID, target_ip: str) -> Set[str]:
    """The routers a trace to this endpoint will pass, from its current path: every answering hop
    except the endpoint itself. Routers send only about one reply per second, so two traces
    sharing one take turns (build plan 6.3). An endpoint with no path yet is assumed to go
    through the server's default gateway."""
    res = await db.execute(
        text("SELECT hops FROM endpoint_baseline_routes WHERE endpoint_id = CAST(:id AS uuid)"),
        {"id": str(endpoint_id)},
    )
    row = res.fetchone()
    hops = row[0] if row else None
    if isinstance(hops, str):
        try:
            hops = json.loads(hops)
        except ValueError:
            hops = None
    routers = {
        str(h.get("ip")) for h in (hops or [])
        if isinstance(h, dict) and h.get("ip") and str(h.get("ip")) != str(target_ip)
    }
    if not routers:
        routers = {get_default_gateway_ip() or "default-gateway"}
    return routers


async def _announce_topology_change(reason: str, endpoint_id: Optional[UUID] = None) -> None:
    """Tells every API worker that routes changed. The event goes out on the broker's
    NODE_STATE_CHANGE channel (no extra LISTEN connection) as type TOPOLOGY_UPDATED; each
    worker's relay rebuilds its own copy of the map and then tells its browsers
    (services/telemetry_relay). Before v3.3.6 only the worker that ran the trace learned of it."""
    try:
        from app.services.driver_manager import driver_manager

        broker = driver_manager.get_event_broker()
        if broker is not None:
            await broker.publish(
                "NODE_STATE_CHANGE",
                {"type": "TOPOLOGY_UPDATED", "reason": reason,
                 "endpoint_id": str(endpoint_id) if endpoint_id else None},
            )
    except Exception as exc:
        logger.debug("Could not announce topology change: %s", exc)


async def _due_for_rolling_trace(db: AsyncSession, hours: int) -> List[Tuple[UUID, str]]:
    """Endpoints UP with scheduled discovery on whose route was last traced more than `hours`
    ago (or never), oldest first."""
    res = await db.execute(
        text("""
            SELECT e.id, host(e.ip_address) AS ip_address
            FROM endpoints e
            LEFT JOIN endpoint_baseline_routes r ON r.endpoint_id = e.id
            LEFT JOIN LATERAL (
                SELECT operational_state FROM endpoint_events
                WHERE endpoint_id = e.id ORDER BY start_time DESC LIMIT 1
            ) ev ON TRUE
            WHERE e.endpoint_status != 'DELETED'
              AND e.enable_scheduled_discovery = TRUE
              AND COALESCE(ev.operational_state, 'UP') = 'UP'
              AND (r.updated_at IS NULL OR r.updated_at < NOW() - make_interval(hours => CAST(:hours AS integer)))
            ORDER BY r.updated_at NULLS FIRST
        """),
        {"hours": int(hours)},
    )
    return [(UUID(str(r[0])), str(r[1])) for r in res.fetchall()]


async def start_midnight_discovery_worker(db_session_factory) -> asyncio.Task:
    """Starts the route discovery worker (name kept from before v3.3.6, when it ran a fleet-wide
    pass at midnight). Three loops:

    - The dispatcher runs queued traces, up to the "parallel route traces" setting at once
      (runtime_settings.discovery_parallel), starting them 0.5 s apart. Traces whose current
      paths share a router take turns (_routers_on_path), because routers answer only about
      once a second and a crowd of traces would come back full of "* * *". A trace that leaves
      a path pending queues its own confirming trace (schedule_confirmation).
    - The rolling schedule (leader only), every 5 minutes, queues the endpoints whose route is
      older than "re-trace every" hours (runtime_settings.rediscovery_hours, default 24), a
      share per tick so the fleet is spread over the period instead of all at midnight.
    - The normal-path keeper (leader only), every 10 minutes: route_paths.promote_normal_paths.
    """
    from app.services.runtime_settings import get_runtime_settings

    running: Dict[asyncio.Task, Set[str]] = {}
    backlog: List[tuple] = []
    wake = asyncio.Event()
    # Set while traces have run since the queue was last empty, for the "fleet done" event.
    worked = {"since_idle": False}

    async def _run_one(item: tuple) -> None:
        from app.services.topology import topology_manager

        endpoint_id, ip_address = item[0], item[1]
        trigger = item[2] if len(item) > 2 else "scheduled"
        try:
            async with db_session_factory() as db:
                res = await refresh_baseline_route(endpoint_id, ip_address, db=db, trigger=trigger)
                await db.commit()
            if res.get("path_outcome") == "pending":
                schedule_confirmation(endpoint_id, ip_address, res.get("confirm_after"))
            # This worker's map now; every worker's map (and browsers) when the route moved.
            await topology_manager.update_endpoint_path(endpoint_id, res.get("hops", []))
            if res.get("path_outcome") in ("first", "path_changed", "back_to_normal"):
                await _announce_topology_change("ENDPOINT_ROUTE_CHANGED", endpoint_id)
        except Exception as e:
            logger.error("Error tracing route to %s: %s", ip_address, e)
        finally:
            discovery_in_progress.discard(endpoint_id)

    async def _dispatcher():
        while True:
            try:
                # Pull everything queued into the backlog without blocking while traces run.
                if not backlog and not running:
                    backlog.append(await discovery_route_queue.get())
                    discovery_route_queue.task_done()
                while not discovery_route_queue.empty():
                    backlog.append(discovery_route_queue.get_nowait())
                    discovery_route_queue.task_done()

                limit = max(1, int(get_runtime_settings().discovery_parallel))
                busy = set().union(*running.values()) if running else set()
                started = False
                for item in list(backlog):
                    if len(running) >= limit:
                        break
                    async with db_session_factory() as db:
                        routers = await _routers_on_path(db, item[0], item[1])
                    if routers & busy:
                        continue  # shares a router with a running trace: wait its turn
                    backlog.remove(item)
                    task = asyncio.create_task(_run_one(item))
                    worked["since_idle"] = True
                    running[task] = routers
                    busy |= routers
                    task.add_done_callback(lambda t: (running.pop(t, None), wake.set()))
                    started = True
                    await asyncio.sleep(START_GAP_SECONDS)

                if not backlog and not running:
                    if worked["since_idle"]:
                        worked["since_idle"] = False
                        await _announce_topology_change("FLEET_DISCOVERY_COMPLETED")
                    continue
                # Wait for a trace to finish or new work to arrive, whichever comes first.
                wake.clear()
                getter = asyncio.create_task(discovery_route_queue.get())
                waiter = asyncio.create_task(wake.wait())
                done, pending = await asyncio.wait({getter, waiter}, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                if getter in done:
                    backlog.append(getter.result())
                    discovery_route_queue.task_done()
                if not started and not running and backlog:
                    # Nothing could start and nothing runs: cannot happen with limit >= 1, but
                    # never spin.
                    await asyncio.sleep(START_GAP_SECONDS)
            except asyncio.CancelledError:
                for t in list(running):
                    t.cancel()
                break
            except Exception as e:
                logger.error("Error in route discovery dispatcher: %s", e)
                await asyncio.sleep(START_GAP_SECONDS)

    async def _rolling_schedule():
        from app.services.scheduler_leader import scheduler_leader

        while True:
            try:
                await asyncio.sleep(ROLLING_TICK_SECONDS)
                if scheduler_leader._task is not None and not scheduler_leader.is_leader:
                    continue
                hours = max(1, int(get_runtime_settings().rediscovery_hours))
                async with db_session_factory() as db:
                    due = await _due_for_rolling_trace(db, hours)
                    total = await db.execute(text(
                        "SELECT COUNT(*) FROM endpoints WHERE endpoint_status != 'DELETED' AND enable_scheduled_discovery"
                    ))
                    fleet = int(total.scalar() or 0)
                # A fair share per tick spreads the fleet over the period; endpoints that are
                # late (e.g. after downtime) catch up a few at a time, oldest first.
                ticks = max(1, (hours * 3600) // ROLLING_TICK_SECONDS)
                share = max(1, -(-fleet // ticks))
                queued = 0
                for endpoint_id, ip in due:
                    if queued >= share:
                        break
                    if endpoint_id in discovery_in_progress:
                        continue
                    discovery_in_progress.add(endpoint_id)
                    await discovery_route_queue.put((endpoint_id, ip, "scheduled"))
                    queued += 1
                if queued:
                    logger.info("Rolling schedule queued %d route trace(s) (every %d h).", queued, hours)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in rolling route schedule: %s", e)

    async def _normal_path_keeper():
        """Every 10 minutes on the leader: current paths held 24 hours become normal, flapping
        that has gone quiet ends, and old route-change log entries go (route_paths)."""
        from app.services.route_paths import promote_normal_paths
        from app.services.scheduler_leader import scheduler_leader

        while True:
            try:
                await asyncio.sleep(600)
                if scheduler_leader._task is not None and not scheduler_leader.is_leader:
                    continue
                async with db_session_factory() as db:
                    promoted = await promote_normal_paths(db)
                    await db.commit()
                if promoted:
                    logger.info("%d endpoint path(s) became the normal path after 24 hours.", promoted)
                    await _announce_topology_change("NORMAL_PATH_PROMOTED")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error promoting normal paths: %s", e)

    tasks = [
        asyncio.create_task(_dispatcher()),
        asyncio.create_task(_rolling_schedule()),
        asyncio.create_task(_normal_path_keeper()),
    ]

    async def _supervisor():
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()

    return asyncio.create_task(_supervisor())
