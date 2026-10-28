"""Next-hop decoding, path observations and route-change detection (v3.4 Phase 4)."""
import datetime
import socket
import struct
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from monitoring.flow.aggregator import FlowAggregator
from monitoring.flow.ipfix_parser import (
    IE_DESTINATION_IPV4_ADDRESS,
    IE_IP_NEXT_HOP_IPV4_ADDRESS,
    IE_OCTET_DELTA_COUNT,
    IE_SOURCE_IPV4_ADDRESS,
    IPFIXParser,
)
from monitoring.flow.path_tracker import DIRECTION_FROM, DIRECTION_TO, PathTracker
from monitoring.flow.v5_parser import parse_v5_packet
from monitoring.flow.v9_parser import (
    FT_IN_BYTES,
    FT_IPV4_DST_ADDR,
    FT_IPV4_NEXT_HOP,
    FT_IPV4_SRC_ADDR,
    FT_IPV6_DST_ADDR,
    FT_IPV6_NEXT_HOP,
    FT_IPV6_SRC_ADDR,
    V9Parser,
)
from tests.monitoring.test_flow_parsers import TestIPFIXParser, TestNetflowV5Parser, TestNetflowV9Parser


# ── Parsers ────────────────────────────────────────────────────────────────────


def test_v5_next_hop_and_zero_address():
    build = TestNetflowV5Parser()._create_v5_packet
    flows = parse_v5_packet(build(records=[{"next_hop": "10.0.0.6"}, {}]), "192.0.2.1")
    assert [f["next_hop"] for f in flows] == ["10.0.0.6", None]


def test_v9_ipv4_and_ipv6_next_hop():
    helper = TestNetflowV9Parser()
    parser = V9Parser()
    parser.parse_packet(
        helper._build_v9_template_packet(1, 256, [(FT_IPV4_SRC_ADDR, 4), (FT_IPV4_DST_ADDR, 4), (FT_IPV4_NEXT_HOP, 4), (FT_IN_BYTES, 4)]),
        "192.0.2.1",
    )
    rec = struct.pack("!4s4s4sI", socket.inet_aton("10.1.1.1"), socket.inet_aton("10.2.2.2"), socket.inet_aton("10.0.0.6"), 100)
    zero = struct.pack("!4s4s4sI", socket.inet_aton("10.1.1.1"), socket.inet_aton("10.2.2.2"), b"\x00" * 4, 100)
    flows = parser.parse_packet(helper._build_v9_data_packet(1, 256, [rec, zero]), "192.0.2.1")
    assert [f["next_hop"] for f in flows] == ["10.0.0.6", None]

    parser.parse_packet(
        helper._build_v9_template_packet(2, 257, [(FT_IPV6_SRC_ADDR, 16), (FT_IPV6_DST_ADDR, 16), (FT_IPV6_NEXT_HOP, 16)]),
        "192.0.2.1",
    )
    v6 = struct.pack(
        "!16s16s16s",
        socket.inet_pton(socket.AF_INET6, "2001:db8::1"),
        socket.inet_pton(socket.AF_INET6, "2001:db8::2"),
        socket.inet_pton(socket.AF_INET6, "fe80::1"),
    )
    flows = parser.parse_packet(helper._build_v9_data_packet(2, 257, [v6]), "192.0.2.1")
    assert flows[0]["next_hop"] == "fe80::1"


def test_v9_without_next_hop_field_reports_none():
    helper = TestNetflowV9Parser()
    parser = V9Parser()
    parser.parse_packet(helper._build_v9_template_packet(1, 256, [(FT_IPV4_SRC_ADDR, 4), (FT_IPV4_DST_ADDR, 4)]), "192.0.2.1")
    rec = struct.pack("!4s4s", socket.inet_aton("10.1.1.1"), socket.inet_aton("10.2.2.2"))
    flows = parser.parse_packet(helper._build_v9_data_packet(1, 256, [rec]), "192.0.2.1")
    assert flows[0]["next_hop"] is None


def test_ipfix_next_hop():
    helper = TestIPFIXParser()
    parser = IPFIXParser()
    fields = [(IE_SOURCE_IPV4_ADDRESS, 4), (IE_DESTINATION_IPV4_ADDRESS, 4), (IE_IP_NEXT_HOP_IPV4_ADDRESS, 4), (IE_OCTET_DELTA_COUNT, 8)]
    parser.parse_packet(helper._build_ipfix_template_packet(7, 300, fields), "192.0.2.9")
    rec = struct.pack("!4s4s4sQ", socket.inet_aton("10.1.1.1"), socket.inet_aton("10.2.2.2"), socket.inet_aton("10.9.9.9"), 5000)
    flows = parser.parse_packet(helper._build_ipfix_data_packet(7, 300, [rec]), "192.0.2.9")
    assert flows[0]["next_hop"] == "10.9.9.9"


# ── Change detector ───────────────────────────────────────────────────────────


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


EXP, EP = uuid4(), uuid4()
PATH_A = (1, "10.0.0.2")
PATH_B = (2, "10.0.0.6")


def _minute(tracker, clock, paths):
    """One arrival minute with `paths` = {(out_if, next_hop): bytes}, then roll to the next."""
    for (out_if, nh), nbytes in paths.items():
        tracker.observe(EXP, EP, DIRECTION_TO, 5, out_if, nh, nbytes, clock.now - 30, clock.now - 1)
    clock.now += 60
    tracker.tick()


def _tracker(minutes=3):
    clock = Clock()
    return PathTracker(change_minutes=minutes, ecmp_share=0.2, clock=clock), clock


def test_steady_path_reports_nothing():
    tracker, clock = _tracker()
    for _ in range(10):
        _minute(tracker, clock, {PATH_A: 1000})
    assert tracker.drain_changes() == []


def test_switch_is_reported_after_n_minutes():
    tracker, clock = _tracker(minutes=3)
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_B: 1000})
    _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []
    _minute(tracker, clock, {PATH_B: 1000, PATH_A: 50})  # old path below 20 %
    (change,) = tracker.drain_changes()
    assert (change.old_out_if, change.old_next_hop) == PATH_A
    assert (change.new_out_if, change.new_next_hop) == PATH_B
    assert change.direction == DIRECTION_TO and change.endpoint_id == EP and change.exporter_id == EXP
    assert change.to_event()["event_type"] == "ROUTE_HINT"
    # Learned: staying on B reports nothing more.
    _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []


def test_flap_shorter_than_n_minutes_is_ignored():
    tracker, clock = _tracker(minutes=3)
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_B: 1000})
    _minute(tracker, clock, {PATH_B: 1000})
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []


def test_load_balancing_is_not_a_change():
    tracker, clock = _tracker(minutes=2)
    _minute(tracker, clock, {PATH_A: 1000})
    for _ in range(5):
        _minute(tracker, clock, {PATH_A: 600, PATH_B: 400})
    # One leg of the learned set carrying everything is not a change either.
    for _ in range(3):
        _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []
    # A path outside the set is.
    for _ in range(2):
        _minute(tracker, clock, {(3, "10.0.0.10"): 1000})
    (change,) = tracker.drain_changes()
    assert change.new_out_if == 3


def test_switch_inside_a_minute_is_reported():
    # Routes rarely switch on a minute boundary: the switch minute carries both paths, which must
    # not be learned as load balancing (that adopted the new path without ever reporting it).
    tracker, clock = _tracker(minutes=3)
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_A: 500, PATH_B: 500})  # switched half way through the minute
    _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []
    _minute(tracker, clock, {PATH_B: 1000})
    (change,) = tracker.drain_changes()
    assert (change.old_out_if, change.old_next_hop) == PATH_A
    assert (change.new_out_if, change.new_next_hop) == PATH_B
    _minute(tracker, clock, {PATH_B: 1000})
    assert tracker.drain_changes() == []


def test_short_split_that_falls_back_is_not_a_change():
    tracker, clock = _tracker(minutes=3)
    _minute(tracker, clock, {PATH_A: 1000})
    _minute(tracker, clock, {PATH_A: 500, PATH_B: 500})
    _minute(tracker, clock, {PATH_B: 1000})
    _minute(tracker, clock, {PATH_A: 1000})  # back before the new path lasted 3 minutes
    _minute(tracker, clock, {PATH_A: 1000})
    assert tracker.drain_changes() == []


def test_idle_keys_are_forgotten():
    clock = Clock()
    tracker = PathTracker(change_minutes=1, idle_minutes=5, clock=clock)
    _minute(tracker, clock, {PATH_A: 1000})
    clock.now += 600
    _minute(tracker, clock, {(9, None): 1000})  # rolls the idle key out first
    _minute(tracker, clock, {(9, None): 1000})
    # Re-learned from scratch, so the new path is not reported as a change.
    assert tracker.drain_changes() == []


def test_observations_drain_and_restore_merge():
    tracker, clock = _tracker()
    tracker.observe(EXP, EP, DIRECTION_FROM, 2, 1, None, 100, 10.0, 20.0)
    drained = tracker.drain_observations()
    assert tracker.drain_observations() == {}
    tracker.observe(EXP, EP, DIRECTION_FROM, 2, 1, None, 50, 5.0, 30.0)
    tracker.restore_observations(drained)
    (entry,) = tracker.drain_observations().values()
    assert entry == [5.0, 30.0, 150, 2]


# ── Aggregator ─────────────────────────────────────────────────────────────────


def _aggregator(correlation, publisher=None):
    correlator = MagicMock()
    correlator.correlate.return_value = correlation
    session = AsyncMock()
    session.__aenter__.return_value = session
    factory = MagicMock(return_value=session)
    agg = FlowAggregator(store_client=None, correlator=correlator, db_session_factory=factory, event_publisher=publisher)
    return agg, session


def _flow(agg, **kw):
    args = dict(
        exporter_ip="10.0.0.1", src_ip="192.168.1.10", dst_ip="10.0.0.20", src_port=50000, dst_port=443,
        protocol=6, flow_bytes=1000, flow_packets=2, start_time_unix=1_700_000_000.0, in_if=3, out_if=7,
        next_hop="10.0.0.6", end_time_unix=1_700_000_030.0,
    )
    args.update(kw)
    agg.accumulate_flow(**args)


def _path_rows(session):
    calls = [c[0] for c in session.execute.call_args_list if "INSERT INTO flow_path_observations" in str(c[0][0])]
    return [row for c in calls for row in c[1]]


@pytest.mark.anyio
async def test_aggregator_records_both_directions_and_skips_the_exporter():
    exp, src, dst = uuid4(), uuid4(), uuid4()
    agg, session = _aggregator((exp, src, dst))
    _flow(agg)
    await agg.flush_to_timescaledb()
    rows = sorted(_path_rows(session), key=lambda r: r["direction"])
    assert [(r["endpoint_id"], r["direction"]) for r in rows] == [(src, "from"), (dst, "to")]
    assert rows[0]["next_hop"] == "10.0.0.6" and rows[0]["out_if"] == 7 and rows[0]["in_if"] == 3
    assert rows[0]["first_seen"] == datetime.datetime.fromtimestamp(1_700_000_000, tz=datetime.timezone.utc)
    assert rows[0]["last_seen"] == datetime.datetime.fromtimestamp(1_700_000_030, tz=datetime.timezone.utc)

    # Traffic to the exporter itself, and from an unknown exporter, records no path.
    agg2, session2 = _aggregator((exp, None, exp))
    _flow(agg2)
    agg3, session3 = _aggregator((None, src, dst))
    _flow(agg3)
    await agg2.flush_to_timescaledb()
    await agg3.flush_to_timescaledb()
    assert _path_rows(session2) == [] and _path_rows(session3) == []


@pytest.mark.anyio
async def test_path_write_failure_keeps_observations_and_never_blocks_rollups():
    exp, dst = uuid4(), uuid4()
    agg, session = _aggregator((exp, None, dst))
    _flow(agg)

    async def execute(sql, params=None):
        if "flow_path_observations" in str(sql):
            raise RuntimeError("foreign key violation")
        return MagicMock()

    session.execute.side_effect = execute
    await agg.flush_to_timescaledb()
    assert agg._accumulator == {}  # rollups committed
    assert len(agg.path_tracker._observations) == 1  # kept for the next flush


@pytest.mark.anyio
async def test_route_changes_are_written_and_published():
    published = []

    async def publisher(channel, payload):
        published.append((channel, payload))

    exp, dst = uuid4(), uuid4()
    agg, session = _aggregator((exp, None, dst), publisher)
    clock = Clock()
    agg.path_tracker = PathTracker(change_minutes=1, clock=clock)
    _flow(agg, next_hop="10.0.0.2", out_if=1)
    clock.now += 60
    _flow(agg, next_hop="10.0.0.6", out_if=2)
    clock.now += 60
    await agg.flush_to_timescaledb()

    change_calls = [c[0] for c in session.execute.call_args_list if "INSERT INTO flow_route_changes" in str(c[0][0])]
    (rows,) = [c[1] for c in change_calls]
    assert rows[0]["old_next_hop"] == "10.0.0.2" and rows[0]["new_next_hop"] == "10.0.0.6"
    assert published == [("ROUTE_HINT", published[0][1])]
    assert published[0][1]["endpoint_id"] == str(dst) and published[0][1]["new_out_if"] == 2
