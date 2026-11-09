import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../backend")))

import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.services.baseline_service import (
    DEFAULT_MEAN_MS,
    DEFAULT_STDDEV_MS,
    BaselineCache,
    calculate_z_score,
    is_latency_degraded,
)
from monitoring.ping import PingResult, classify_ping_result


class TestHybridAdaptiveBaseline(unittest.TestCase):

    def setUp(self) -> None:
        self.cache = BaselineCache()
        self.endpoint_id = uuid4()
        self.now = datetime.now(timezone.utc)

    def test_no_baseline_for_uncached_endpoint(self) -> None:
        # Without history there is nothing to judge RTT against (it used to be 50 +/- 15 ms,
        # which marked any healthy endpoint above 95 ms UP-UNSTABLE).
        self.assertIsNone(self.cache.get_baseline(self.endpoint_id, self.now))

    def test_grace_period_fallback_for_new_endpoint(self) -> None:
        # Endpoint onboarded 2 days ago (< 7 days)
        onboarded_time = self.now - timedelta(days=2)
        self.cache._onboarded_dates[self.endpoint_id] = onboarded_time

        # Populate cache 1D array of 168 entries
        arr = [None] * 168
        dow = self.now.weekday()
        hour = self.now.hour
        idx = (dow * 24) + hour
        arr[idx] = (20.0, 5.0, 100)
        self.cache._cache[self.endpoint_id] = arr

        # Still no baseline during the 7-day grace period: a few days are too little history
        self.assertIsNone(self.cache.get_baseline(self.endpoint_id, self.now))

    def test_baseline_lookup_after_grace_period(self) -> None:
        # Endpoint onboarded 10 days ago (> 7 days)
        onboarded_time = self.now - timedelta(days=10)
        self.cache._onboarded_dates[self.endpoint_id] = onboarded_time

        arr = [None] * 168
        dow = self.now.weekday()
        hour = self.now.hour
        idx = (dow * 24) + hour
        expected_mean, expected_stddev, expected_count = 25.5, 4.2, 250
        arr[idx] = (expected_mean, expected_stddev, expected_count)
        self.cache._cache[self.endpoint_id] = arr

        mean, stddev, count = self.cache.get_baseline(self.endpoint_id, self.now)
        self.assertEqual(mean, expected_mean)
        self.assertEqual(stddev, expected_stddev)
        self.assertEqual(count, expected_count)

    def test_empty_hour_slot_has_no_baseline(self) -> None:
        self.cache._onboarded_dates[self.endpoint_id] = self.now - timedelta(days=10)
        self.cache._cache[self.endpoint_id] = [None] * 168
        self.assertIsNone(self.cache.get_baseline(self.endpoint_id, self.now))

    def test_slow_endpoint_without_history_is_up(self) -> None:
        # A distant site answering every ping in 120 ms: UP, not UP-UNSTABLE, until its own
        # history exists. Loss still counts as before.
        baseline = self.cache.get_baseline(self.endpoint_id)
        mean, stddev = (baseline[0], baseline[1]) if baseline else (None, None)
        all_replies = PingResult(success_count=5, failed_count=0, avg_rtt_ms=120.0)
        self.assertEqual(classify_ping_result(all_replies, baseline_mean=mean, baseline_stddev=stddev), ("UP", "UP"))
        two_lost = PingResult(success_count=3, failed_count=2, avg_rtt_ms=120.0)
        self.assertEqual(classify_ping_result(two_lost, baseline_mean=mean, baseline_stddev=stddev), ("UP", "UP-UNSTABLE"))

    def test_1d_array_index_calculation_helper(self) -> None:
        onboarded_time = self.now - timedelta(days=10)
        self.cache._onboarded_dates[self.endpoint_id] = onboarded_time

        arr = [None] * 168
        # Day of week 3 (Thursday), hour 14 -> index = 3 * 24 + 14 = 86
        idx = (3 * 24) + 14
        arr[idx] = (33.0, 7.0, 500)
        self.cache._cache[self.endpoint_id] = arr

        mean, stddev, count = self.cache.get_baseline(self.endpoint_id, 3, 14)
        self.assertEqual(mean, 33.0)
        self.assertEqual(stddev, 7.0)
        self.assertEqual(count, 500)

    def test_z_score_calculation(self) -> None:
        # mean = 50, stddev = 10, reading = 80 -> z = (80 - 50) / 10 = 3.0
        z = calculate_z_score(latency_ms=80.0, mean_ms=50.0, stddev_ms=10.0)
        self.assertAlmostEqual(z, 3.0)

    def test_division_by_zero_protection(self) -> None:
        # Zero stddev must not raise DivisionByZero
        z_zero_stddev = calculate_z_score(latency_ms=100.0, mean_ms=50.0, stddev_ms=0.0)
        self.assertIsInstance(z_zero_stddev, float)

        # None stddev must not raise Exception
        z_none_stddev = calculate_z_score(latency_ms=100.0, mean_ms=50.0, stddev_ms=None)  # type: ignore
        self.assertIsInstance(z_none_stddev, float)

    def test_is_latency_degraded(self) -> None:
        mean, stddev = 30.0, 10.0
        # Threshold = 30 + 3 * 10 = 60.0
        self.assertFalse(is_latency_degraded(latency_ms=50.0, mean_ms=mean, stddev_ms=stddev, k=3.0))
        self.assertFalse(is_latency_degraded(latency_ms=60.0, mean_ms=mean, stddev_ms=stddev, k=3.0))
        self.assertTrue(is_latency_degraded(latency_ms=60.1, mean_ms=mean, stddev_ms=stddev, k=3.0))
        self.assertTrue(is_latency_degraded(latency_ms=100.0, mean_ms=mean, stddev_ms=stddev, k=3.0))

    def test_classify_ping_result_dynamic_baseline(self) -> None:
        # 100% successful ping, but latency = 120ms (baseline mean=30ms, stddev=10ms)
        # Threshold = 30 + 3*10 = 60ms. 120ms > 60ms -> UP, UP-UNSTABLE
        result = PingResult(success_count=10, failed_count=0, avg_rtt_ms=120.0)
        op_state, det_state = classify_ping_result(
            result, baseline_mean=30.0, baseline_stddev=10.0, k=3.0
        )
        self.assertEqual(op_state, "UP")
        self.assertEqual(det_state, "UP-UNSTABLE")

        # Latency within normal bounds (40ms <= 60ms) -> UP, UP
        result_normal = PingResult(success_count=10, failed_count=0, avg_rtt_ms=40.0)
        op_state_n, det_state_n = classify_ping_result(
            result_normal, baseline_mean=30.0, baseline_stddev=10.0, k=3.0
        )
        self.assertEqual(op_state_n, "UP")
        self.assertEqual(det_state_n, "UP")


if __name__ == "__main__":
    unittest.main()
