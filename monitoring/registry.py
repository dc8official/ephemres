from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple
from uuid import UUID

logger = logging.getLogger(__name__)


@dataclass
class MonitoredEndpoint:
    id: UUID
    ip_address: str
    hostname: str
    device_type: str = "SERVER"
    location: Optional[str] = None
    endpoint_status: str = "ACTIVE"
    monitoring_enabled: bool = True
    allow_incident_trace: bool = True
    allow_topology_discovery: bool = True
    enable_rca: bool = True
    enable_scheduled_discovery: bool = True
    is_l2_segment: bool = False
    manual_parent_id: Optional[UUID] = None
    created_at: Optional[datetime] = None
    baseline_mean: Optional[float] = None
    baseline_stddev: Optional[float] = None


def _get_val(obj: Any, key: str, default: Any = None) -> Any:
    """Extracts an attribute from an ORM model, dataclass, or dictionary safely."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    if hasattr(obj, key):
        val = getattr(obj, key, default)
        return default if val is None else val
    return default


def _has_val(obj: Any, key: str) -> bool:
    """Checks if a key/attribute is present in an ORM model, dataclass, or dictionary."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        return key in obj
    return hasattr(obj, key)


class EndpointRegistry:
    """
    Concurrent-safe in-memory registry managing active monitoring targets,
    operational toggles, baseline caches, and live task lifecycles.
    """

    def __init__(self) -> None:
        self._endpoints: Dict[UUID, MonitoredEndpoint] = {}
        self._running_tasks: Dict[UUID, asyncio.Task] = {}
        self._diagnostic_tasks: Dict[UUID, Set[asyncio.Task]] = {}
        # endpoint_id -> (time.monotonic() of the last incident trace, detailed state it traced)
        self._last_incident_trace: Dict[UUID, Tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    @property
    def endpoints_count(self) -> int:
        return len(self._endpoints)

    @property
    def running_tasks_count(self) -> int:
        return len(self._running_tasks)

    async def add_endpoint(
        self,
        ep_data: dict[str, Any] | Any,
        spawn_coro_fn: Optional[Callable[[MonitoredEndpoint], Coroutine]] = None,
    ) -> MonitoredEndpoint:
        """
        Ingests a new or updated target into the in-memory registry,
        caches toggles and baseline stats, and optionally spawns a sweep task.
        """
        ep_id = _get_val(ep_data, "id")
        if isinstance(ep_id, str):
            ep_id = UUID(ep_id)
        if not ep_id:
            raise ValueError("Endpoint data must contain a valid UUID id.")

        ip_addr = str(_get_val(ep_data, "ip_address", "")).split("/")[0].strip()
        hostname = str(_get_val(ep_data, "hostname", ip_addr))
        device_type = str(_get_val(ep_data, "device_type", "SERVER"))
        location = _get_val(ep_data, "location")
        endpoint_status = str(_get_val(ep_data, "endpoint_status", "ACTIVE")).upper()
        monitoring_enabled = bool(_get_val(ep_data, "monitoring_enabled", True))
        allow_incident_trace = bool(_get_val(ep_data, "allow_incident_trace", True))
        allow_topology_discovery = bool(_get_val(ep_data, "allow_topology_discovery", True))
        enable_rca = bool(_get_val(ep_data, "enable_rca", True))
        enable_scheduled_discovery = bool(_get_val(ep_data, "enable_scheduled_discovery", True))
        is_l2_segment = bool(_get_val(ep_data, "is_l2_segment", False))
        manual_parent_id = _get_val(ep_data, "manual_parent_id")
        if isinstance(manual_parent_id, str):
            manual_parent_id = UUID(manual_parent_id)

        created_at = _get_val(ep_data, "created_at")
        baseline_mean = _get_val(ep_data, "baseline_mean")
        baseline_stddev = _get_val(ep_data, "baseline_stddev")

        endpoint = MonitoredEndpoint(
            id=ep_id,
            ip_address=ip_addr,
            hostname=hostname,
            device_type=device_type,
            location=location,
            endpoint_status=endpoint_status,
            monitoring_enabled=monitoring_enabled,
            allow_incident_trace=allow_incident_trace,
            allow_topology_discovery=allow_topology_discovery,
            enable_rca=enable_rca,
            enable_scheduled_discovery=enable_scheduled_discovery,
            is_l2_segment=is_l2_segment,
            manual_parent_id=manual_parent_id,
            created_at=created_at,
            baseline_mean=baseline_mean,
            baseline_stddev=baseline_stddev,
        )

        async with self._lock:
            self._endpoints[ep_id] = endpoint

            # If target is active and monitoring enabled, ensure a sweep task is running
            should_run = endpoint.endpoint_status == "ACTIVE" and endpoint.monitoring_enabled
            if should_run and ep_id not in self._running_tasks and spawn_coro_fn is not None:
                logger.info(
                    "Registry: Spawning monitoring sweep task for %s (%s)",
                    ep_id,
                    endpoint.ip_address,
                )
                task = asyncio.create_task(spawn_coro_fn(endpoint))
                self._running_tasks[ep_id] = task

                def _on_done(t: asyncio.Task, target_id: UUID = ep_id):
                    self._running_tasks.pop(target_id, None)
                    if not t.cancelled() and t.exception():
                        logger.error(
                            "Monitoring task for endpoint %s failed: %s",
                            target_id,
                            t.exception(),
                        )

                task.add_done_callback(_on_done)
            elif not should_run and ep_id in self._running_tasks:
                logger.info("Registry: Cancelling sweep task for deactivated target %s", ep_id)
                self._running_tasks[ep_id].cancel()
                self._running_tasks.pop(ep_id, None)

        return endpoint

    async def update_endpoint(self, ep_data: dict[str, Any] | Any) -> Optional[MonitoredEndpoint]:
        """Updates target attributes in place without losing active sweep continuity."""
        ep_id = _get_val(ep_data, "id")
        if isinstance(ep_id, str):
            ep_id = UUID(ep_id)
        if not ep_id:
            return None

        async with self._lock:
            existing = self._endpoints.get(ep_id)
            if not existing:
                return None

            if _has_val(ep_data, "ip_address"):
                existing.ip_address = str(
                    _get_val(ep_data, "ip_address", existing.ip_address)
                ).split("/")[0].strip()
            if _has_val(ep_data, "hostname"):
                existing.hostname = str(
                    _get_val(ep_data, "hostname", existing.hostname)
                )
            if _has_val(ep_data, "endpoint_status"):
                existing.endpoint_status = str(
                    _get_val(ep_data, "endpoint_status", existing.endpoint_status)
                ).upper()
            if _has_val(ep_data, "monitoring_enabled"):
                val = _get_val(ep_data, "monitoring_enabled")
                if val is not None:
                    existing.monitoring_enabled = bool(val)
            if _has_val(ep_data, "allow_incident_trace"):
                val = _get_val(ep_data, "allow_incident_trace")
                if val is not None:
                    existing.allow_incident_trace = bool(val)
            if _has_val(ep_data, "allow_topology_discovery"):
                val = _get_val(ep_data, "allow_topology_discovery")
                if val is not None:
                    existing.allow_topology_discovery = bool(val)
            if _has_val(ep_data, "enable_rca"):
                val = _get_val(ep_data, "enable_rca")
                if val is not None:
                    existing.enable_rca = bool(val)
            if _has_val(ep_data, "enable_scheduled_discovery"):
                val = _get_val(ep_data, "enable_scheduled_discovery")
                if val is not None:
                    existing.enable_scheduled_discovery = bool(val)
            if _has_val(ep_data, "is_l2_segment"):
                val = _get_val(ep_data, "is_l2_segment")
                if val is not None:
                    existing.is_l2_segment = bool(val)
            if _has_val(ep_data, "manual_parent_id"):
                val = _get_val(ep_data, "manual_parent_id")
                if isinstance(val, str):
                    val = UUID(val)
                existing.manual_parent_id = val

            # Check if task needs cancellation due to disabling
            should_run = existing.endpoint_status == "ACTIVE" and existing.monitoring_enabled
            if not should_run and ep_id in self._running_tasks:
                logger.info("Registry: Cancelling sweep task for updated disabled target %s", ep_id)
                self._running_tasks[ep_id].cancel()
                self._running_tasks.pop(ep_id, None)

            return existing

    async def remove_endpoint(self, endpoint_id: UUID) -> None:
        """
        Safely evicts target from active sweep and cancels any running
        background diagnostic/monitoring tasks.
        """
        async with self._lock:
            # 1. Cancel main monitoring task
            if endpoint_id in self._running_tasks:
                logger.info("Registry: Cancelling active monitoring task for %s", endpoint_id)
                task = self._running_tasks.pop(endpoint_id)
                task.cancel()

            # 2. Cancel in-flight background diagnostic tasks
            if endpoint_id in self._diagnostic_tasks:
                diag_tasks = self._diagnostic_tasks.pop(endpoint_id, set())
                for t in diag_tasks:
                    if not t.done():
                        logger.info("Registry: Cancelling in-flight diagnostic task for %s", endpoint_id)
                        t.cancel()

            # 3. Evict from memory
            self._endpoints.pop(endpoint_id, None)
            self._last_incident_trace.pop(endpoint_id, None)

    def register_diagnostic_task(self, endpoint_id: UUID, task: asyncio.Task) -> None:
        """Tracks in-flight diagnostic tasks for lifetime management."""
        if endpoint_id not in self._diagnostic_tasks:
            self._diagnostic_tasks[endpoint_id] = set()
        self._diagnostic_tasks[endpoint_id].add(task)

        def _cleanup(t: asyncio.Task, ep_id: UUID = endpoint_id):
            tasks_set = self._diagnostic_tasks.get(ep_id)
            if tasks_set:
                tasks_set.discard(t)
                if not tasks_set:
                    self._diagnostic_tasks.pop(ep_id, None)

        task.add_done_callback(_cleanup)

    def has_active_diagnostic_task(self, endpoint_id: UUID) -> bool:
        """Returns True if there is currently an in-flight diagnostic trace task for this endpoint."""
        tasks = self._diagnostic_tasks.get(endpoint_id)
        if not tasks:
            return False
        return any(not t.done() for t in tasks)

    def incident_trace_due(self, endpoint_id: UUID, detailed_state: str, cooldown_seconds: float) -> bool:
        """True if a failing endpoint should get a new incident trace now.

        Not while one is in flight; otherwise when its confirmed state differs from the last traced
        one, or `cooldown_seconds` have passed. Without this a DOWN endpoint was re-traced on every
        failed sweep, forever.
        """
        if self.has_active_diagnostic_task(endpoint_id):
            return False
        last = self._last_incident_trace.get(endpoint_id)
        if last is None:
            return True
        last_at, last_state = last
        return detailed_state != last_state or time.monotonic() - last_at >= cooldown_seconds

    def record_incident_trace(self, endpoint_id: UUID, detailed_state: str) -> None:
        """Marks an incident trace as started for `detailed_state` (see incident_trace_due)."""
        self._last_incident_trace[endpoint_id] = (time.monotonic(), detailed_state)

    def get_endpoint(self, endpoint_id: UUID) -> Optional[MonitoredEndpoint]:
        """Retrieves cached endpoint with operational toggles without database locks."""
        return self._endpoints.get(endpoint_id)

    def get_toggles(self, endpoint_id: UUID) -> dict[str, bool]:
        """Returns in-memory operational toggles to prevent in-cycle database queries."""
        ep = self._endpoints.get(endpoint_id)
        if not ep:
            return {
                "allow_incident_trace": True,
                "enable_rca": True,
                "allow_topology_discovery": True,
            }
        return {
            "allow_incident_trace": ep.allow_incident_trace,
            "enable_rca": ep.enable_rca,
            "allow_topology_discovery": ep.allow_topology_discovery,
        }

    def list_active_endpoints(self) -> List[MonitoredEndpoint]:
        """Lists all active and monitored endpoints."""
        return [
            ep
            for ep in self._endpoints.values()
            if ep.endpoint_status == "ACTIVE" and ep.monitoring_enabled
        ]


# Global singleton registry instance
endpoint_registry = EndpointRegistry()
