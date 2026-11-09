"""Maps the IP addresses in flows to Ephemres endpoints, and decides which senders are exporters."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional, Set, Tuple
from uuid import UUID

try:
    from app.core.orm import select
except ModuleNotFoundError:  # pragma: no cover
    select = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class FlowCorrelator:
    """
    In-memory dual-role correlation engine providing O(1) matching for:
    - Exporters (devices sending NetFlow/IPFIX, matching primary IP or flow_exporter_ips aliases).
    - Traffic Participants (endpoints communicating across the network).

    Tracks unmatched exporters in FastStore ('flow:unmatched:<ip>') with a 15-minute TTL.

    The maps are rebuilt from the endpoints table at start, every 60 s, and at once when the
    API announces a change on channel:registry_sync. Any endpoint's primary IP is accepted as an
    exporter, plus the extra addresses in its flow_exporter_ips (a router often exports from a
    loopback or management address that is not the one Ephemres pings).
    """

    def __init__(self, db_session_factory: Any, store_client: Any = None, *, redis_client: Any = None):
        self.db_factory = db_session_factory
        self.store = store_client if store_client is not None else redis_client

        # IP -> Endpoint UUID
        self._exporter_map: Dict[str, UUID] = {}
        self._participant_map: Dict[str, UUID] = {}
        self._unmatched_local: Dict[str, float] = {}
        # Flows seen per unmatched exporter IP since its last FastStore write.
        self._unmatched_counts: Dict[str, int] = {}

        self._lock = asyncio.Lock()
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._pubsub_task: Optional[asyncio.Task] = None

    @property
    def redis(self) -> Any:
        return self.store

    @redis.setter
    def redis(self, val: Any) -> None:
        self.store = val

    def correlate(
        self, exporter_ip: str, src_ip: str, dst_ip: str
    ) -> Tuple[Optional[UUID], Optional[UUID], Optional[UUID]]:
        """
        O(1) in-memory lookup returning:
        (exporter_id, src_endpoint_id, dst_endpoint_id)
        """
        exp_clean = exporter_ip.split("/")[0].strip()
        src_clean = src_ip.split("/")[0].strip()
        dst_clean = dst_ip.split("/")[0].strip()

        exporter_id = self._exporter_map.get(exp_clean)
        src_endpoint_id = self._participant_map.get(src_clean)
        dst_endpoint_id = self._participant_map.get(dst_clean)

        if exporter_id is None:
            self._record_unmatched_exporter(exp_clean)

        return exporter_id, src_endpoint_id, dst_endpoint_id

    def admit_exporter(self, exporter_ip: str, records: int = 1) -> bool:
        """Whether flows from this sender are kept (the collector asks before parsing a packet).

        Only enrolled exporters (an endpoint's primary IP or one of its flow_exporter_ips) are
        kept. Any other sender is recorded as an unmatched exporter candidate, so the Traffic
        page can offer to enroll it, and its packets are dropped: anyone who can reach the flow
        ports could otherwise fill the flow buffer and the database.
        """
        ip = exporter_ip.split("/")[0].strip()
        if ip in self._exporter_map:
            return True
        self._record_unmatched_exporter(ip, max(int(records), 1))
        return False

    def _record_unmatched_exporter(self, ip: str, flows: int = 1) -> None:
        now = time.time()
        self._unmatched_counts[ip] = self._unmatched_counts.get(ip, 0) + flows
        last_recorded = self._unmatched_local.get(ip, 0.0)
        # Rate-limit FastStore updates to once every 10 seconds per unmatched IP
        if now - last_recorded > 10.0:
            # Flow rate since the previous write; unknown on the first sighting.
            flows = self._unmatched_counts.pop(ip, 0)
            flows_per_sec = round(flows / (now - last_recorded), 2) if last_recorded else None
            self._unmatched_local[ip] = now
            if self.store:
                asyncio.create_task(self._async_record_redis_unmatched(ip, now, flows_per_sec))

    async def _async_record_redis_unmatched(
        self, ip: str, timestamp: float, flows_per_sec: Optional[float] = None
    ) -> None:
        try:
            key = f"flow:unmatched:{ip}"
            await self.store.set(key, str(timestamp), ex=900)
            if flows_per_sec is not None:
                await self.store.set(f"flow:unmatched:{ip}:fps", str(flows_per_sec), ex=900)
            await self.store.sadd("flow:unmatched:set", ip)
            # Last-seen time per IP in a sorted set, keeping the past 24 h and at most 1,000 IPs,
            # so a scan from many spoofed sources can't grow it without limit.
            await self.store.zadd("flow:unmatched:zset", {ip: timestamp})
            cutoff = timestamp - 86400.0
            await self.store.zremrangebyscore("flow:unmatched:zset", "-inf", cutoff)
            card = await self.store.zcard("flow:unmatched:zset")
            if card > 1000:
                await self.store.zremrangebyrank("flow:unmatched:zset", 0, card - 1001)
        except Exception as e:
            logger.debug("Failed to record unmatched exporter in FastStore: %s", e)

    async def refresh_from_db(self) -> None:
        """Loads all active endpoints, their primary IPs, and flow_exporter_ips aliases."""
        from app.models.endpoint import Endpoint

        async with self.db_factory() as db:
            stmt = select(Endpoint).where(
                Endpoint.deleted_at.is_(None),
                Endpoint.endpoint_status != "DELETED",
            )
            res = await db.execute(stmt)
            endpoints = res.scalars().all()

        new_exporter_map: Dict[str, UUID] = {}
        new_participant_map: Dict[str, UUID] = {}

        for ep in endpoints:
            primary_ip = str(ep.ip_address).split("/")[0].strip()
            new_participant_map[primary_ip] = ep.id
            new_exporter_map[primary_ip] = ep.id

            # Map all alias IPs configured on this exporter
            aliases = getattr(ep, "flow_exporter_ips", None) or []
            for alias in aliases:
                clean_alias = str(alias).split("/")[0].strip()
                if clean_alias:
                    new_exporter_map[clean_alias] = ep.id

        async with self._lock:
            self._exporter_map = new_exporter_map
            self._participant_map = new_participant_map

        logger.debug(
            "FlowCorrelator: Synchronized %d exporters and %d participants from DB.",
            len(new_exporter_map),
            len(new_participant_map),
        )

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(60.0)
                await self.refresh_from_db()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("FlowCorrelator DB refresh failed: %s", e)

    async def _pubsub_listener(self) -> None:
        if not self.store:
            return

        while self._running:
            try:
                pubsub = self.store.pubsub()
                await pubsub.subscribe("channel:registry_sync")
                async for msg in pubsub.listen():
                    if not self._running:
                        break
                    if msg and msg.get("type") == "message":
                        try:
                            # Trigger immediate refresh upon registry mutations
                            await self.refresh_from_db()
                        except Exception as e:
                            logger.error("Failed to refresh correlator on registry sync: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("FlowCorrelator pubsub listener disconnected (%s). Retrying in 5s...", e)
                await asyncio.sleep(5.0)

    async def start(self) -> None:
        self._running = True
        await self.refresh_from_db()
        self._sync_task = asyncio.create_task(self._poll_loop())
        if self.store:
            self._pubsub_task = asyncio.create_task(self._pubsub_listener())
        logger.info("FlowCorrelator initialized with O(1) routing table.")

    async def stop(self) -> None:
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
        if self._pubsub_task:
            self._pubsub_task.cancel()
        logger.info("FlowCorrelator stopped.")
