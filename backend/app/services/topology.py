from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import collections
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class EmptyQueryResult:
    def fetchall(self) -> list:
        return []

    def fetchone(self) -> None:
        return None


async def _safe_execute(db: AsyncSession, query: Any) -> Any:
    try:
        return await db.execute(query)
    except Exception as exc:
        logger.error("Topology database query failed: %s", exc, exc_info=True)
        return EmptyQueryResult()


def get_subnet_group(ip_str: Optional[str]) -> Optional[str]:
    """
    Computes the IPv4 /24 subnet CIDR block for an IP address for visual clustering.
    """
    if not ip_str:
        return None
    try:
        net = ipaddress.ip_network(f"{ip_str}/24", strict=False)
        return str(net)
    except Exception:
        return None


class TopologyGraphManager:
    """
    In-Memory Event-Driven Directed Acyclic Graph (DAG) Topology Service.

    Thread-safe Singleton maintaining pre-constructed topology nodes, edges,
    and pre-serialized JSON graph for O(1) read latency on API endpoints.
    """

    _instance: Optional[TopologyGraphManager] = None
    _lock = asyncio.Lock()

    def __init__(self) -> None:
        self._nodes: Dict[str, Dict[str, Any]] = {}
        self._edges: Set[Tuple[str, str]] = set()
        self._transit_children: Dict[str, Set[str]] = {}
        self._monitored_by_id: Dict[str, Dict[str, Any]] = {}
        self._monitored_by_ip: Dict[str, str] = {}
        self._disabled_topology_ep_ids: Set[str] = set()
        self._baseline_routes: Dict[str, List[Dict[str, Any]]] = {}
        self._failed_hop_ips: Set[str] = set()
        self._failed_endpoint_ids: Set[str] = set()

        self._cached_graph: Dict[str, Any] = {"nodes": [], "edges": []}
        self._cached_graph_json: str = json.dumps({"nodes": [], "edges": []})
        self._initialized: bool = False

    @classmethod
    def get_instance(cls) -> TopologyGraphManager:
        if cls._instance is None:
            cls._instance = TopologyGraphManager()
        return cls._instance

    def get_cached_graph(self) -> Dict[str, Any]:
        """
        Returns the pre-constructed in-memory graph dictionary in O(1) time with 0 DB queries.
        """
        return self._cached_graph

    def get_cached_graph_json(self) -> str:
        """
        Returns the pre-serialized JSON string of the graph in O(1) time.
        """
        return self._cached_graph_json

    def get_node(self, endpoint_id: Any) -> Optional[Dict[str, Any]]:
        """
        Retrieves in-memory topology node metadata by endpoint_id in O(1) time.
        """
        if not endpoint_id:
            return None
        return self._nodes.get(str(endpoint_id))

    async def full_rebuild(self, db: AsyncSession) -> Dict[str, Any]:
        """
        Asynchronously rebuilds the entire DAG graph in memory from PostgreSQL.
        Triggered on app startup, bulk endpoint creation, or midnight route discovery passes.
        """
        async with self._lock:
            # 1. Fetch endpoints & current state
            endpoints_query = text("""
                SELECT
                    e.id,
                    e.hostname,
                    host(e.ip_address) AS ip_address,
                    e.device_type,
                    e.location,
                    e.endpoint_status,
                    e.allow_topology_discovery,
                    e.manual_parent_id,
                    e.is_l2_segment,
                    COALESCE(ev.operational_state, 'UP') AS operational_state,
                    COALESCE(ev.detailed_state, 'UP') AS detailed_state
                FROM endpoints e
                LEFT JOIN LATERAL (
                    SELECT operational_state, detailed_state
                    FROM endpoint_events
                    WHERE endpoint_id = e.id
                    ORDER BY start_time DESC
                    LIMIT 1
                ) ev ON TRUE
                WHERE e.endpoint_status != 'DELETED'
            """)
            ep_result = await _safe_execute(db, endpoints_query)
            ep_rows = ep_result.fetchall()

            monitored_by_id: Dict[str, Dict[str, Any]] = {}
            monitored_by_ip: Dict[str, str] = {}
            disabled_topology_ep_ids: Set[str] = set()

            for row in ep_rows:
                ep_id = str(row.id)
                ip = str(row.ip_address)
                if not bool(row.allow_topology_discovery):
                    disabled_topology_ep_ids.add(ep_id)
                    continue

                monitored_by_id[ep_id] = {
                    "id": ep_id,
                    "label": row.hostname or ip,
                    "ip_address": ip,
                    "node_type": "monitored",
                    "status": row.detailed_state or row.operational_state or "UP",
                    "device_type": row.device_type or "ENDPOINT",
                    "endpoint_id": ep_id,
                    "is_l2_segment": bool(row.is_l2_segment),
                    "manual_parent_id": str(row.manual_parent_id) if row.manual_parent_id else None,
                    "subnet": get_subnet_group(ip),
                }
                monitored_by_ip[ip] = ep_id

            # 2. Fetch baseline routes
            baseline_query = text("SELECT endpoint_id, total_hops, hops FROM endpoint_baseline_routes")
            bl_result = await _safe_execute(db, baseline_query)
            bl_rows = bl_result.fetchall()

            baseline_routes: Dict[str, List[Dict[str, Any]]] = {}
            for row in bl_rows:
                ep_id = str(getattr(row, "endpoint_id", ""))
                hops_raw = getattr(row, "hops", None)
                if hops_raw is None or not isinstance(hops_raw, list):
                    t_data = getattr(row, "trace_data", None)
                    if isinstance(t_data, (str, dict)):
                        if isinstance(t_data, str):
                            try:
                                t_data = json.loads(t_data)
                            except Exception:
                                t_data = {}
                        if isinstance(t_data, dict) and "hops" in t_data:
                            hops_raw = t_data["hops"]

                if isinstance(hops_raw, str):
                    try:
                        hops_raw = json.loads(hops_raw)
                    except Exception:
                        hops_raw = []
                if isinstance(hops_raw, list):
                    baseline_routes[ep_id] = hops_raw

            # Diagnostic traces fallback
            traces_query = text("""
                SELECT DISTINCT ON (endpoint_id) endpoint_id, trace_data
                FROM endpoint_diagnostic_traces
                ORDER BY endpoint_id, timestamp DESC
            """)
            tr_result = await _safe_execute(db, traces_query)
            for row in tr_result.fetchall():
                ep_id = str(row.endpoint_id)
                if ep_id not in baseline_routes:
                    raw_data = row.trace_data
                    if isinstance(raw_data, str):
                        try:
                            raw_data = json.loads(raw_data)
                        except Exception:
                            raw_data = {}
                    if isinstance(raw_data, dict) and "hops" in raw_data:
                        baseline_routes[ep_id] = raw_data["hops"]

            # 3. Fetch active RCA incidents
            incidents_query = text("""
                SELECT endpoint_id, failed_hop_ip, rca_summary
                FROM endpoint_rca_incidents
                WHERE is_resolved = FALSE
            """)
            inc_result = await _safe_execute(db, incidents_query)
            inc_rows = inc_result.fetchall()

            failed_hop_ips: Set[str] = set()
            failed_endpoint_ids: Set[str] = set()
            for row in inc_rows:
                failed_endpoint_ids.add(str(row.endpoint_id))
                if row.failed_hop_ip:
                    failed_hop_ips.add(str(row.failed_hop_ip))

            nodes: Dict[str, Dict[str, Any]] = {}
            edges: Set[Tuple[str, str]] = set()
            transit_children: Dict[str, Set[str]] = {}

            # Root Node
            root_node_id = "root"
            nodes[root_node_id] = {
                "id": root_node_id,
                "label": "Ephemres Engine",
                "type": "root",
                "node_type": "root",
                "state": "UP",
                "status": "UP",
                "ip_address": None,
                "device_type": "MONITORING_ENGINE",
                "endpoint_id": None,
            }

            # Add monitored nodes
            for ep_id, ep_data in monitored_by_id.items():
                node_state = ep_data["status"]
                if ep_data["ip_address"] in failed_hop_ips or ep_id in failed_endpoint_ids:
                    if node_state in ("DOWN", "DOWN-UNSTABLE"):
                        node_state = "FAILURE_POINT"

                nodes[ep_id] = {
                    "id": ep_id,
                    "label": ep_data["label"],
                    "type": "monitored",
                    "node_type": "monitored",
                    "state": node_state,
                    "status": node_state,
                    "ip_address": ep_data["ip_address"],
                    "device_type": ep_data["device_type"],
                    "endpoint_id": ep_id,
                    "is_l2_segment": ep_data["is_l2_segment"],
                    "manual_parent_id": ep_data["manual_parent_id"],
                }

            # 4. Trie/Tree-Merging Algorithm
            for ep_id, ep_data in monitored_by_id.items():
                manual_parent = ep_data.get("manual_parent_id")
                if manual_parent and manual_parent in nodes:
                    edges.add((manual_parent, ep_id))
                    if manual_parent not in transit_children:
                        transit_children[manual_parent] = set()
                    transit_children[manual_parent].add(ep_id)
                    continue

                raw_hops = baseline_routes.get(ep_id, [])
                from app.services.diagnostics import sanitize_traceroute_hops
                hops = sanitize_traceroute_hops(raw_hops)

                if not hops:
                    edges.add((root_node_id, ep_id))
                    if root_node_id not in transit_children:
                        transit_children[root_node_id] = set()
                    transit_children[root_node_id].add(ep_id)
                    continue

                previous_node_id: str = root_node_id
                previous_hop_ip_tag: str = "root"

                for idx, hop in enumerate(hops):
                    hop_ip = hop.get("ip")

                    if hop_ip is None:
                        current_node_id = f"anon_{previous_hop_ip_tag}_to_{ep_id[:8]}"
                        if current_node_id not in nodes:
                            nodes[current_node_id] = {
                                "id": current_node_id,
                                "label": "* * *",
                                "type": "transit",
                                "node_type": "transit",
                                "state": "UP",
                                "status": "UP",
                                "ip_address": None,
                                "device_type": "ANONYMOUS_HOP",
                                "endpoint_id": None,
                            }
                    else:
                        hop_ep_id = monitored_by_ip.get(hop_ip)
                        if hop_ep_id:
                            current_node_id = hop_ep_id
                        else:
                            current_node_id = f"transit:{hop_ip}"
                            if current_node_id not in nodes:
                                transit_state = "FAILURE_POINT" if hop_ip in failed_hop_ips else "UP"
                                nodes[current_node_id] = {
                                    "id": current_node_id,
                                    "label": f"Transit ({hop_ip})",
                                    "type": "transit",
                                    "node_type": "transit",
                                    "state": transit_state,
                                    "status": transit_state,
                                    "ip_address": hop_ip,
                                    "device_type": "TRANSIT_ROUTER",
                                    "endpoint_id": None,
                                    "subnet": get_subnet_group(hop_ip),
                                }
                            elif hop_ip in failed_hop_ips:
                                nodes[current_node_id]["state"] = "FAILURE_POINT"
                                nodes[current_node_id]["status"] = "FAILURE_POINT"

                        previous_hop_ip_tag = hop_ip.replace(".", "_")

                    if previous_node_id != current_node_id:
                        edges.add((previous_node_id, current_node_id))
                        if previous_node_id not in transit_children:
                            transit_children[previous_node_id] = set()
                        transit_children[previous_node_id].add(current_node_id)

                    previous_node_id = current_node_id

                if previous_node_id != ep_id:
                    edges.add((previous_node_id, ep_id))
                    if previous_node_id not in transit_children:
                        transit_children[previous_node_id] = set()
                    transit_children[previous_node_id].add(ep_id)

            # 4b. Synthetic Subnet Clustering for Direct 1-Hop / L2 Devices
            # Endpoints connecting directly to root with no intermediate hops are grouped by subnet
            direct_root_eps = [
                ep_id for (src, ep_id) in edges
                if src == root_node_id and ep_id in monitored_by_id
            ]
            direct_eps_by_subnet: Dict[str, List[str]] = defaultdict(list)
            for d_ep_id in direct_root_eps:
                subnet = monitored_by_id[d_ep_id].get("subnet")
                if subnet:
                    direct_eps_by_subnet[subnet].append(d_ep_id)

            for subnet_str, ep_ids in direct_eps_by_subnet.items():
                if len(ep_ids) >= 2:
                    hub_id = f"subnet:{subnet_str.replace('/', '_').replace('.', '_')}"
                    if hub_id not in nodes:
                        nodes[hub_id] = {
                            "id": hub_id,
                            "label": f"Subnet ({subnet_str})",
                            "type": "subnet_hub",
                            "node_type": "subnet_hub",
                            "state": "UP",
                            "status": "UP",
                            "ip_address": None,
                            "device_type": "SUBNET_HUB",
                            "endpoint_id": None,
                            "subnet": subnet_str,
                        }
                    edges.add((root_node_id, hub_id))
                    if root_node_id not in transit_children:
                        transit_children[root_node_id] = set()
                    transit_children[root_node_id].add(hub_id)
                    if hub_id not in transit_children:
                        transit_children[hub_id] = set()

                    for d_ep_id in ep_ids:
                        edges.discard((root_node_id, d_ep_id))
                        transit_children[root_node_id].discard(d_ep_id)
                        edges.add((hub_id, d_ep_id))
                        transit_children[hub_id].add(d_ep_id)

            # 5. RCA Status Propagation - Inferred Down
            self._propagate_inferred_down(nodes, transit_children)

            # 6. Compute DAG Topological Longest-Path Levels
            adj: Dict[str, List[str]] = {nid: [] for nid in nodes}
            in_degrees: Dict[str, int] = {nid: 0 for nid in nodes}
            for src, tgt in edges:
                if src in adj and tgt in in_degrees:
                    adj[src].append(tgt)
                    in_degrees[tgt] += 1

            levels: Dict[str, int] = {}
            from collections import deque
            queue = deque([nid for nid, deg in in_degrees.items() if deg == 0 or nid == root_node_id])
            for nid in queue:
                levels[nid] = 0

            while queue:
                u = queue.popleft()
                u_level = levels.get(u, 0)
                for v in adj.get(u, []):
                    cand_level = u_level + 1
                    if v not in levels or cand_level > levels[v]:
                        levels[v] = cand_level
                    in_degrees[v] -= 1
                    if in_degrees[v] <= 0:
                        queue.append(v)

            for nid, node_data in nodes.items():
                node_data["level"] = levels.get(nid, 1 if nid != root_node_id else 0)

            self._nodes = nodes
            self._edges = edges
            self._transit_children = transit_children
            self._monitored_by_id = monitored_by_id
            self._monitored_by_ip = monitored_by_ip
            self._disabled_topology_ep_ids = disabled_topology_ep_ids
            self._baseline_routes = baseline_routes
            self._failed_hop_ips = failed_hop_ips
            self._failed_endpoint_ids = failed_endpoint_ids

            self._cached_graph = {
                "nodes": list(nodes.values()),
                "edges": [{"source": src, "target": tgt} for src, tgt in edges],
            }
            self._cached_graph_json = json.dumps(self._cached_graph)
            self._initialized = True

            logger.info("TopologyGraphManager full rebuild completed: %d nodes, %d edges.", len(nodes), len(edges))
            return self._cached_graph

    def _propagate_inferred_down(self, nodes: Dict[str, Dict[str, Any]], transit_children: Dict[str, Set[str]]) -> None:
        def get_all_downstream_monitored(start_node: str, visited: Set[str]) -> Set[str]:
            monitored: Set[str] = set()
            visited.add(start_node)
            for child in transit_children.get(start_node, set()):
                if child in visited:
                    continue
                if nodes.get(child, {}).get("type") == "monitored":
                    monitored.add(child)
                else:
                    monitored.update(get_all_downstream_monitored(child, visited))
            return monitored

        for node_id, node_info in list(nodes.items()):
            node_type = node_info.get("type") or node_info.get("node_type")
            device_type = node_info.get("device_type")
            # Only propagate inferred down for real physical transit routers with valid IP addresses
            if node_type == "transit" and device_type == "TRANSIT_ROUTER" and node_info.get("state") != "FAILURE_POINT":
                downstream = get_all_downstream_monitored(node_id, set())
                if downstream:
                    all_down = all(
                        nodes[c_id].get("state") in ("DOWN", "DOWN-UNSTABLE", "FAILURE_POINT") or nodes[c_id].get("status") in ("DOWN", "DOWN-UNSTABLE", "FAILURE_POINT")
                        for c_id in downstream
                        if c_id in nodes
                    )
                    if all_down:
                        node_info["state"] = "INFERRED_DOWN"
                        node_info["status"] = "INFERRED_DOWN"

    async def update_node_status(self, node_id: str, new_state: str) -> None:
        """
        Incremental Event Mutation Hook:
        Updates target node's state in memory without re-parsing graph edges or querying DB.
        """
        if node_id in self._disabled_topology_ep_ids:
            return

        async with self._lock:
            if node_id in self._nodes:
                self._nodes[node_id]["state"] = new_state
                self._nodes[node_id]["status"] = new_state
                self._propagate_inferred_down(self._nodes, self._transit_children)
                self._cached_graph = {
                    "nodes": list(self._nodes.values()),
                    "edges": [{"source": src, "target": tgt} for src, tgt in self._edges],
                }
                self._cached_graph_json = json.dumps(self._cached_graph)

    async def update_endpoint_path(self, endpoint_id: UUID, new_hops: List[dict]) -> None:
        """
        Incremental Event Mutation Hook:
        Recalculates visual edges affected by a single refreshed baseline route,
        prunes stale parent edges and orphaned ghost transit nodes/edges,
        recomputes localized topological levels, and broadcasts an SSE event.
        """
        ep_id = str(endpoint_id)
        if ep_id in self._disabled_topology_ep_ids:
            return

        from app.services.diagnostics import sanitize_traceroute_hops
        clean_hops = sanitize_traceroute_hops(new_hops)

        async with self._lock:
            self._baseline_routes[ep_id] = clean_hops

            # 1. Prune stale incoming edges to this endpoint so it doesn't retain multiple parents
            stale_incoming = [(src, tgt) for (src, tgt) in self._edges if tgt == ep_id]
            for src, tgt in stale_incoming:
                self._edges.discard((src, tgt))
                if src in self._transit_children:
                    self._transit_children[src].discard(tgt)

            # 2. Re-stitch path for this endpoint
            previous_node_id = "root"
            previous_hop_ip_tag = "root"

            for idx, hop in enumerate(clean_hops):
                hop_ip = hop.get("ip")

                if hop_ip is None:
                    current_node_id = f"anon_{previous_hop_ip_tag}_to_{ep_id[:8]}"
                    if current_node_id not in self._nodes:
                        self._nodes[current_node_id] = {
                            "id": current_node_id,
                            "label": "* * *",
                            "type": "transit",
                            "node_type": "transit",
                            "state": "UP",
                            "status": "UP",
                            "ip_address": None,
                            "device_type": "ANONYMOUS_HOP",
                            "endpoint_id": None,
                        }
                else:
                    hop_ep_id = self._monitored_by_ip.get(hop_ip)
                    if hop_ep_id:
                        current_node_id = hop_ep_id
                    else:
                        current_node_id = f"transit:{hop_ip}"
                        if current_node_id not in self._nodes:
                            self._nodes[current_node_id] = {
                                "id": current_node_id,
                                "label": f"Transit ({hop_ip})",
                                "type": "transit",
                                "node_type": "transit",
                                "state": "UP",
                                "status": "UP",
                                "ip_address": hop_ip,
                                "device_type": "TRANSIT_ROUTER",
                                "endpoint_id": None,
                                "subnet": get_subnet_group(hop_ip),
                            }
                    previous_hop_ip_tag = hop_ip.replace(".", "_")

                if previous_node_id != current_node_id:
                    self._edges.add((previous_node_id, current_node_id))
                    if previous_node_id not in self._transit_children:
                        self._transit_children[previous_node_id] = set()
                    self._transit_children[previous_node_id].add(current_node_id)

                previous_node_id = current_node_id

            if previous_node_id != ep_id and ep_id in self._nodes:
                self._edges.add((previous_node_id, ep_id))
                if previous_node_id not in self._transit_children:
                    self._transit_children[previous_node_id] = set()
                self._transit_children[previous_node_id].add(ep_id)

            # 3. Ghost Transit Node & Edge Pruning Pass:
            active_transit_nodes: Set[str] = set()
            for active_id, a_hops in self._baseline_routes.items():
                prev_tag = "root"
                for h in sanitize_traceroute_hops(a_hops):
                    h_ip = h.get("ip")
                    if h_ip is None:
                        active_transit_nodes.add(f"anon_{prev_tag}_to_{active_id[:8]}")
                    else:
                        h_ep = self._monitored_by_ip.get(h_ip)
                        if not h_ep:
                            active_transit_nodes.add(f"transit:{h_ip}")
                        prev_tag = h_ip.replace(".", "_")

            orphaned_ids = [
                n_id for n_id, n_info in self._nodes.items()
                if n_info.get("type") == "transit" and n_id not in active_transit_nodes
            ]

            for orphan_id in orphaned_ids:
                del self._nodes[orphan_id]

            self._edges = {
                (src, tgt) for (src, tgt) in self._edges
                if src in self._nodes and tgt in self._nodes
            }

            self._transit_children = {}
            for src, tgt in self._edges:
                if src not in self._transit_children:
                    self._transit_children[src] = set()
                self._transit_children[src].add(tgt)

            self._propagate_inferred_down(self._nodes, self._transit_children)

            # 4. Compute DAG Topological Longest-Path Levels
            adj: Dict[str, List[str]] = {nid: [] for nid in self._nodes}
            in_degrees: Dict[str, int] = {nid: 0 for nid in self._nodes}
            for src, tgt in self._edges:
                if src in adj and tgt in in_degrees:
                    adj[src].append(tgt)
                    in_degrees[tgt] += 1

            levels: Dict[str, int] = {}
            queue = deque([nid for nid, deg in in_degrees.items() if deg == 0 or nid == "root"])
            for nid in queue:
                levels[nid] = 0

            while queue:
                u = queue.popleft()
                u_level = levels.get(u, 0)
                for v in adj.get(u, []):
                    cand_level = u_level + 1
                    if v not in levels or cand_level > levels[v]:
                        levels[v] = cand_level
                    in_degrees[v] -= 1
                    if in_degrees[v] <= 0:
                        queue.append(v)

            for nid, node_data in self._nodes.items():
                node_data["level"] = levels.get(nid, 1 if nid != "root" else 0)

            self._cached_graph = {
                "nodes": list(self._nodes.values()),
                "edges": [{"source": src, "target": tgt} for src, tgt in self._edges],
            }
            self._cached_graph_json = json.dumps(self._cached_graph)

        # 5. Broadcast real-time SSE event to connected browsers
        try:
            from app.routers.events import broadcast_sse_event
            await broadcast_sse_event(
                "TOPOLOGY_UPDATED",
                {
                    "reason": "ROUTE_DISCOVERED",
                    "endpoint_id": ep_id,
                },
            )
        except Exception as exc:
            logger.debug("Failed to broadcast TOPOLOGY_UPDATED SSE: %s", exc)


# Global singleton instance
topology_manager = TopologyGraphManager.get_instance()


async def generate_unified_topology(db: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """
    Interface for fetching the unified topology graph.
    - If db is provided (e.g. tests or explicit rebuild call), triggers full_rebuild(db).
    - If db is None (e.g. O(1) read endpoint), returns cached graph directly from RAM.
    """
    if db is not None:
        return await topology_manager.full_rebuild(db)
    return topology_manager.get_cached_graph()


async def get_topology_graph(db: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """Alias for backwards compatibility."""
    return await generate_unified_topology(db)
