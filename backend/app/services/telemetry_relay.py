from __future__ import annotations

import asyncio
import collections
import logging
import time
from typing import Any, Deque, Dict, List, Optional

from app.core.orm import text
from app.database import AsyncSessionLocal
from app.routers.events import broadcast_sse_event
from app.services.driver_manager import driver_manager
from app.services.topology import topology_manager

logger = logging.getLogger(__name__)

# Targeted traceroutes started by route changes seen in flows (ROUTE_HINT): at most one per
# endpoint per cooldown, and at most this many per minute overall, so a core-link failover
# cannot set off a traceroute storm. A ROUTE_HINT with reason "ttl" comes from the engine's reply
# TTL watch (monitoring/ttl_watch.py) and is handled the same way, with its own cooldown.
ROUTE_HINT_COOLDOWN_SECONDS = 900
ROUTE_HINT_MAX_PER_MINUTE = 10

# Transit triggers (v3.3.6 build plan 6.2): when a monitored device that other endpoints' routes
# pass through goes DOWN, the endpoints behind it that are still UP a cycle later must have moved
# to another path, so they are re-traced; when it comes back, the endpoints whose normal path
# goes through it but whose current one doesn't are re-traced. Same cooldown idea per endpoint;
# instead of dropping traces past the per-minute cap they are spread over the next minutes.
TRANSIT_COOLDOWN_SECONDS = 900
TRANSIT_MAX_PER_MINUTE = 10

# The broker channels every API worker listens on. Without Performance Mode each one is a
# PostgreSQL LISTEN connection outside the pool, which main.py counts in the startup connection
# budget, so add a channel here and nowhere else.
CHANNELS = ("STATE_TRANSITION", "NODE_STATE_CHANGE", "RCA_INCIDENT", "ROUTE_HINT")


class TelemetryRelay:
    """Carries events from the other processes into this API worker.

    Listens on STATE_TRANSITION, NODE_STATE_CHANGE, RCA_INCIDENT and ROUTE_HINT through the
    active event broker (PostgreSQL LISTEN/NOTIFY, or FastStore pub/sub in Performance Mode).
    State changes update this worker's topology map, and a recovery to UP can start a baseline
    discovery for an endpoint that has none; a ROUTE_HINT may queue a traceroute (leader worker
    only). Every event is then passed on to this worker's SSE clients.
    """

    def __init__(self) -> None:
        self._running: bool = False
        self._tasks: List[asyncio.Task] = []
        self._route_hint_last: Dict[str, float] = {}
        self._route_hint_recent: Deque[float] = collections.deque()
        # Transit triggers: per (endpoint, direction) last re-trace, and the waits in flight.
        self._transit_last: Dict[str, float] = {}
        self._transit_tasks: set = set()

    async def start(self) -> None:
        """Starts background listener tasks for telemetry pub/sub channels."""
        if self._running:
            return
        self._running = True
        for channel in CHANNELS:
            self._tasks.append(
                asyncio.create_task(
                    self._listen_channel(channel),
                    name=f"telemetry_relay_{channel.lower()}",
                )
            )
        logger.info("TelemetryRelay started listening to inter-process broker channels.")

    async def stop(self) -> None:
        """Stops background listener tasks cleanly."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
        logger.info("TelemetryRelay stopped.")

    async def _listen_channel(self, channel: str) -> None:
        """Listens to a broker channel with automatic reconnection loop."""
        while self._running:
            try:
                async for event in driver_manager.subscribe(channel):
                    if not self._running:
                        break
                    await self._handle_relayed_event(channel, event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(
                    "TelemetryRelay: subscription to '%s' encountered error: %s. Retrying in 2s...",
                    channel,
                    exc,
                )
                await asyncio.sleep(2.0)

    async def _handle_relayed_event(self, channel: str, data: Dict[str, Any]) -> None:
        """Processes an incoming event from the event broker."""
        try:
            # Routes changed somewhere (services/baseline_route._announce_topology_change):
            # rebuild this worker's map first, then tell its browsers, so their reload sees it.
            if channel == "NODE_STATE_CHANGE" and data.get("type") == "TOPOLOGY_UPDATED":
                self._schedule_topology_rebuild(data)
                return

            # 1. Synchronize API in-memory topology manager
            if channel == "STATE_TRANSITION":
                endpoint_id = data.get("endpoint_id")
                new_state = data.get("current_state") or data.get("operational_state")
                if endpoint_id and new_state:
                    await topology_manager.update_node_status(
                        str(endpoint_id), str(new_state)
                    )

                    # Auto-discovery on recovery for endpoints with missing baseline routes
                    if str(new_state).upper() == "UP":
                        await self._maybe_trigger_auto_discovery(
                            str(endpoint_id), data.get("ip_address")
                        )
                    # A device others route through went down or came back: re-trace them.
                    if str(new_state).upper() in ("UP", "DOWN"):
                        await self._maybe_trace_dependents(
                            str(endpoint_id), str(new_state).upper(), data.get("ip_address")
                        )
            elif channel == "ROUTE_HINT":
                await self._handle_route_hint(data)
            elif channel == "NODE_STATE_CHANGE":
                node_id = data.get("node_id") or data.get("endpoint_id")
                new_state = data.get("operational_state") or data.get("new_state")
                if node_id and new_state:
                    await topology_manager.update_node_status(
                        str(node_id), str(new_state)
                    )

            # 2. Push SSE event payload to connected browser streams
            await broadcast_sse_event(channel, data)
        except Exception as exc:
            logger.error(
                "TelemetryRelay: failed to process event on channel '%s': %s",
                channel,
                exc,
            )

    async def _handle_route_hint(self, data: Dict[str, Any], now: Optional[float] = None) -> bool:
        # A link on a polled device went down or up (v3.3.6, alert leader): re-trace the paths
        # through that device, with the transit trigger's wait and cooldown.
        if data.get("type") == "SNMP_LINK":
            if data.get("endpoint_id") and data.get("state") in ("DOWN", "UP"):
                await self._maybe_trace_dependents(str(data["endpoint_id"]), data["state"], data.get("ip_address"))
            return False
        return await self._route_hint(data, now)

    async def _route_hint(self, data: Dict[str, Any], now: Optional[float] = None) -> bool:
        """Re-traces one endpoint after flows showed its traffic switch path. Returns True if queued.

        Only endpoints that are UP and have scheduled discovery on: the baseline is the
        last-known-good path RCA compares against, so it is never re-taken during an outage.
        """
        endpoint_id = str(data.get("endpoint_id") or "")
        if not endpoint_id:
            return False
        now = time.monotonic() if now is None else now
        # Flow route hints and reply-TTL hints are separate signals with their own cooldowns.
        trigger = "ttl" if data.get("reason") == "ttl" else "route_hint"
        cooldown_key = endpoint_id if trigger == "route_hint" else f"{endpoint_id}:{trigger}"

        last = self._route_hint_last.get(cooldown_key)
        if last is not None and now - last < ROUTE_HINT_COOLDOWN_SECONDS:
            return False
        while self._route_hint_recent and now - self._route_hint_recent[0] >= 60.0:
            self._route_hint_recent.popleft()
        if len(self._route_hint_recent) >= ROUTE_HINT_MAX_PER_MINUTE:
            logger.info("Route hint for %s skipped: targeted traceroute cap reached.", endpoint_id)
            return False

        from uuid import UUID

        from app.services.baseline_route import discovery_in_progress, discovery_route_queue
        from app.services.scheduler_leader import scheduler_leader

        # Every API worker listens on the pub/sub channel so SSE clients on any worker receive
        # the event, but only the elected leader worker enqueues the active traceroute.
        if scheduler_leader._task is not None and not scheduler_leader.is_leader:
            return False

        try:
            ep_uuid = UUID(endpoint_id)
        except ValueError:
            return False
        if ep_uuid in discovery_in_progress:
            return False

        discovery_in_progress.add(ep_uuid)
        try:
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    text("""
                        SELECT host(e.ip_address) AS ip_address
                        FROM endpoints e
                        LEFT JOIN LATERAL (
                            SELECT operational_state FROM endpoint_events
                            WHERE endpoint_id = e.id ORDER BY start_time DESC LIMIT 1
                        ) ev ON TRUE
                        WHERE e.id = CAST(:endpoint_id AS uuid)
                          AND e.endpoint_status != 'DELETED'
                          AND e.enable_scheduled_discovery = TRUE
                          AND COALESCE(ev.operational_state, 'UP') = 'UP'
                    """),
                    {"endpoint_id": endpoint_id},
                )
                row = res.fetchone()
        except Exception:
            discovery_in_progress.discard(ep_uuid)
            raise
        if row is None:
            discovery_in_progress.discard(ep_uuid)
            return False

        self._route_hint_last[cooldown_key] = now
        self._route_hint_recent.append(now)
        await discovery_route_queue.put((ep_uuid, str(row[0]), trigger))
        logger.info(
            "Route change seen in flows for %s (exporter %s, out if%s via %s): re-tracing its route.",
            endpoint_id,
            data.get("exporter_id"),
            data.get("new_out_if"),
            data.get("new_next_hop") or "no next hop",
        )
        return True

    # Route changes arrive in bursts (a failover moves many endpoints): one rebuild per burst.
    TOPOLOGY_REBUILD_DELAY = 2.0

    def _schedule_topology_rebuild(self, data: Dict[str, Any]) -> None:
        self._rebuild_reasons = getattr(self, "_rebuild_reasons", set())
        self._rebuild_reasons.add(str(data.get("reason") or "ROUTES_CHANGED"))
        task = getattr(self, "_rebuild_task", None)
        if task is not None and not task.done():
            return
        self._rebuild_task = asyncio.create_task(self._rebuild_topology_soon())

    async def _rebuild_topology_soon(self) -> None:
        await asyncio.sleep(self.TOPOLOGY_REBUILD_DELAY)
        reasons, self._rebuild_reasons = self._rebuild_reasons, set()
        try:
            async with AsyncSessionLocal() as session:
                await topology_manager.full_rebuild(session)
        except Exception as exc:
            logger.warning("Topology rebuild after a route change failed: %s", exc)
        # FLEET_DISCOVERY_COMPLETED wins: the map ends its "discovering" indicator on it.
        reason = "FLEET_DISCOVERY_COMPLETED" if "FLEET_DISCOVERY_COMPLETED" in reasons else sorted(reasons)[0]
        await broadcast_sse_event("TOPOLOGY_UPDATED", {"reason": reason})

    async def _maybe_trace_dependents(
        self, endpoint_id: str, new_state: str, ip_address: Optional[str], delay: Optional[float] = None
    ) -> List[str]:
        """Transit trigger. Finds the endpoints whose route passes through this device and, after
        one monitoring cycle, re-traces those still UP (leader worker only). Returns the ids it
        will look at again (for tests); the re-traces themselves happen in the background.

        DOWN: endpoints whose CURRENT path goes through it (they must have moved if still up).
        UP: endpoints whose NORMAL path goes through it but whose current path doesn't (they
        may now move back). Matching is by the device's monitored address appearing as a hop,
        so a router that answers traceroute from another interface is not recognised."""
        from app.services.scheduler_leader import scheduler_leader

        if scheduler_leader._task is not None and not scheduler_leader.is_leader:
            return []
        try:
            async with AsyncSessionLocal() as session:
                if not ip_address:
                    res = await session.execute(
                        text("SELECT host(ip_address) FROM endpoints WHERE id = CAST(:id AS uuid)"),
                        {"id": endpoint_id},
                    )
                    row = res.fetchone()
                    ip_address = str(row[0]) if row else None
                if not ip_address:
                    return []
                # Every address the device is known by (v3.3.6): its SNMP-reported addresses
                # too, so a router that answers traceroute from another interface still counts.
                addresses = [str(ip_address).split("/")[0]]
                try:
                    extra = await session.execute(
                        text("SELECT addresses FROM snmp_device_state WHERE endpoint_id = CAST(:id AS uuid)"),
                        {"id": endpoint_id},
                    )
                    row = extra.fetchone()
                    addresses += [a for a in ((row[0] if row else None) or []) if a and a not in addresses]
                except Exception:
                    pass
                probes = {f"probe{i}": [{"ip": a}] for i, a in enumerate(addresses[:20])}
                on_current = " OR ".join(f"r.hops @> CAST(:{k} AS jsonb)" for k in probes)
                on_normal = " OR ".join(f"r.normal_hops @> CAST(:{k} AS jsonb)" for k in probes)
                where = (
                    f"({on_current})"
                    if new_state == "DOWN"
                    else f"({on_normal}) AND NOT ({on_current})"
                )
                res = await session.execute(
                    text(f"""
                        SELECT r.endpoint_id, host(e.ip_address)
                        FROM endpoint_baseline_routes r
                        JOIN endpoints e ON e.id = r.endpoint_id
                        WHERE {where}
                          AND r.endpoint_id <> CAST(:id AS uuid)
                          AND e.endpoint_status != 'DELETED'
                          AND e.enable_scheduled_discovery = TRUE
                    """),
                    {**probes, "id": endpoint_id},
                )
                dependents = [(str(r[0]), str(r[1])) for r in res.fetchall()]
        except Exception as exc:
            logger.debug("Transit trigger lookup failed for %s: %s", endpoint_id, exc)
            return []
        if not dependents:
            return []

        from app.config import settings

        wait = delay if delay is not None else float(getattr(settings.monitoring, "interval_seconds", 60)) + 5.0
        logger.info(
            "Transit device %s is %s: re-tracing %d endpoint(s) routed through it after one cycle.",
            endpoint_id, new_state, len(dependents),
        )
        task = asyncio.create_task(self._retrace_after(dependents, new_state, wait))
        self._transit_tasks.add(task)
        task.add_done_callback(self._transit_tasks.discard)
        return [d[0] for d in dependents]

    async def _retrace_after(self, dependents: List[tuple], direction: str, wait: float) -> int:
        """Waits a cycle, keeps the dependents that are still UP, and queues their re-traces:
        at most TRANSIT_MAX_PER_MINUTE now, the rest a minute later each batch. Returns how
        many were queued or scheduled."""
        from uuid import UUID

        from app.services.baseline_route import discovery_in_progress, discovery_route_queue

        await asyncio.sleep(wait)
        ids = [d[0] for d in dependents]
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                text("""
                    SELECT e.id FROM endpoints e
                    LEFT JOIN LATERAL (
                        SELECT operational_state FROM endpoint_events
                        WHERE endpoint_id = e.id ORDER BY start_time DESC LIMIT 1
                    ) ev ON TRUE
                    WHERE e.id = ANY(CAST(:ids AS uuid[]))
                      AND COALESCE(ev.operational_state, 'UP') = 'UP'
                """),
                {"ids": tuple(ids)},
            )
            still_up = {str(r[0]) for r in res.fetchall()}

        now = time.monotonic()
        queued = 0
        for ep_id, ip in dependents:
            if ep_id not in still_up:
                continue
            key = f"{ep_id}:transit:{direction}"
            last = self._transit_last.get(key)
            if last is not None and now - last < TRANSIT_COOLDOWN_SECONDS:
                continue
            self._transit_last[key] = now
            batch = queued // TRANSIT_MAX_PER_MINUTE
            queued += 1
            ep_uuid = UUID(ep_id)

            async def _put(ep_uuid=ep_uuid, ip=ip, later=batch * 60.0):
                if later:
                    await asyncio.sleep(later)
                if ep_uuid in discovery_in_progress:
                    return
                discovery_in_progress.add(ep_uuid)
                await discovery_route_queue.put((ep_uuid, ip, "transit"))

            if batch == 0:
                await _put()
            else:
                t = asyncio.create_task(_put())
                self._transit_tasks.add(t)
                t.add_done_callback(self._transit_tasks.discard)
        return queued

    async def _maybe_trigger_auto_discovery(
        self, endpoint_id_str: str, ip_address: Optional[str]
    ) -> None:
        """
        Auto-enqueues route discovery for endpoints recovering to UP state
        if they currently lack verified baseline routes, protected by in-progress lock.
        """
        try:
            from uuid import UUID
            from app.services.baseline_route import (
                discovery_in_progress,
                discovery_route_queue,
            )
            from app.services.scheduler_leader import scheduler_leader

            if scheduler_leader._task is not None and not scheduler_leader.is_leader:
                return

            ep_uuid = UUID(endpoint_id_str)
            if ep_uuid in discovery_in_progress:
                return

            known_hops = topology_manager._baseline_routes.get(endpoint_id_str, [])
            if not known_hops:
                resolved_ip = ip_address
                if not resolved_ip:
                    node = topology_manager.get_node(endpoint_id_str)
                    if node:
                        resolved_ip = node.get("ip_address")

                if resolved_ip:
                    discovery_in_progress.add(ep_uuid)
                    await discovery_route_queue.put((ep_uuid, str(resolved_ip)))
                    logger.info(
                        "Auto-enqueued route discovery for recovered endpoint %s (%s)",
                        endpoint_id_str,
                        resolved_ip,
                    )
        except Exception as exc:
            logger.debug(
                "Failed to evaluate auto-discovery on recovery for %s: %s",
                endpoint_id_str,
                exc,
            )


# Global singleton instance
telemetry_relay = TelemetryRelay()
