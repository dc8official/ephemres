"""The route-discovery dispatcher's two limits, run for real with fake traces.

Found in the GNS3 LNMP II lab (2026-11-17): every trace there passes the server's gateway, and
with "per router 3" no more than 2 ran at once; with "per router 1" discovery stopped after the
first trace and waited for good. These drive start_midnight_discovery_worker itself, with
refresh_baseline_route replaced by a sleep, so the counting and the waiting are what is tested.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.services.baseline_route as br

GATEWAY = "192.168.10.1"


async def _run(parallel: int, per_router: int, durations: list[float], limit_s: float = 5.0):
    """Queues one trace per duration (all through the gateway, as in the lab) and returns the
    most that ran at once and how many finished before limit_s."""
    live: set[str] = set()
    peak = [0]
    done: list[str] = []
    ips = [f"10.0.{i}.1" for i in range(len(durations))]
    took = dict(zip(ips, durations))

    async def fake_trace(endpoint_id, ip, db=None, trigger=None):
        live.add(ip)
        peak[0] = max(peak[0], len(live))
        await asyncio.sleep(took[ip])
        live.discard(ip)
        done.append(ip)
        return {"hops": [], "path_outcome": "same"}

    async def through_gateway(db, endpoint_id, ip):
        return {GATEWAY}

    @asynccontextmanager
    async def fake_db():
        yield SimpleNamespace(commit=lambda: asyncio.sleep(0))

    async def nothing(*_a, **_k):
        return None

    settings = SimpleNamespace(discovery_parallel=parallel, discovery_per_router=per_router, rediscovery_hours=24)
    with patch.object(br, "discovery_route_queue", asyncio.Queue()), \
         patch.object(br, "START_GAP_SECONDS", 0.05), \
         patch.object(br, "refresh_baseline_route", fake_trace), \
         patch.object(br, "_routers_on_path", through_gateway), \
         patch.object(br, "_announce_topology_change", nothing), \
         patch("app.services.runtime_settings.get_runtime_settings", return_value=settings), \
         patch("app.services.topology.topology_manager.update_endpoint_path", nothing):
        task = await br.start_midnight_discovery_worker(fake_db)
        for i, ip in enumerate(ips):
            await br.discovery_route_queue.put((f"ep{i}", ip, "scheduled"))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit_s
        while len(done) < len(ips) and loop.time() < deadline:
            await asyncio.sleep(0.02)
        task.cancel()
    return peak[0], len(done)


@pytest.mark.anyio
@pytest.mark.parametrize("parallel, per_router, expected", [(1, 3, 1), (2, 3, 2), (8, 3, 3), (8, 2, 2)])
async def test_traces_through_one_gateway_run_up_to_the_smaller_limit(parallel, per_router, expected):
    # As in the lab, the first trace (the local gateway, skipped at once) ends inside the first
    # start gap. The old running tally still counted it, so the gateway looked full one trace
    # early and the peak stayed one below the limit.
    peak, finished = await _run(parallel, per_router, [0.01] + [0.3] * 8)
    assert peak == expected
    assert finished == 9


@pytest.mark.anyio
async def test_discovery_carries_on_after_a_trace_ends_during_a_start_gap():
    # The lab's case: the first trace (the local gateway, skipped at once) ends inside the start
    # gap. Its wake-up used to be cleared before the wait, so at one trace per router nothing
    # else ever started.
    peak, finished = await _run(8, 1, [0.01] + [0.1] * 5)
    assert peak == 1
    assert finished == 6
