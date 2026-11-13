"""Normal and current route per endpoint on real PostgreSQL (v3.3.6 build plan 6.4).

The branch walk-through agreed with Kenneth, step by step, with time moved by passing `now`:
PE-A fails, traffic moves over ISP-B, MPLS comes back; plus the hold-down, the log limit,
flapping, the 24-hour promotion, pinning and the RCA note.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services import route_paths as rp

T0 = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)


def hops(*ips):
    return [{"hop": i + 1, "ip": ip, "rtt_ms": 1.0 if ip else None} for i, ip in enumerate(ips)]


PRIMARY = hops("10.1.0.1", "172.16.1.1", "10.20.0.1", "10.20.0.2")
BACKUP = hops("10.1.0.1", "10.1.0.254", "41.0.0.1", "10.20.0.1", "10.20.0.2")


@pytest.fixture
def endpoint(run_sql):
    ep_id = uuid.uuid4()

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
               VALUES ($1, $2, 'abj-sw', 'Switch', 'ACTIVE')""",
            ep_id, f"10.20.{ep_id.int % 200}.{ep_id.int % 250 + 2}",
        )

    run_sql(seed)
    return ep_id


def run(dsn, fn):
    async def _inner():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                out = await fn(db)
                await db.commit()
                return out
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def trace(dsn, ep, path, at, trigger="scheduled", confirm_now=False):
    return run(dsn, lambda db: rp.record_trace(db, ep, path, trigger, confirm_now=confirm_now, now=at))


def status(dsn, ep):
    return run(dsn, lambda db: rp.path_status(db, ep))


def test_pure_rules():
    assert rp.same_path(PRIMARY, hops("10.1.0.1", None, "10.20.0.1", "10.20.0.2"))  # a hole is not a change
    assert rp.same_path(PRIMARY, PRIMARY + [{"hop": 5, "ip": None}])  # trailing silence ignored
    assert not rp.same_path(PRIMARY, BACKUP)
    assert rp.first_difference(PRIMARY, BACKUP) == 2
    assert rp.merge_holes(PRIMARY, hops("10.1.0.1", None, "10.20.0.1", "10.20.0.2"))[1]["ip"] == "172.16.1.1"


def test_branch_failover_walkthrough(timescale_dsn, endpoint):
    ep = endpoint
    assert trace(timescale_dsn, ep, PRIMARY, T0, "added")["outcome"] == "first"

    # 14:02 PE-A fails, traffic on ISP-B. One trace is not enough...
    first = trace(timescale_dsn, ep, BACKUP, T0 + timedelta(minutes=2), "transit")
    assert first["outcome"] == "pending" and first["hops"] == PRIMARY
    # ...nor a second one within 2 minutes...
    assert trace(timescale_dsn, ep, BACKUP, T0 + timedelta(minutes=3))["outcome"] == "pending"
    # ...a second one 2 minutes later is.
    assert trace(timescale_dsn, ep, BACKUP, T0 + timedelta(minutes=4, seconds=5), "confirm")["outcome"] == "path_changed"

    s = status(timescale_dsn, ep)
    assert s["current_hops"] == BACKUP and s["normal_hops"] == PRIMARY
    assert not s["on_normal_path"] and s["first_diff_hop"] == 2
    assert s["changes"][0]["kind"] == "path_changed"
    assert "172.16.1.1" in s["changes"][0]["detail"]

    # RCA during the failover compares with BACKUP and says it was on an alternate path.
    note = rp.alternate_path_note(s["current_hops"], s["normal_hops"], s["current_since"])
    assert note.startswith("On an alternate path since 2026-11-12 14:04 UTC")
    assert "via 172.16.1.1" in note

    # 15:30 MPLS back: two agreeing traces return it to normal; logged as back to normal.
    trace(timescale_dsn, ep, PRIMARY, T0 + timedelta(minutes=90))
    assert trace(timescale_dsn, ep, PRIMARY, T0 + timedelta(minutes=93))["outcome"] == "back_to_normal"
    s = status(timescale_dsn, ep)
    assert s["on_normal_path"] and s["changes"][0]["kind"] == "back_to_normal"


def test_a_trace_with_no_answers_keeps_the_path(timescale_dsn, endpoint):
    trace(timescale_dsn, endpoint, PRIMARY, T0, "added")
    assert trace(timescale_dsn, endpoint, hops(None, None), T0 + timedelta(minutes=5))["outcome"] == "no_answer"
    assert status(timescale_dsn, endpoint)["current_hops"] == PRIMARY


def test_manual_refresh_counts_at_once(timescale_dsn, endpoint):
    trace(timescale_dsn, endpoint, PRIMARY, T0, "added")
    assert trace(timescale_dsn, endpoint, BACKUP, T0 + timedelta(minutes=1), "manual", confirm_now=True)["outcome"] == "path_changed"


def test_log_limit_and_flapping(timescale_dsn, endpoint):
    trace(timescale_dsn, endpoint, PRIMARY, T0, "added")
    t = T0
    paths = [BACKUP, PRIMARY, BACKUP, PRIMARY, BACKUP]
    for path in paths:  # five confirmed changes, 6 minutes apart
        t += timedelta(minutes=3)
        trace(timescale_dsn, endpoint, path, t)
        t += timedelta(minutes=3)
        trace(timescale_dsn, endpoint, path, t)

    s = status(timescale_dsn, endpoint)
    kinds = [c["kind"] for c in reversed(s["changes"])]
    # 1st change logged; 2nd and 3rd within 15 min of it not; 4th change in the hour: flapping, once.
    assert kinds == ["path_changed", "flapping"]
    assert s["flapping_since"] is not None
    assert s["current_hops"] == BACKUP  # the path itself still follows

    # An hour quiet ends it.
    run(timescale_dsn, lambda db: rp.promote_normal_paths(db, now=t + timedelta(hours=1, minutes=1)))
    assert status(timescale_dsn, endpoint)["flapping_since"] is None


def test_promotion_after_24_hours_and_pinning(timescale_dsn, endpoint):
    trace(timescale_dsn, endpoint, PRIMARY, T0, "added")
    trace(timescale_dsn, endpoint, BACKUP, T0 + timedelta(minutes=1))
    trace(timescale_dsn, endpoint, BACKUP, T0 + timedelta(minutes=4))

    assert run(timescale_dsn, lambda db: rp.promote_normal_paths(db, now=T0 + timedelta(hours=23))) == 0
    assert run(timescale_dsn, lambda db: rp.promote_normal_paths(db, now=T0 + timedelta(hours=24, minutes=5))) == 1
    s = status(timescale_dsn, endpoint)
    assert s["normal_hops"] == BACKUP and s["on_normal_path"]
    assert s["changes"][0]["kind"] == "became_normal"

    # Pin it; a later path held 24 h does not replace a pinned normal path.
    assert run(timescale_dsn, lambda db: rp.set_normal_path(db, endpoint, pinned=True, now=T0 + timedelta(hours=25)))
    later = T0 + timedelta(hours=26)
    trace(timescale_dsn, endpoint, PRIMARY, later)
    trace(timescale_dsn, endpoint, PRIMARY, later + timedelta(minutes=3))
    assert run(timescale_dsn, lambda db: rp.promote_normal_paths(db, now=later + timedelta(hours=30))) == 0
    s = status(timescale_dsn, endpoint)
    assert s["normal_pinned"] and s["normal_hops"] == BACKUP and not s["on_normal_path"]

    assert run(timescale_dsn, lambda db: rp.unpin_normal_path(db, endpoint, now=later + timedelta(hours=31)))
    assert run(timescale_dsn, lambda db: rp.promote_normal_paths(db, now=later + timedelta(hours=31))) == 1


def test_route_api_and_admin_actions(api_client, run_sql):
    ep_id = uuid.uuid4()

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
               VALUES ($1, '10.77.0.9', 'api-ep', 'Router', 'ACTIVE')""",
            ep_id,
        )

    run_sql(seed)
    assert api_client.get(f"/api/v1/endpoints/{ep_id}/route").json()["data"] is None
    assert api_client.post(f"/api/v1/endpoints/{ep_id}/route/normal", json={"pinned": True}).status_code == 404

    async def add_path(conn):
        await conn.execute(
            """INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops, normal_hops)
               VALUES ($1, 2, $2::jsonb, $2::jsonb)""",
            ep_id, '[{"hop": 1, "ip": "10.1.0.1"}, {"hop": 2, "ip": "10.77.0.9"}]',
        )

    run_sql(add_path)
    res = api_client.post(f"/api/v1/endpoints/{ep_id}/route/normal", json={"pinned": True})
    assert res.status_code == 200, res.text
    data = api_client.get(f"/api/v1/endpoints/{ep_id}/route").json()["data"]
    assert data["normal_pinned"] and data["on_normal_path"]
    assert data["changes"][0]["kind"] == "pinned"
    assert api_client.delete(f"/api/v1/endpoints/{ep_id}/route/pin").status_code == 200
    assert api_client.delete(f"/api/v1/endpoints/{ep_id}/route/pin").status_code == 404


def test_transit_trigger_retraces_endpoints_routed_through_a_device(timescale_dsn, run_sql):
    """R-ABJ (10.20.0.1) is monitored and abj-sw routes through it. R-ABJ going DOWN re-traces
    abj-sw (still UP); R-ABJ coming back re-traces endpoints whose normal path uses it but whose
    current path doesn't."""
    from unittest.mock import patch

    from app.services import baseline_route
    from app.services.telemetry_relay import TelemetryRelay

    router_id, sw_id, moved_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def seed(conn):
        for eid, ip, name in ((router_id, "10.20.0.1", "r-abj"), (sw_id, "10.20.0.2", "abj-sw"),
                              (moved_id, "10.20.0.3", "abj-ap")):
            await conn.execute(
                """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
                   VALUES ($1, $2, $3, 'Router', 'ACTIVE')""", eid, ip, name,
            )
        import json as _json
        await conn.execute(
            "INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops, normal_hops) VALUES ($1, 4, $2::jsonb, $2::jsonb)",
            sw_id, _json.dumps(PRIMARY),
        )
        backup_ap = hops("10.1.0.1", "10.1.0.254", "41.0.0.1", "10.20.0.3")
        primary_ap = hops("10.1.0.1", "172.16.1.1", "10.20.0.1", "10.20.0.3")
        await conn.execute(
            "INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops, normal_hops) VALUES ($1, 4, $2::jsonb, $3::jsonb)",
            moved_id, _json.dumps(backup_ap), _json.dumps(primary_ap),
        )

    run_sql(seed)

    async def scenario(direction):
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        queue = asyncio.Queue()
        relay = TelemetryRelay()
        try:
            with patch("app.services.telemetry_relay.AsyncSessionLocal", factory), \
                 patch.object(baseline_route, "discovery_route_queue", queue), \
                 patch.object(baseline_route, "discovery_in_progress", set()):
                looked_at = await relay._maybe_trace_dependents(str(router_id), direction, "10.20.0.1", delay=0)
                await asyncio.gather(*list(relay._transit_tasks))
        finally:
            await engine.dispose()
        queued = []
        while not queue.empty():
            queued.append(queue.get_nowait())
        return looked_at, queued

    looked_at, queued = asyncio.run(scenario("DOWN"))
    assert looked_at == [str(sw_id)]
    assert [(str(q[0]), q[2]) for q in queued] == [(str(sw_id), "transit")]

    looked_at, queued = asyncio.run(scenario("UP"))
    assert looked_at == [str(moved_id)]
    assert [(str(q[0]), q[2]) for q in queued] == [(str(moved_id), "transit")]


def test_parallel_traces_take_turns_at_a_shared_router(timescale_dsn, run_sql):
    """Parallel 3: A and B both route through 10.1.0.1 and must not overlap; C goes through
    10.9.9.1 and runs alongside A."""
    import json as _json
    import time
    from unittest.mock import AsyncMock, patch
    from dataclasses import replace

    from app.services import baseline_route, runtime_settings
    from app.services.runtime_settings import config_defaults, set_runtime_settings

    ids = {name: uuid.uuid4() for name in ("A", "B", "C")}
    paths = {
        "A": hops("10.1.0.1", "10.30.0.1"),
        "B": hops("10.1.0.1", "10.30.0.2"),
        "C": hops("10.9.9.1", "10.30.0.3"),
    }

    async def seed(conn):
        for name, eid in ids.items():
            await conn.execute(
                "INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status) VALUES ($1, $2, $3, 'Server', 'ACTIVE')",
                eid, paths[name][-1]["ip"], f"par-{name}",
            )
            await conn.execute(
                "INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops, normal_hops) VALUES ($1, 2, $2::jsonb, $2::jsonb)",
                eid, _json.dumps(paths[name]),
            )

    run_sql(seed)
    spans = {}

    async def fake_refresh(endpoint_id, ip, db=None, trigger="scheduled", confirm_now=False):
        name = next(n for n, e in ids.items() if e == endpoint_id)
        start = time.monotonic()
        await asyncio.sleep(0.6)
        spans[name] = (start, time.monotonic())
        return {"hops": paths[name], "path_outcome": "unchanged"}

    async def scenario():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        queue = asyncio.Queue()
        set_runtime_settings(replace(config_defaults(), discovery_parallel=3))
        try:
            with patch.object(baseline_route, "discovery_route_queue", queue), \
                 patch.object(baseline_route, "discovery_in_progress", set()), \
                 patch.object(baseline_route, "refresh_baseline_route", fake_refresh), \
                 patch.object(baseline_route, "START_GAP_SECONDS", 0.05), \
                 patch("app.services.topology.topology_manager.update_endpoint_path", AsyncMock()), \
                 patch.object(baseline_route, "_announce_topology_change", AsyncMock()):
                worker = await baseline_route.start_midnight_discovery_worker(factory)
                for name in ("A", "B", "C"):
                    await queue.put((ids[name], paths[name][-1]["ip"], "scheduled"))
                for _ in range(60):
                    if len(spans) == 3:
                        break
                    await asyncio.sleep(0.1)
                worker.cancel()
        finally:
            runtime_settings._current = None
            await engine.dispose()

    asyncio.run(scenario())
    assert set(spans) == {"A", "B", "C"}

    def overlap(x, y):
        return spans[x][0] < spans[y][1] and spans[y][0] < spans[x][1]

    assert not overlap("A", "B")
    assert overlap("A", "C")
