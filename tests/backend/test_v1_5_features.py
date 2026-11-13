from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.services.baseline_route import is_local_subnet, refresh_baseline_route
from app.services.rca_engine import handle_endpoint_recovery, run_differential_rca
from app.services.topology import generate_unified_topology


class TestV15BackendCore(unittest.TestCase):

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_is_local_subnet_detection(self) -> None:
        # Loopback 127.0.0.1 must be identified as local
        self.assertTrue(is_local_subnet("127.0.0.1"))
        # Invalid IP string handling
        self.assertFalse(is_local_subnet("invalid_ip"))

    @patch("app.services.baseline_route.run_throttled_traceroute")
    def test_refresh_baseline_route_l2_detection(self, mock_trace: MagicMock) -> None:
        mock_trace.return_value = {
            "target_ip": "192.168.1.50",
            "hops": [{"hop": 1, "ip": "192.168.1.50", "rtt_ms": 0.5}],
        }
        endpoint_id = uuid4()
        mock_db = AsyncMock()
        # No stored path yet (route_paths.record_trace reads it first): the trace becomes the
        # current and normal path, then the Layer 2 flag is written.
        no_row = MagicMock()
        no_row.fetchone.return_value = None
        mock_db.execute.return_value = no_row

        res = self.loop.run_until_complete(
            refresh_baseline_route(endpoint_id, "192.168.1.50", db=mock_db)
        )

        self.assertEqual(res["total_hops"], 1)
        self.assertTrue(res["is_l2_segment"])
        self.assertEqual(len(res["hops"]), 1)
        self.assertEqual(res["path_outcome"], "first")
        # Read the stored path, insert the first one, update the Layer 2 flag.
        self.assertEqual(mock_db.execute.call_count, 3)

    @patch("app.services.rca_engine.run_throttled_traceroute")
    def test_differential_rca_layer2_failure(self, mock_trace: MagicMock) -> None:
        endpoint_id = uuid4()
        mock_db = AsyncMock()

        # Mock DB returns endpoint as L2 segment
        ep_res = MagicMock()
        ep_res.fetchone.return_value = MagicMock(
            id=endpoint_id,
            ip_address="192.168.1.100",
            enable_rca=True,
            is_l2_segment=True,
        )

        bl_res = MagicMock()
        bl_res.fetchone.return_value = None

        inc_res = MagicMock()
        inc_res.fetchone.return_value = MagicMock(id=uuid4())

        sym_res = MagicMock()
        sym_res.fetchall.return_value = []

        mock_db.execute.side_effect = [ep_res, bl_res, inc_res, sym_res]

        mock_trace.return_value = {
            "target_ip": "192.168.1.100",
            "hops": [{"hop": 1, "ip": "192.168.1.100", "rtt_ms": None}],
        }

        res = self.loop.run_until_complete(run_differential_rca(endpoint_id, db=mock_db))

        self.assertIsNotNone(res)
        self.assertEqual(res["failed_hop_ip"], "192.168.1.100")
        self.assertIn("Direct Layer 2 Attachment", res["rca_summary"])

    @patch("app.services.rca_engine.run_throttled_traceroute")
    def test_differential_rca_layer3_transit_failure(self, mock_trace: MagicMock) -> None:
        endpoint_id = uuid4()
        mock_db = AsyncMock()

        # Endpoint is L3 multi-hop
        ep_res = MagicMock()
        ep_res.fetchone.return_value = MagicMock(
            id=endpoint_id,
            ip_address="10.0.0.50",
            enable_rca=True,
            is_l2_segment=False,
        )

        # Baseline route has 3 hops: 192.168.1.1 -> 10.254.0.1 -> 10.0.0.50
        baseline_hops = [
            {"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.0},
            {"hop": 2, "ip": "10.254.0.1", "rtt_ms": 10.0},
            {"hop": 3, "ip": "10.0.0.50", "rtt_ms": 25.0},
        ]
        bl_res = MagicMock()
        bl_res.fetchone.return_value = MagicMock(
            total_hops=3,
            hops=baseline_hops,
        )

        inc_res = MagicMock()
        inc_res.fetchone.return_value = MagicMock(id=uuid4())

        sym_res = MagicMock()
        sym_res.fetchall.return_value = []

        mock_db.execute.side_effect = [ep_res, bl_res, inc_res, sym_res]

        # Live failure traceroute times out at Hop 2 (10.254.0.1 is None/*)
        mock_trace.return_value = {
            "target_ip": "10.0.0.50",
            "hops": [
                {"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.1},
                {"hop": 2, "ip": None, "rtt_ms": None},
                {"hop": 3, "ip": None, "rtt_ms": None},
            ],
        }

        res = self.loop.run_until_complete(run_differential_rca(endpoint_id, db=mock_db))

        self.assertIsNotNone(res)
        self.assertEqual(res["failed_hop_number"], 2)
        self.assertEqual(res["failed_hop_ip"], "10.254.0.1")
        self.assertEqual(res["last_known_good_hop_ip"], "192.168.1.1")
        self.assertIn("Transit failure at Hop 2 (10.254.0.1)", res["rca_summary"])

    @patch("app.services.rca_engine.refresh_baseline_route")
    def test_handle_endpoint_recovery(self, mock_refresh: AsyncMock) -> None:
        endpoint_id = uuid4()
        mock_db = AsyncMock()
        update_res = MagicMock()
        update_res.rowcount = 1
        mock_db.execute.return_value = update_res

        self.loop.run_until_complete(
            handle_endpoint_recovery(endpoint_id, "10.0.0.50", db=mock_db)
        )

        self.assertEqual(mock_db.execute.call_count, 1)
        mock_refresh.assert_called_once_with(endpoint_id, "10.0.0.50", db=mock_db, trigger="recovery")

    def test_generate_unified_topology_tree_merging_and_rca(self) -> None:
        ep1_id = uuid4()
        ep2_id = uuid4()

        ep_rows = [
            MagicMock(
                id=ep1_id,
                hostname="server-1",
                ip_address="10.0.0.1",
                device_type="SERVER",
                location="DC1",
                endpoint_status="ACTIVE",
                allow_topology_discovery=True,
                manual_parent_id=None,
                is_l2_segment=False,
                operational_state="DOWN",
                detailed_state="DOWN",
            ),
            MagicMock(
                id=ep2_id,
                hostname="server-2",
                ip_address="10.0.0.2",
                device_type="SERVER",
                location="DC1",
                endpoint_status="ACTIVE",
                allow_topology_discovery=True,
                manual_parent_id=None,
                is_l2_segment=False,
                operational_state="DOWN",
                detailed_state="DOWN",
            ),
        ]

        bl_rows = [
            MagicMock(
                endpoint_id=ep1_id,
                total_hops=3,
                hops=[
                    {"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.0},
                    {"hop": 2, "ip": None, "rtt_ms": None},  # Anonymous hop
                    {"hop": 3, "ip": "10.0.0.1", "rtt_ms": 20.0},
                ],
            ),
            MagicMock(
                endpoint_id=ep2_id,
                total_hops=3,
                hops=[
                    {"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.0},
                    {"hop": 2, "ip": "10.254.0.1", "rtt_ms": 10.0},  # Failure point
                    {"hop": 3, "ip": "10.0.0.2", "rtt_ms": 25.0},
                ],
            ),
        ]

        inc_rows = [
            MagicMock(
                endpoint_id=ep2_id,
                failed_hop_ip="10.254.0.1",
                rca_summary="Transit failure at Hop 2",
            )
        ]

        mock_db = AsyncMock()

        ep_res = MagicMock()
        ep_res.fetchall.return_value = ep_rows

        bl_res = MagicMock()
        bl_res.fetchall.return_value = bl_rows

        tr_res = MagicMock()
        tr_res.fetchall.return_value = []

        inc_res = MagicMock()
        inc_res.fetchall.return_value = inc_rows

        mock_db.execute.side_effect = [ep_res, bl_res, tr_res, inc_res]

        graph = self.loop.run_until_complete(generate_unified_topology(mock_db))

        nodes_by_id = {n["id"]: n for n in graph["nodes"]}
        self.assertIn("root", nodes_by_id)
        self.assertIn("transit:192.168.1.1", nodes_by_id)
        self.assertIn("transit:10.254.0.1", nodes_by_id)
        self.assertIn(str(ep1_id), nodes_by_id)
        self.assertIn(str(ep2_id), nodes_by_id)

        # 10.254.0.1 matches active failed_hop_ip -> state must be FAILURE_POINT
        self.assertEqual(nodes_by_id["transit:10.254.0.1"]["state"], "FAILURE_POINT")

        # Check edges
        edges = {(e["source"], e["target"]) for e in graph["edges"]}
        self.assertIn(("root", "transit:192.168.1.1"), edges)
        self.assertIn(("transit:192.168.1.1", "transit:10.254.0.1"), edges)
        self.assertIn(("transit:10.254.0.1", str(ep2_id)), edges)

    def test_topology_graph_manager_singleton_and_mutation_hooks(self) -> None:
        from app.services.topology import TopologyGraphManager
        manager = TopologyGraphManager.get_instance()
        self.assertIs(manager, TopologyGraphManager.get_instance())

        # Test incremental node status update
        manager._nodes["test-node-1"] = {"id": "test-node-1", "state": "UP", "status": "UP", "type": "monitored"}
        self.loop.run_until_complete(manager.update_node_status("test-node-1", "DOWN"))
        cached_graph = manager.get_cached_graph()
        test_node = next((n for n in cached_graph["nodes"] if n["id"] == "test-node-1"), None)
        self.assertIsNotNone(test_node)
        self.assertEqual(test_node["state"], "DOWN")

        # Test incremental endpoint path update
        ep_id = uuid4()
        self.loop.run_until_complete(manager.update_endpoint_path(ep_id, [{"hop": 1, "ip": "172.16.0.1"}, {"hop": 2, "ip": "172.16.0.2"}]))
        cached_graph_updated = manager.get_cached_graph()
        nodes_map = {n["id"]: n for n in cached_graph_updated["nodes"]}
        self.assertIn("transit:172.16.0.1", nodes_map)
        self.assertIn("transit:172.16.0.2", nodes_map)

    def test_fhrp_mac_regex_patterns(self) -> None:
        from app.services.baseline_route import get_fhrp_type
        self.assertEqual(get_fhrp_type("00:00:0c:07:ac:01"), "HSRP_V1")
        self.assertEqual(get_fhrp_type("00:00:0c:9f:f1:02"), "HSRP_V2")
        self.assertEqual(get_fhrp_type("00:00:5e:00:01:0a"), "VRRP_IPV4")
        self.assertEqual(get_fhrp_type("00:00:5e:00:02:0b"), "VRRP_IPV6")
        self.assertEqual(get_fhrp_type("00:07:b4:01:02:03"), "GLBP")
        self.assertIsNone(get_fhrp_type("00:11:22:33:44:55"))

    def test_4_tier_boundary_classifier(self) -> None:
        from app.services.baseline_route import classify_boundary_tier
        tier, is_l2, default_gw, mac_addr, fhrp_type = classify_boundary_tier("127.0.0.1")
        self.assertEqual(tier, "L2_LOCAL_HOST")
        self.assertTrue(is_l2)

        tier_wan, is_l2_wan, _, _, _ = classify_boundary_tier("8.8.8.8")
        self.assertEqual(tier_wan, "L3_ROUTED_TRANSIT")
        self.assertFalse(is_l2_wan)

    def test_subnet_group_calculation(self) -> None:
        from app.services.topology import get_subnet_group
        self.assertEqual(get_subnet_group("192.168.1.50"), "192.168.1.0/24")
        self.assertEqual(get_subnet_group("10.0.5.12"), "10.0.5.0/24")
        self.assertIsNone(get_subnet_group(None))

    def test_sanitize_traceroute_hops_collapsing(self) -> None:
        from app.services.diagnostics import sanitize_traceroute_hops

        raw_hops = [
            {"hop": 1, "ip": "192.168.1.1", "rtt_ms": 1.0},
            {"hop": 2, "ip": None, "rtt_ms": None},
            {"hop": 3, "ip": None, "rtt_ms": None},
            {"hop": 4, "ip": "10.0.0.5", "rtt_ms": 5.0},
            {"hop": 5, "ip": None, "rtt_ms": None},
            {"hop": 6, "ip": None, "rtt_ms": None},
        ]

        cleaned = sanitize_traceroute_hops(raw_hops)
        # Trailing nulls (hop 5 & 6) must be stripped.
        # Consecutive nulls (hop 2 & 3) must be collapsed to 1 null.
        self.assertEqual(len(cleaned), 3)
        self.assertEqual(cleaned[0]["ip"], "192.168.1.1")
        self.assertIsNone(cleaned[1]["ip"])
        self.assertEqual(cleaned[2]["ip"], "10.0.0.5")

    def test_ghost_node_pruning_on_update_endpoint_path(self) -> None:
        from app.services.topology import TopologyGraphManager
        manager = TopologyGraphManager.get_instance()
        ep_id = uuid4()
        ep_str = str(ep_id)

        # Setup initial state with a stale transit node
        manager._nodes = {
            "root": {"id": "root", "type": "root"},
            ep_str: {"id": ep_str, "type": "monitored"},
            "transit:10.0.0.99": {"id": "transit:10.0.0.99", "type": "transit", "ip_address": "10.0.0.99"},
        }
        manager._monitored_by_id = {ep_str: {"id": ep_str}}
        manager._monitored_by_ip = {"192.168.1.100": ep_str}

        # Update path with new route (192.168.1.1)
        self.loop.run_until_complete(manager.update_endpoint_path(ep_id, [{"hop": 1, "ip": "192.168.1.1"}]))

        cached_graph = manager.get_cached_graph()
        node_ids = {n["id"] for n in cached_graph["nodes"]}

        # Old transit 10.0.0.99 must be pruned as a ghost node
        self.assertNotIn("transit:10.0.0.99", node_ids)
        self.assertIn("transit:192.168.1.1", node_ids)


if __name__ == "__main__":
    unittest.main()
