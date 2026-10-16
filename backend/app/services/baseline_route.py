from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import struct
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.services.diagnostics import run_throttled_traceroute
from app.services.net_interfaces import get_local_interface_networks
from app.services.timezone_utils import get_local_timezone

logger = logging.getLogger(__name__)

# Async Queue for sequential midnight baseline discovery
discovery_route_queue: asyncio.Queue[tuple[UUID, str]] = asyncio.Queue()
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
) -> Dict[str, Any]:
    """
    Executes asynchronous traceroute, parses hops, detects Layer 2 segment status
    via 4-Tier Boundary Classifier, and upserts the baseline route record in `endpoint_baseline_routes`.
    """
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

    upsert_route_sql = text("""
        INSERT INTO endpoint_baseline_routes (
            endpoint_id, updated_at, total_hops, hops
        ) VALUES (
            CAST(:endpoint_id AS uuid), NOW(), :total_hops, :hops
        )
        ON CONFLICT (endpoint_id) DO UPDATE SET
            updated_at = NOW(),
            total_hops = EXCLUDED.total_hops,
            hops = EXCLUDED.hops
    """)

    update_l2_sql = text("""
        UPDATE endpoints
        SET is_l2_segment = :is_l2_segment,
            updated_at = NOW()
        WHERE id = CAST(:endpoint_id AS uuid)
    """)

    has_valid_new_hops = any(h.get("ip") is not None for h in formatted_hops)
    final_hops = formatted_hops
    final_hops_count = total_hops

    async def _execute_queries(session: AsyncSession):
        nonlocal final_hops, final_hops_count
        if not has_valid_new_hops:
            check_sql = text("""
                SELECT total_hops, hops 
                FROM endpoint_baseline_routes 
                WHERE endpoint_id = CAST(:endpoint_id AS uuid)
            """)
            res = await session.execute(check_sql, {"endpoint_id": str(endpoint_id)})
            row = res.fetchone()
            if row and row.hops:
                existing = row.hops if isinstance(row.hops, list) else json.loads(row.hops) if isinstance(row.hops, str) else []
                if any(h.get("ip") is not None for h in existing):
                    logger.warning(
                        "Traceroute for %s returned 0 reachable hops. Preserving last-known-good baseline route.",
                        target_ip,
                    )
                    final_hops = existing
                    final_hops_count = len(existing)

        await session.execute(
            upsert_route_sql,
            {
                "endpoint_id": str(endpoint_id),
                "total_hops": final_hops_count,
                "hops": json.dumps(final_hops),
            },
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

    logger.info(
        "Refreshed baseline route for endpoint %s (%s): total_hops=%d, is_l2_segment=%s, tier=%s, fhrp=%s",
        endpoint_id,
        target_ip,
        final_hops_count,
        is_l2,
        tier,
        fhrp_type,
    )

    return {
        "endpoint_id": str(endpoint_id),
        "target_ip": target_ip,
        "total_hops": final_hops_count,
        "hops": final_hops,
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


async def start_midnight_discovery_worker(db_session_factory) -> asyncio.Task:
    """
    Sequential Midnight Discovery Worker task:
    1. Reads queued targets from `discovery_route_queue` and processes them sequentially with 500ms delay.
    2. Runs scheduled midnight job daily.
    """

    async def _queue_consumer():
        from app.services.topology import topology_manager
        while True:
            try:
                endpoint_id, ip_address = await discovery_route_queue.get()
                logger.info(
                    "Processing baseline route discovery for endpoint %s (%s)",
                    endpoint_id,
                    ip_address,
                )
                try:
                    async with db_session_factory() as db:
                        res = await refresh_baseline_route(endpoint_id, ip_address, db=db)
                        await db.commit()

                        # Incremental in-memory RAM update
                        await topology_manager.update_endpoint_path(
                            endpoint_id, res.get("hops", [])
                        )

                        # If discovery queue has fully drained, trigger full rebuild to finalize global layout
                        if discovery_route_queue.empty():
                            logger.info("Discovery queue drained. Finalizing full topology graph rebuild...")
                            await topology_manager.full_rebuild(db)
                            try:
                                from app.routers.events import broadcast_sse_event
                                await broadcast_sse_event(
                                    "TOPOLOGY_UPDATED",
                                    {"reason": "FLEET_DISCOVERY_COMPLETED"},
                                )
                            except Exception:
                                pass
                except Exception as e:
                    logger.error(
                        "Error refreshing baseline route for %s: %s",
                        ip_address,
                        e,
                    )
                finally:
                    discovery_in_progress.discard(endpoint_id)
                    discovery_route_queue.task_done()

                # Mandatory 500ms delay between targets
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in baseline route discovery queue consumer: %s", e)
                await asyncio.sleep(0.5)

    async def _midnight_scheduler():
        while True:
            try:
                now = datetime.now(get_local_timezone())
                # Compute seconds until next 00:00 (midnight)
                seconds_until_midnight = (
                    24 * 3600 - (now.hour * 3600 + now.minute * 60 + now.second)
                )
                if seconds_until_midnight <= 0:
                    seconds_until_midnight = 86400
                await asyncio.sleep(seconds_until_midnight)

                logger.info("Midnight reached. Triggering scheduled baseline discovery.")
                async with db_session_factory() as db:
                    await enqueue_scheduled_discovery(db)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in midnight scheduler loop: %s", e)
                await asyncio.sleep(3600)

    consumer_task = asyncio.create_task(_queue_consumer())
    scheduler_task = asyncio.create_task(_midnight_scheduler())

    # Combined supervisor task
    async def _supervisor():
        try:
            await asyncio.gather(consumer_task, scheduler_task)
        except asyncio.CancelledError:
            consumer_task.cancel()
            scheduler_task.cancel()

    return asyncio.create_task(_supervisor())
