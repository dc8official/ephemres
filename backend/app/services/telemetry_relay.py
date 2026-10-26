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
# cannot set off a traceroute storm.
ROUTE_HINT_COOLDOWN_SECONDS = 900
ROUTE_HINT_MAX_PER_MINUTE = 10


class TelemetryRelay:
    """
    Inter-process telemetry relay bridge.
    Subscribes to monitoring engine pub/sub channels (STATE_TRANSITION and NODE_STATE_CHANGE)
    via the active EventBroker (PostgreSQL LISTEN/NOTIFY or Redis Pub/Sub),
    updates API server in-memory DAG topology_manager, and broadcasts SSE envelopes
    to connected browser clients.
    """

    def __init__(self) -> None:
        self._running: bool = False
        self._tasks: List[asyncio.Task] = []
        self._route_hint_last: Dict[str, float] = {}
        self._route_hint_recent: Deque[float] = collections.deque()

    async def start(self) -> None:
        """Starts background listener tasks for telemetry pub/sub channels."""
        if self._running:
            return
        self._running = True
        self._tasks.append(
            asyncio.create_task(
                self._listen_channel("STATE_TRANSITION"),
                name="telemetry_relay_state_transition",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._listen_channel("NODE_STATE_CHANGE"),
                name="telemetry_relay_node_state_change",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._listen_channel("RCA_INCIDENT"),
                name="telemetry_relay_rca_incident",
            )
        )
        self._tasks.append(
            asyncio.create_task(
                self._listen_channel("ROUTE_HINT"),
                name="telemetry_relay_route_hint",
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
                broker = driver_manager.get_event_broker()
                async for event in broker.subscribe(channel):
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
        """Re-traces one endpoint after flows showed its traffic switch path. Returns True if queued.

        Only endpoints that are UP and have scheduled discovery on: the baseline is the
        last-known-good path RCA compares against, so it is never re-taken during an outage.
        """
        endpoint_id = str(data.get("endpoint_id") or "")
        if not endpoint_id:
            return False
        now = time.monotonic() if now is None else now

        last = self._route_hint_last.get(endpoint_id)
        if last is not None and now - last < ROUTE_HINT_COOLDOWN_SECONDS:
            return False
        while self._route_hint_recent and now - self._route_hint_recent[0] >= 60.0:
            self._route_hint_recent.popleft()
        if len(self._route_hint_recent) >= ROUTE_HINT_MAX_PER_MINUTE:
            logger.info("Route hint for %s skipped: targeted traceroute cap reached.", endpoint_id)
            return False

        from uuid import UUID

        from app.services.baseline_route import discovery_in_progress, discovery_route_queue

        try:
            ep_uuid = UUID(endpoint_id)
        except ValueError:
            return False
        if ep_uuid in discovery_in_progress:
            return False

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
        if row is None:
            return False

        self._route_hint_last[endpoint_id] = now
        self._route_hint_recent.append(now)
        discovery_in_progress.add(ep_uuid)
        await discovery_route_queue.put((ep_uuid, str(row[0])))
        logger.info(
            "Route change seen in flows for %s (exporter %s, out if%s via %s): re-tracing its route.",
            endpoint_id,
            data.get("exporter_id"),
            data.get("new_out_if"),
            data.get("new_next_hop") or "no next hop",
        )
        return True

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
