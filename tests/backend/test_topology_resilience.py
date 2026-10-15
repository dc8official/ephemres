from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from app.services.topology import TopologyGraphManager, topology_manager


def test_synthetic_subnet_clustering_direct_eps():
    """
    Verifies that when >= 2 endpoints reside on the same subnet and connect directly
    to Root with no intermediate hops, Synthetic Subnet Clustering groups them under
    a 'subnet_hub' node at Level 1, placing endpoints at Level 2, breaking the flat line.
    """
    async def _test():
        ep1_id = uuid4()
        ep2_id = uuid4()
        ep3_id = uuid4()

        ep_rows = [
            MagicMock(
                id=ep1_id,
                hostname="switch-core-1",
                ip_address="192.168.10.10",
                device_type="SWITCH",
                location="HQ",
                endpoint_status="ACTIVE",
                allow_topology_discovery=True,
                manual_parent_id=None,
                is_l2_segment=True,
                operational_state="UP",
                detailed_state="UP",
            ),
            MagicMock(
                id=ep2_id,
                hostname="switch-core-2",
                ip_address="192.168.10.11",
                device_type="SWITCH",
                location="HQ",
                endpoint_status="ACTIVE",
                allow_topology_discovery=True,
                manual_parent_id=None,
                is_l2_segment=True,
                operational_state="UP",
                detailed_state="UP",
            ),
            MagicMock(
                id=ep3_id,
                hostname="nas-storage",
                ip_address="192.168.10.20",
                device_type="SERVER",
                location="HQ",
                endpoint_status="ACTIVE",
                allow_topology_discovery=True,
                manual_parent_id=None,
                is_l2_segment=True,
                operational_state="UP",
                detailed_state="UP",
            ),
        ]

        # All 3 endpoints have 1-hop routes directly to themselves (direct local connection)
        bl_rows = [
            MagicMock(
                endpoint_id=ep1_id,
                total_hops=1,
                hops=[{"hop": 1, "ip": "192.168.10.10", "rtt_ms": 0.5}],
            ),
            MagicMock(
                endpoint_id=ep2_id,
                total_hops=1,
                hops=[{"hop": 1, "ip": "192.168.10.11", "rtt_ms": 0.6}],
            ),
            MagicMock(
                endpoint_id=ep3_id,
                total_hops=1,
                hops=[{"hop": 1, "ip": "192.168.10.20", "rtt_ms": 0.4}],
            ),
        ]

        mock_db = AsyncMock()
        ep_res = MagicMock()
        ep_res.fetchall.return_value = ep_rows
        bl_res = MagicMock()
        bl_res.fetchall.return_value = bl_rows
        tr_res = MagicMock()
        tr_res.fetchall.return_value = []
        inc_res = MagicMock()
        inc_res.fetchall.return_value = []

        mock_db.execute.side_effect = [ep_res, bl_res, tr_res, inc_res]

        mgr = TopologyGraphManager.get_instance()
        graph = await mgr.full_rebuild(mock_db)

        nodes_by_id = {n["id"]: n for n in graph["nodes"]}
        edges = {(e["source"], e["target"]) for e in graph["edges"]}

        # Hub ID format: subnet:192_168_10_0_24
        expected_hub_id = "subnet:192_168_10_0_24"
        assert expected_hub_id in nodes_by_id
        hub_node = nodes_by_id[expected_hub_id]
        assert hub_node["type"] == "subnet_hub"
        assert hub_node["node_type"] == "subnet_hub"
        assert hub_node["level"] == 1

        # Check edge structure: root -> subnet_hub -> endpoints
        assert ("root", expected_hub_id) in edges
        assert (expected_hub_id, str(ep1_id)) in edges
        assert (expected_hub_id, str(ep2_id)) in edges
        assert (expected_hub_id, str(ep3_id)) in edges

        # Ensure direct root edges were pruned
        assert ("root", str(ep1_id)) not in edges
        assert ("root", str(ep2_id)) not in edges
        assert ("root", str(ep3_id)) not in edges

        # Check endpoint levels: they must be at level 2
        assert nodes_by_id[str(ep1_id)]["level"] == 2
        assert nodes_by_id[str(ep2_id)]["level"] == 2
        assert nodes_by_id[str(ep3_id)]["level"] == 2

    asyncio.run(_test())


def test_update_endpoint_path_stale_edge_pruning_and_leveling():
    """
    Verifies that update_endpoint_path:
    1. Prunes stale incoming edges so an endpoint never retains multiple parents.
    2. Recalculates DAG longest-path levels accurately in RAM.
    3. Prunes orphaned ghost transit nodes.
    """
    async def _test():
        ep_id = uuid4()
        mgr = TopologyGraphManager.get_instance()

        # Seed initial state with endpoint connected directly to root
        async with mgr._lock:
            mgr._nodes = {
                "root": {"id": "root", "label": "Ephemres Engine", "type": "root", "level": 0},
                str(ep_id): {
                    "id": str(ep_id),
                    "label": "server-1",
                    "type": "monitored",
                    "ip_address": "10.50.1.100",
                    "level": 1,
                },
            }
            mgr._edges = {("root", str(ep_id))}
            mgr._transit_children = {"root": {str(ep_id)}}
            mgr._monitored_by_id = {str(ep_id): {"ip_address": "10.50.1.100"}}
            mgr._monitored_by_ip = {"10.50.1.100": str(ep_id)}
            mgr._disabled_topology_ep_ids = set()
            mgr._baseline_routes = {str(ep_id): []}

        # Now discover a 2-hop route: root -> transit:10.50.0.1 -> ep
        new_hops = [
            {"hop": 1, "ip": "10.50.0.1", "rtt_ms": 1.2},
            {"hop": 2, "ip": "10.50.1.100", "rtt_ms": 2.5},
        ]

        with patch("app.routers.events.broadcast_sse_event", new_callable=AsyncMock) as mock_broadcast:
            await mgr.update_endpoint_path(ep_id, new_hops)
            mock_broadcast.assert_awaited_once_with(
                "TOPOLOGY_UPDATED",
                {"reason": "ROUTE_DISCOVERED", "endpoint_id": str(ep_id)},
            )

        cached = mgr.get_cached_graph()
        edges = {(e["source"], e["target"]) for e in cached["edges"]}
        nodes_by_id = {n["id"]: n for n in cached["nodes"]}

        # Stale incoming edge ("root", ep_id) MUST be discarded
        assert ("root", str(ep_id)) not in edges
        # New hierarchy must be established
        assert ("root", "transit:10.50.0.1") in edges
        assert ("transit:10.50.0.1", str(ep_id)) in edges

        # Levels must be: root: 0, transit: 1, ep: 2
        assert nodes_by_id["root"]["level"] == 0
        assert nodes_by_id["transit:10.50.0.1"]["level"] == 1
        assert nodes_by_id[str(ep_id)]["level"] == 2

        # Now route changes to a 3-hop path: root -> transit:172.16.1.1 -> transit:172.16.2.1 -> ep
        newer_hops = [
            {"hop": 1, "ip": "172.16.1.1", "rtt_ms": 1.0},
            {"hop": 2, "ip": "172.16.2.1", "rtt_ms": 2.0},
            {"hop": 3, "ip": "10.50.1.100", "rtt_ms": 4.0},
        ]
        with patch("app.routers.events.broadcast_sse_event", new_callable=AsyncMock):
            await mgr.update_endpoint_path(ep_id, newer_hops)

        cached2 = mgr.get_cached_graph()
        edges2 = {(e["source"], e["target"]) for e in cached2["edges"]}
        nodes_by_id2 = {n["id"]: n for n in cached2["nodes"]}

        # Old transit:10.50.0.1 should be pruned as ghost node
        assert "transit:10.50.0.1" not in nodes_by_id2
        assert ("transit:10.50.0.1", str(ep_id)) not in edges2

        # New paths and levels:
        assert ("root", "transit:172.16.1.1") in edges2
        assert ("transit:172.16.1.1", "transit:172.16.2.1") in edges2
        assert ("transit:172.16.2.1", str(ep_id)) in edges2

        assert nodes_by_id2["transit:172.16.1.1"]["level"] == 1
        assert nodes_by_id2["transit:172.16.2.1"]["level"] == 2
        assert nodes_by_id2[str(ep_id)]["level"] == 3

    asyncio.run(_test())


def test_refresh_baseline_route_preserves_last_known_good():
    """
    Verifies that when a probe times out and returns 0 reachable hops,
    refresh_baseline_route preserves the existing valid baseline route from the database
    instead of wiping it out.
    """
    async def _test():
        from app.services.baseline_route import refresh_baseline_route

        ep_id = uuid4()
        target_ip = "198.51.100.55"

        # Mock traceroute returning only timeouts (None)
        timeout_trace = {
            "target": target_ip,
            "hops": [
                {"hop": 1, "ip": None, "rtt_ms": None},
                {"hop": 2, "ip": None, "rtt_ms": None},
            ],
            "total_hops": 2,
        }

        # Mock existing good hops in DB
        good_hops = [
            {"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.2},
            {"hop": 2, "ip": target_ip, "rtt_ms": 15.0},
        ]

        mock_db = AsyncMock()
        check_row = MagicMock(total_hops=2, hops=good_hops)
        check_res = MagicMock()
        check_res.fetchone.return_value = check_row
        mock_db.execute.return_value = check_res

        with patch("app.services.baseline_route.run_throttled_traceroute", return_value=timeout_trace):
            res = await refresh_baseline_route(ep_id, target_ip, db=mock_db)

        # Result hops should be the preserved good hops
        assert res["hops"] == good_hops
        assert res["total_hops"] == 2

    asyncio.run(_test())


def test_telemetry_relay_auto_discovery_trigger():
    """
    Verifies that when an endpoint transitions to UP and has 0 hops,
    telemetry_relay automatically enqueues route discovery and applies flapping lock.
    """
    async def _test():
        from app.services.baseline_route import discovery_in_progress, discovery_route_queue
        from app.services.telemetry_relay import telemetry_relay

        ep_id = uuid4()
        ep_str = str(ep_id)

        # Clear queue and set
        discovery_in_progress.clear()
        while not discovery_route_queue.empty():
            discovery_route_queue.get_nowait()

        # Seed topology_manager with empty baseline routes for this endpoint
        topology_manager._baseline_routes[ep_str] = []

        # 1. First recovery to UP should enqueue
        await telemetry_relay._maybe_trigger_auto_discovery(ep_str, "10.200.1.5")
        assert ep_id in discovery_in_progress
        assert discovery_route_queue.qsize() == 1
        item = await discovery_route_queue.get()
        assert item == (ep_id, "10.200.1.5")

        # 2. Link flap: while in-progress, another UP event arrives -> should NOT enqueue
        discovery_in_progress.add(ep_id)
        await telemetry_relay._maybe_trigger_auto_discovery(ep_str, "10.200.1.5")
        assert discovery_route_queue.qsize() == 0

        # 3. Once route is populated with valid hops, future UP events do NOT enqueue
        discovery_in_progress.discard(ep_id)
        topology_manager._baseline_routes[ep_str] = [{"hop": 1, "ip": "10.200.1.5", "rtt_ms": 1.0}]
        await telemetry_relay._maybe_trigger_auto_discovery(ep_str, "10.200.1.5")
        assert discovery_route_queue.qsize() == 0

    asyncio.run(_test())
