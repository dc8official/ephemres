from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.models.user_session import UserSession
from app.services.auth_service import (
    get_trusted_client_ip,
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from app.services.diagnostics import (
    _parse_trace_output,
    is_local_subnet_destination,
    run_traceroute,
    trace_semaphore,
)
from app.services.driver_manager import StorageDriverManager, driver_manager
from app.services.event_broker import PostgresEventBroker, RedisEventBroker
from app.services.session_store import PostgresSessionStore, RedisSessionStore
from monitoring.ping import PingResult, classify_ping_result
from monitoring.registry import EndpointRegistry, MonitoredEndpoint


class TestMilestone2TimingBudget(unittest.TestCase):
    """Test 5-ping ratio thresholds and timing budget classification."""

    def test_classify_5_of_5_healthy_up(self) -> None:
        result = PingResult(success_count=5, failed_count=0, avg_rtt_ms=12.5)
        op_state, det_state = classify_ping_result(
            result, baseline_mean=10.0, baseline_stddev=2.0
        )
        self.assertEqual(op_state, "UP")
        self.assertEqual(det_state, "UP")

    def test_classify_5_of_5_degraded_rtt_up_unstable(self) -> None:
        # Baseline = 10.0, stddev = 2.0 -> threshold = 10.0 + (3 * 2.0) = 16.0
        result = PingResult(success_count=5, failed_count=0, avg_rtt_ms=22.0)
        op_state, det_state = classify_ping_result(
            result, baseline_mean=10.0, baseline_stddev=2.0
        )
        self.assertEqual(op_state, "UP")
        self.assertEqual(det_state, "UP-UNSTABLE")

    def test_classify_4_of_5_received_up_unstable(self) -> None:
        result = PingResult(success_count=4, failed_count=1, avg_rtt_ms=10.0)
        op_state, det_state = classify_ping_result(result)
        self.assertEqual(op_state, "UP")
        self.assertEqual(det_state, "UP-UNSTABLE")

    def test_classify_3_of_5_received_up_unstable(self) -> None:
        result = PingResult(success_count=3, failed_count=2, avg_rtt_ms=10.0)
        op_state, det_state = classify_ping_result(result)
        self.assertEqual(op_state, "UP")
        self.assertEqual(det_state, "UP-UNSTABLE")

    def test_classify_2_of_5_received_down_unstable(self) -> None:
        result = PingResult(success_count=2, failed_count=3, avg_rtt_ms=10.0)
        op_state, det_state = classify_ping_result(result)
        self.assertEqual(op_state, "DOWN")
        self.assertEqual(det_state, "DOWN-UNSTABLE")

    def test_classify_1_of_5_received_down_unstable(self) -> None:
        result = PingResult(success_count=1, failed_count=4, avg_rtt_ms=10.0)
        op_state, det_state = classify_ping_result(result)
        self.assertEqual(op_state, "DOWN")
        self.assertEqual(det_state, "DOWN-UNSTABLE")

    def test_classify_0_of_5_received_down(self) -> None:
        result = PingResult(success_count=0, failed_count=5, avg_rtt_ms=None)
        op_state, det_state = classify_ping_result(result)
        self.assertEqual(op_state, "DOWN")
        self.assertEqual(det_state, "DOWN")


class TestMilestone2DynamicRegistry(unittest.TestCase):
    """Test concurrent-safe in-memory EndpointRegistry."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.registry = EndpointRegistry()

    def tearDown(self) -> None:
        self.loop.close()

    def test_add_and_get_endpoint_toggles(self) -> None:
        ep_id = uuid4()
        ep_data = {
            "id": ep_id,
            "ip_address": "192.168.1.50",
            "hostname": "core-gw",
            "endpoint_status": "ACTIVE",
            "monitoring_enabled": True,
            "allow_incident_trace": True,
            "enable_rca": False,
        }

        async def _run():
            ep = await self.registry.add_endpoint(ep_data)
            self.assertEqual(ep.id, ep_id)
            self.assertEqual(ep.ip_address, "192.168.1.50")
            toggles = self.registry.get_toggles(ep_id)
            self.assertTrue(toggles["allow_incident_trace"])
            self.assertFalse(toggles["enable_rca"])

        self.loop.run_until_complete(_run())

    def test_update_and_remove_endpoint_lifecycle(self) -> None:
        ep_id = uuid4()
        ep_data = {
            "id": ep_id,
            "ip_address": "10.0.0.1",
            "hostname": "switch-1",
            "endpoint_status": "ACTIVE",
            "monitoring_enabled": True,
        }

        async def dummy_coro(target):
            try:
                while True:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                pass

        async def _run():
            await self.registry.add_endpoint(ep_data, spawn_coro_fn=dummy_coro)
            self.assertEqual(self.registry.endpoints_count, 1)
            self.assertEqual(self.registry.running_tasks_count, 1)

            # Update hostname and toggles
            updated = await self.registry.update_endpoint({
                "id": ep_id,
                "hostname": "switch-1-renamed",
                "allow_incident_trace": False,
            })
            self.assertEqual(updated.hostname, "switch-1-renamed")
            self.assertFalse(self.registry.get_toggles(ep_id)["allow_incident_trace"])

            # Register mock diagnostic task
            diag_task = self.loop.create_task(asyncio.sleep(10))
            self.registry.register_diagnostic_task(ep_id, diag_task)

            # Remove endpoint
            await self.registry.remove_endpoint(ep_id)
            await asyncio.sleep(0)
            self.assertEqual(self.registry.endpoints_count, 0)
            self.assertEqual(self.registry.running_tasks_count, 0)
            self.assertTrue(diag_task.cancelling() or diag_task.cancelled() or diag_task.done())

        self.loop.run_until_complete(_run())


class TestMilestone2AsyncCryptographyAndSecurity(unittest.TestCase):
    """Test non-blocking Argon2 cryptography and trusted proxy validation."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_async_hash_and_verify(self) -> None:
        async def _run():
            password = "super-secret-password-123"
            hashed = await hash_password_async(password)
            self.assertTrue(hashed.startswith("$scrypt$"))

            is_valid = await verify_password_async(password, hashed)
            self.assertTrue(is_valid)

            is_invalid = await verify_password_async("wrong-password", hashed)
            self.assertFalse(is_invalid)

        self.loop.run_until_complete(_run())

    def test_trusted_proxy_header_extraction(self) -> None:
        # Case 1: Direct peer connection is local trusted proxy (127.0.0.1)
        req_trusted = MagicMock()
        req_trusted.client.host = "127.0.0.1"
        req_trusted.headers.get.side_effect = lambda k: {
            "X-Forwarded-For": "203.0.113.195, 127.0.0.1",
        }.get(k)

        ip = get_trusted_client_ip(req_trusted)
        self.assertEqual(ip, "203.0.113.195")

        # Case 2: Untrusted direct peer connection (spoofing attempt)
        req_untrusted = MagicMock()
        req_untrusted.client.host = "198.51.100.44"  # Public untrusted client
        req_untrusted.headers.get.side_effect = lambda k: {
            "X-Forwarded-For": "1.1.1.1",  # Attempted spoof
        }.get(k)

        ip_safe = get_trusted_client_ip(req_untrusted)
        self.assertEqual(ip_safe, "198.51.100.44")


class TestMilestone2DualStorageAndEventBroker(unittest.TestCase):
    """Test Dual Driver Session Store and Event Broker implementations."""

    def setUp(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self) -> None:
        self.loop.close()

    def test_redis_session_store_lifecycle(self) -> None:
        mock_redis = AsyncMock()
        fake_storage: dict[str, str] = {}

        async def fake_get(k):
            return fake_storage.get(k)

        async def fake_set(k, v, ex=None):
            fake_storage[k] = v

        async def fake_del(k):
            fake_storage.pop(k, None)

        mock_redis.get.side_effect = fake_get
        mock_redis.set.side_effect = fake_set
        mock_redis.delete.side_effect = fake_del
        mock_redis.ttl.return_value = 7200

        store = RedisSessionStore(mock_redis)

        async def _run():
            user_id = str(uuid4())
            jti_1 = "sess_1"
            jti_2 = "sess_2"
            jti_3 = "sess_3"

            # Register sessions with max 2 (FIFO rotation)
            await store.register_session(user_id, jti_1, max_sessions=2)
            await store.register_session(user_id, jti_2, max_sessions=2)
            self.assertTrue(await store.is_session_active(user_id, jti_1))
            self.assertTrue(await store.is_session_active(user_id, jti_2))

            # Adding 3rd session should evict jti_1
            await store.register_session(user_id, jti_3, max_sessions=2)
            self.assertFalse(await store.is_session_active(user_id, jti_1))
            self.assertTrue(await store.is_session_active(user_id, jti_2))
            self.assertTrue(await store.is_session_active(user_id, jti_3))

            # Invalidate session 2
            await store.invalidate_session(user_id, jti_2)
            self.assertFalse(await store.is_session_active(user_id, jti_2))

            # Invalidate all
            await store.invalidate_all_user_sessions(user_id)
            self.assertFalse(await store.is_session_active(user_id, jti_3))

        self.loop.run_until_complete(_run())

    def test_postgres_session_store_mock_db(self) -> None:
        mock_session = AsyncMock()
        mock_session.add = MagicMock()
        mock_execute_res = MagicMock()
        mock_execute_res.scalars.return_value.all.return_value = []
        mock_execute_res.fetchall.return_value = []
        mock_session.execute.return_value = mock_execute_res

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__.return_value = mock_session

        store = PostgresSessionStore(mock_factory)

        async def _run():
            user_id = str(uuid4())
            jti = "token_abc"
            await store.register_session(user_id, jti, max_sessions=2)
            self.assertTrue(mock_session.add.called)
            self.assertTrue(mock_session.commit.called)

        self.loop.run_until_complete(_run())

    def test_event_broker_publish(self) -> None:
        mock_redis = AsyncMock()
        redis_broker = RedisEventBroker(mock_redis)

        async def _run():
            await redis_broker.publish("state_changes", {"endpoint_id": "123", "state": "UP"})
            mock_redis.publish.assert_called_once()
            args = mock_redis.publish.call_args[0]
            self.assertEqual(args[0], "state_changes")
            self.assertIn("UP", args[1])

        self.loop.run_until_complete(_run())


class TestMilestone2HighFidelityDiagnostics(unittest.TestCase):
    """Test high-fidelity diagnostics, multi-probe parsing, and L2 auto-bypass."""

    def test_multi_probe_q2_output_parsing(self) -> None:
        sample_q2_output = (
            "traceroute to 8.8.8.8 (8.8.8.8), 30 hops max, 60 byte packets\n"
            " 1  192.168.1.1  0.500 ms  0.400 ms\n"
            " 2  10.0.0.1  5.100 ms  5.300 ms\n"
            " 3  * *\n"
            " 4  8.8.8.8  14.200 ms  14.600 ms\n"
        )
        hops = _parse_trace_output(sample_q2_output)
        self.assertEqual(len(hops), 4)

        # Hop 1 average of (0.500 + 0.400) / 2 = 0.450
        self.assertEqual(hops[0]["hop"], 1)
        self.assertEqual(hops[0]["ip"], "192.168.1.1")
        self.assertAlmostEqual(hops[0]["rtt_ms"], 0.450, places=3)

        # Hop 2 average of (5.100 + 5.300) / 2 = 5.200
        self.assertEqual(hops[1]["hop"], 2)
        self.assertEqual(hops[1]["ip"], "10.0.0.1")
        self.assertAlmostEqual(hops[1]["rtt_ms"], 5.200, places=3)

        # Hop 3 timeout
        self.assertEqual(hops[2]["hop"], 3)
        self.assertIsNone(hops[2]["ip"])

        # Hop 4 terminal
        self.assertEqual(hops[3]["hop"], 4)
        self.assertEqual(hops[3]["ip"], "8.8.8.8")
        self.assertAlmostEqual(hops[3]["rtt_ms"], 14.400, places=3)

    def test_layer2_subnet_auto_bypass(self) -> None:
        # Loopback target auto-bypass
        self.assertTrue(is_local_subnet_destination("127.0.0.1"))
        self.assertTrue(is_local_subnet_destination("127.0.0.2"))

    def test_trace_semaphore_bound(self) -> None:
        self.assertEqual(trace_semaphore._value, 3)


if __name__ == "__main__":
    unittest.main()
