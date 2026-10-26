import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from monitoring.flow.correlator import FlowCorrelator
from monitoring.flow.aggregator import normalize_service_port


class TestFlowCorrelation(unittest.IsolatedAsyncioTestCase):
    async def test_normalize_service_port(self):
        # Client contacting well-known server port 443
        self.assertEqual(normalize_service_port(src_port=58912, dst_port=443), 443)
        # Server responding from port 80 to high client port
        self.assertEqual(normalize_service_port(src_port=80, dst_port=58912), 80)
        # Known DB port 5432 responding to client
        self.assertEqual(normalize_service_port(src_port=5432, dst_port=41234), 5432)
        # Web alt 8080 responding to client
        self.assertEqual(normalize_service_port(src_port=8080, dst_port=41234), 8080)
        # Both high ephemeral ports -> fallback to dst_port
        self.assertEqual(normalize_service_port(src_port=50000, dst_port=60000), 60000)

    async def test_correlator_primary_and_alias_lookup(self):
        correlator = FlowCorrelator(db_session_factory=None, redis_client=None)

        gw_id = uuid4()
        server_id = uuid4()
        client_id = uuid4()

        # Seed maps directly to test in-memory lookup logic
        correlator._exporter_map["192.168.1.1"] = gw_id      # Primary IP
        correlator._exporter_map["10.255.255.1"] = gw_id    # Alias in flow_exporter_ips

        correlator._participant_map["192.168.1.100"] = server_id
        correlator._participant_map["192.168.1.200"] = client_id

        # Case 1: Packet from primary exporter IP
        exp_id, src_id, dst_id = correlator.correlate(
            exporter_ip="192.168.1.1",
            src_ip="192.168.1.200",
            dst_ip="192.168.1.100",
        )
        self.assertEqual(exp_id, gw_id)
        self.assertEqual(src_id, client_id)
        self.assertEqual(dst_id, server_id)

        # Case 2: Packet from loopback / alias exporter IP
        exp_id2, src_id2, dst_id2 = correlator.correlate(
            exporter_ip="10.255.255.1",
            src_ip="192.168.1.100",
            dst_ip="8.8.8.8",
        )
        self.assertEqual(exp_id2, gw_id)
        self.assertEqual(src_id2, server_id)
        self.assertIsNone(dst_id2)  # External unmonitored IP

    async def test_correlator_unmatched_tracking(self):
        mock_redis = AsyncMock()
        correlator = FlowCorrelator(db_session_factory=None, redis_client=mock_redis)

        # Correlate from unknown exporter IP
        unknown_ip = "172.31.254.1"
        exp_id, src_id, dst_id = correlator.correlate(
            exporter_ip=unknown_ip,
            src_ip="10.0.0.1",
            dst_ip="10.0.0.2",
        )
        self.assertIsNone(exp_id)
        self.assertIn(unknown_ip, correlator._unmatched_local)

        # Yield control to let background record_unmatched task run
        await asyncio.sleep(0.01)

        # Verify Redis key set with 900s TTL
        mock_redis.set.assert_awaited()
        call_args = mock_redis.set.await_args
        self.assertEqual(call_args.args[0], f"flow:unmatched:{unknown_ip}")
        self.assertEqual(call_args.kwargs.get("ex"), 900)

    async def test_correlator_unmatched_flow_rate(self):
        """The second write for an unmatched exporter carries its flows/s since the first."""
        from unittest.mock import patch

        mock_redis = AsyncMock()
        correlator = FlowCorrelator(db_session_factory=None, redis_client=mock_redis)
        ip = "172.31.254.9"

        with patch("monitoring.flow.correlator.time.time", return_value=1000.0):
            correlator.correlate(exporter_ip=ip, src_ip="10.0.0.1", dst_ip="10.0.0.2")
        await asyncio.sleep(0.01)
        fps_writes = [c for c in mock_redis.set.await_args_list if c.args[0].endswith(":fps")]
        self.assertEqual(fps_writes, [])  # first sighting: no rate yet

        # 59 more flows inside the rate-limit window, then one after it (60 total in 20 s).
        with patch("monitoring.flow.correlator.time.time", return_value=1005.0):
            for _ in range(59):
                correlator.correlate(exporter_ip=ip, src_ip="10.0.0.1", dst_ip="10.0.0.2")
        with patch("monitoring.flow.correlator.time.time", return_value=1020.0):
            correlator.correlate(exporter_ip=ip, src_ip="10.0.0.1", dst_ip="10.0.0.2")
        await asyncio.sleep(0.01)

        fps_writes = [c for c in mock_redis.set.await_args_list if c.args[0].endswith(":fps")]
        self.assertEqual(len(fps_writes), 1)
        self.assertEqual(fps_writes[0].args, (f"flow:unmatched:{ip}:fps", "3.0"))
        self.assertEqual(fps_writes[0].kwargs.get("ex"), 900)

    async def test_correlator_sync_from_db(self):
        gw_id = uuid4()
        srv_id = uuid4()

        # Mock DB rows
        mock_row1 = MagicMock()
        mock_row1.id = gw_id
        mock_row1.ip_address = "192.168.1.1"
        mock_row1.flow_exporter_ips = ["10.255.255.1", "172.16.1.1"]

        mock_row2 = MagicMock()
        mock_row2.id = srv_id
        mock_row2.ip_address = "192.168.1.50"
        mock_row2.flow_exporter_ips = None

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_row1, mock_row2]

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result

        class MockDbFactory:
            def __call__(self):
                return self
            async def __aenter__(self):
                return mock_session
            async def __aexit__(self, *args):
                pass

        correlator = FlowCorrelator(db_session_factory=MockDbFactory(), redis_client=None)
        await correlator.refresh_from_db()

        # Check that exporter map has primary and aliases
        self.assertEqual(correlator._exporter_map["192.168.1.1"], gw_id)
        self.assertEqual(correlator._exporter_map["10.255.255.1"], gw_id)
        self.assertEqual(correlator._exporter_map["172.16.1.1"], gw_id)
        self.assertEqual(correlator._exporter_map["192.168.1.50"], srv_id)

        # Check participant map
        self.assertEqual(correlator._participant_map["192.168.1.1"], gw_id)
        self.assertEqual(correlator._participant_map["192.168.1.50"], srv_id)


if __name__ == "__main__":
    unittest.main()
