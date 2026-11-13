"""SNMP poller step 2: the rules that turn two counter readings into per-minute traffic
(monitoring/snmp/poller.py counter_delta and spread)."""
from datetime import datetime, timezone

from monitoring.snmp.poller import COUNTER32, Sample, counter_delta, spread

T0 = 1_762_934_400.0  # 2025-11-12 08:00:00 UTC, a minute boundary


def reading(at, uptime, values, hc=True, disc=0):
    return Sample(at, uptime, disc, hc, values)


def test_a_normal_delta_uses_the_devices_clock():
    prev = reading(T0, 100_000, [1000, 2000, 10, 20, 0, 0, 0, 0])
    cur = reading(T0 + 61, 106_000, [7_500_000, 2500, 110, 25, 1, 0, 2, 0])
    assert counter_delta(prev, cur, 60, 1000) == [7_499_000, 500, 100, 5, 1, 0, 2, 0]


def test_resets_gaps_and_glitches_are_dropped():
    prev = reading(T0, 100_000, [5000, 5000, 0, 0, 0, 0, 0, 0])
    # A 64-bit counter going back is a reset.
    assert counter_delta(prev, reading(T0 + 60, 106_000, [10, 5000, 0, 0, 0, 0, 0, 0]), 60, 1000) is None
    # ifCounterDiscontinuityTime moved.
    assert counter_delta(prev, reading(T0 + 60, 106_000, [6000, 6000, 0, 0, 0, 0, 0, 0], disc=500), 60, 1000) is None
    # More than three intervals apart.
    assert counter_delta(prev, reading(T0 + 60 * 3 + 31, 106_000, [6000, 6000, 0, 0, 0, 0, 0, 0]), 60, 1000) is None
    # 10 Mb/s link, 200 MB in a minute (26.7 Mb/s, over 1.5 x the speed): a glitch.
    assert counter_delta(prev, reading(T0 + 60, 106_000, [200_005_000, 5000, 0, 0, 0, 0, 0, 0]), 60, 10) is None
    # 64-bit on one side only: the counters are not comparable.
    assert counter_delta(prev, reading(T0 + 60, 106_000, [6000, 6000, 0, 0, 0, 0, 0, 0], hc=False), 60, 1000) is None


def test_a_32_bit_counter_that_wrapped_once_is_added_up():
    prev = reading(T0, 100_000, [COUNTER32 - 1000, 0, 0, 0, COUNTER32 - 1, 0, 0, 0], hc=False)
    cur = reading(T0 + 60, 106_000, [500, 0, 0, 0, 2, 0, 0, 0], hc=False)
    assert counter_delta(prev, cur, 60, 100) == [1500, 0, 0, 0, 3, 0, 0, 0]


def test_without_uptime_our_clock_is_the_time_base_and_missing_columns_count_zero():
    prev = reading(T0, None, [0, 0, None, None, 0, 0, 0, 0])
    cur = reading(T0 + 60, None, [600, 0, None, None, 0, 0, 0, 0])
    assert counter_delta(prev, cur, 60, 1) == [600, 0, 0, 0, 0, 0, 0, 0]


def test_spread_shares_by_time_and_keeps_totals_exact():
    # 30 s in the first minute, 60 in the second, 10 in the third: 100 s in all.
    minutes = spread(T0 + 30, T0 + 130, [1001, 0, 7, 0, 0, 0, 0, 0])
    m = {k.astimezone(timezone.utc).strftime("%H:%M"): v[0] for k, v in minutes.items()}
    assert m == {"08:00": 300, "08:01": 600, "08:02": 101}
    assert sum(v[0] for v in minutes.values()) == 1001 and sum(v[2] for v in minutes.values()) == 7
    # Inside one minute: all of it there.
    assert list(spread(T0 + 5, T0 + 50, [9, 0, 0, 0, 0, 0, 0, 0]).values()) == [[9, 0, 0, 0, 0, 0, 0, 0]]
    assert spread(T0 + 5, T0 + 5, [9, 0, 0, 0, 0, 0, 0, 0]) == {}
    assert next(iter(spread(T0, T0 + 10, [1] + [0] * 7))) == datetime.fromtimestamp(T0, tz=timezone.utc)
