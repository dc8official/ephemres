"""Traceroute links checked against flow next hops, and ROUTE_HINT guards (v3.4 Phase 4)."""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.services.flow_paths import (
    EDGE_CONFIRMED,
    EDGE_DIFFERS,
    EDGE_INFERRED,
    classify_route_edges,
)

EP = "ep-1"
EXP = "exp-1"


def _route(*hops):
    """hops: (node_id, ip); the endpoint closes the path."""
    return [(EP, list(hops) + [(EP, "10.9.9.9")])]


def test_confirmed_when_next_hop_matches_the_next_traceroute_hop():
    routes = _route(("transit:10.0.0.1", "10.0.0.1"), ("transit:10.0.0.2", "10.0.0.2"))
    result = classify_route_edges(routes, {"10.0.0.1": EXP}, {(EXP, EP): ["10.0.0.2"]})
    assert result == {
        ("transit:10.0.0.1", "transit:10.0.0.2"): {
            "flow_status": EDGE_CONFIRMED,
            "flow_detail": "Traffic from 10.0.0.1 goes to 10.0.0.2",
        }
    }


def test_differs_when_traffic_uses_another_next_hop():
    routes = _route(("transit:10.0.0.1", "10.0.0.1"), ("transit:10.0.0.2", "10.0.0.2"))
    result = classify_route_edges(routes, {"10.0.0.1": EXP}, {(EXP, EP): ["10.0.0.6"]})
    (entry,) = result.values()
    assert entry["flow_status"] == EDGE_DIFFERS
    assert "10.0.0.6" in entry["flow_detail"] and "10.0.0.2" in entry["flow_detail"]


def test_inferred_fills_a_silent_hop():
    routes = _route(("transit:10.0.0.1", "10.0.0.1"), ("anon_10_0_0_1_to_ep-1", None))
    result = classify_route_edges(routes, {"10.0.0.1": EXP}, {(EXP, EP): ["10.0.0.2"]})
    assert result[("transit:10.0.0.1", "anon_10_0_0_1_to_ep-1")]["flow_status"] == EDGE_INFERRED
    assert result[("transit:10.0.0.1", "anon_10_0_0_1_to_ep-1")]["inferred_ip"] == "10.0.0.2"


def test_last_hop_to_the_endpoint_and_unknowns():
    routes = _route(("transit:10.0.0.1", "10.0.0.1"))
    # Next hop is the endpoint itself: confirmed.
    assert classify_route_edges(routes, {"10.0.0.1": EXP}, {(EXP, EP): ["10.9.9.9"]})[
        ("transit:10.0.0.1", EP)
    ]["flow_status"] == EDGE_CONFIRMED
    # Hop not an exporter, or no recent next hop: left out (unknown), never "differs".
    assert classify_route_edges(routes, {}, {(EXP, EP): ["10.9.9.9"]}) == {}
    assert classify_route_edges(routes, {"10.0.0.1": EXP}, {}) == {}


def test_shared_link_confirmed_by_any_route_wins():
    link = ("transit:10.0.0.1", "transit:10.0.0.2")
    routes = [
        ("ep-a", [("transit:10.0.0.1", "10.0.0.1"), ("transit:10.0.0.2", "10.0.0.2"), ("ep-a", "10.9.0.1")]),
        ("ep-b", [("transit:10.0.0.1", "10.0.0.1"), ("transit:10.0.0.2", "10.0.0.2"), ("ep-b", "10.9.0.2")]),
    ]
    hops = {(EXP, "ep-a"): ["10.0.0.6"], (EXP, "ep-b"): ["10.0.0.2"]}
    assert classify_route_edges(routes, {"10.0.0.1": EXP}, hops)[link]["flow_status"] == EDGE_CONFIRMED


# ── ROUTE_HINT → targeted traceroute ──────────────────────────────────────────


def _session_returning(row):
    session = AsyncMock()
    session.__aenter__.return_value = session
    result = MagicMock()
    result.fetchone.return_value = row
    session.execute.return_value = result
    return MagicMock(return_value=session)


@pytest.fixture
def relay_env():
    from app.services import baseline_route
    from app.services.telemetry_relay import TelemetryRelay

    queue = MagicMock()
    queue.put = AsyncMock()
    in_progress = set()
    with patch.object(baseline_route, "discovery_route_queue", queue), patch.object(
        baseline_route, "discovery_in_progress", in_progress
    ):
        yield TelemetryRelay(), queue, in_progress


@pytest.mark.anyio
async def test_route_hint_queues_one_traceroute_per_cooldown(relay_env):
    relay, queue, in_progress = relay_env
    ep = str(uuid4())
    with patch("app.services.telemetry_relay.AsyncSessionLocal", _session_returning(("10.9.9.9",))):
        assert await relay._handle_route_hint({"endpoint_id": ep}, now=1000.0) is True
        in_progress.clear()  # the traceroute finished
        assert await relay._handle_route_hint({"endpoint_id": ep}, now=1000.0 + 600) is False
        assert await relay._handle_route_hint({"endpoint_id": ep}, now=1000.0 + 901) is True
    assert queue.put.await_count == 2
    assert str(queue.put.await_args_list[0][0][0][0]) == ep


@pytest.mark.anyio
async def test_route_hint_skips_down_or_opted_out_endpoints(relay_env):
    relay, queue, _ = relay_env
    # The query only returns UP endpoints with scheduled discovery on.
    with patch("app.services.telemetry_relay.AsyncSessionLocal", _session_returning(None)):
        assert await relay._handle_route_hint({"endpoint_id": str(uuid4())}, now=1.0) is False
    queue.put.assert_not_awaited()


@pytest.mark.anyio
async def test_route_hint_global_cap(relay_env):
    from app.services.telemetry_relay import ROUTE_HINT_MAX_PER_MINUTE

    relay, queue, _ = relay_env
    with patch("app.services.telemetry_relay.AsyncSessionLocal", _session_returning(("10.9.9.9",))):
        results = [await relay._handle_route_hint({"endpoint_id": str(uuid4())}, now=50.0) for _ in range(ROUTE_HINT_MAX_PER_MINUTE + 3)]
        assert results.count(True) == ROUTE_HINT_MAX_PER_MINUTE
        # A minute later the window has room again.
        assert await relay._handle_route_hint({"endpoint_id": str(uuid4())}, now=111.0) is True
