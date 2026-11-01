"""NetFlow route augmentation against real TimescaleDB SQL (v3.4 Phase 4, migration 0017).

edge-router (10.0.0.1, exporter) forwards traffic for app-a (10.9.9.9). Its traceroute
baseline is 10.0.0.1 -> 10.0.0.2 -> app-a; flows first say next hop 10.0.0.2, later 10.0.0.6.
"""
import asyncio
import datetime
import json
import uuid
from unittest.mock import patch

import pytest

ROUTER_ID = uuid.UUID("33333333-0000-0000-0000-0000000000a1")
APP_ID = uuid.UUID("33333333-0000-0000-0000-0000000000a2")


def _seed(run_sql):
    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips,
                                   flow_interface_aliases, enable_rca)
            VALUES ($1, '10.0.0.1', 'edge-router', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER',
                    '{10.0.0.1}', $3::jsonb, false),
                   ($2, '10.9.9.9', 'app-a', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}', '{}', true)
            """,
            ROUTER_ID, APP_ID,
            json.dumps({"7": {"name": "ISP-A", "role": "wan"}, "8": {"name": "ISP-B", "role": "wan"}}),
        )
        await conn.execute(
            """
            INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops)
            VALUES ($1, 3, $2::jsonb)
            """,
            APP_ID,
            json.dumps([
                {"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.0},
                {"hop": 2, "ip": "10.0.0.2", "rtt_ms": 2.0},
                {"hop": 3, "ip": "10.9.9.9", "rtt_ms": 3.0},
            ]),
        )

    run_sql(seed)


def _session_factory(dsn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _flush_flows(dsn, flows, clock_start):
    """Feeds flows through a real FlowAggregator: [(minute_offset, out_if, next_hop, bytes)]."""
    from monitoring.flow.aggregator import FlowAggregator
    from monitoring.flow.path_tracker import PathTracker

    class Correlator:
        def correlate(self, exporter_ip, src_ip, dst_ip):
            return ROUTER_ID, None, APP_ID

    clock = {"now": clock_start}
    published = []

    async def publish(channel, payload):
        published.append((channel, payload))

    engine, factory = _session_factory(dsn)
    try:
        agg = FlowAggregator(
            store_client=None,
            correlator=Correlator(),
            db_session_factory=factory,
            path_tracker=PathTracker(change_minutes=2, clock=lambda: clock["now"]),
            event_publisher=publish,
        )
        for minute, out_if, next_hop, nbytes in flows:
            clock["now"] = clock_start + minute * 60
            ts = clock["now"] - 20
            agg.accumulate_flow(
                exporter_ip="10.0.0.1", src_ip="52.94.0.1", dst_ip="10.9.9.9", src_port=443, dst_port=50000,
                protocol=6, flow_bytes=nbytes, flow_packets=10, start_time_unix=ts - 10,
                in_if=5, out_if=out_if, next_hop=next_hop, end_time_unix=ts,
            )
            await agg.flush_to_timescaledb()
        clock["now"] += 60
        await agg.flush_to_timescaledb()
    finally:
        await engine.dispose()
    return published


def _data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_observations_upsert_and_route_change(api_client, run_sql, timescale_dsn):
    _seed(run_sql)
    start = datetime.datetime.now(datetime.timezone.utc).timestamp() - 600
    flows = [(0, 7, "10.0.0.2", 1000), (0, 7, "10.0.0.2", 500), (1, 8, "10.0.0.6", 1000), (2, 8, "10.0.0.6", 1000), (3, 8, "10.0.0.6", 1000)]
    published = asyncio.run(_flush_flows(timescale_dsn, flows, start))

    rows = run_sql(lambda conn: conn.fetch(
        "SELECT out_if, host(next_hop) AS nh, bytes, flow_count FROM flow_path_observations ORDER BY out_if"
    ))
    # Repeated flushes of the same path add up in one row (expression-index ON CONFLICT).
    assert [(r["out_if"], r["nh"], r["bytes"], r["flow_count"]) for r in rows] == [(7, "10.0.0.2", 1500, 2), (8, "10.0.0.6", 3000, 3)]

    (change,) = run_sql(lambda conn: conn.fetch(
        "SELECT old_out_if, host(old_next_hop) AS old_nh, new_out_if, host(new_next_hop) AS new_nh FROM flow_route_changes"
    ))
    assert (change["old_out_if"], change["old_nh"], change["new_out_if"], change["new_nh"]) == (7, "10.0.0.2", 8, "10.0.0.6")
    assert [ch for ch, _ in published] == ["ROUTE_HINT"]
    assert published[0][1]["endpoint_id"] == str(APP_ID)

    data = _data(api_client.get(f"/api/v1/endpoints/{APP_ID}/flow-paths"))
    assert data["next_hop_reported"] is True
    assert {(p["out_if_name"], p["next_hop"], p["direction"]) for p in data["paths"]} == {
        ("ISP-A", "10.0.0.2", "to"), ("ISP-B", "10.0.0.6", "to")
    }
    (rc,) = data["route_changes"]
    assert (rc["old_out_if_name"], rc["new_out_if_name"], rc["exporter_hostname"]) == ("ISP-A", "ISP-B", "edge-router")


def test_topology_links_confirmed_then_differing(run_sql, timescale_dsn):
    from app.services.topology import TopologyGraphManager

    _seed(run_sql)
    start = datetime.datetime.now(datetime.timezone.utc).timestamp() - 300

    async def refresh():
        engine, factory = _session_factory(timescale_dsn)
        try:
            manager = TopologyGraphManager()
            async with factory() as db:
                await manager.full_rebuild(db)
                await manager.refresh_flow_status(db)
            return {(e["source"], e["target"]): e for e in manager.get_cached_graph()["edges"]}
        finally:
            await engine.dispose()

    asyncio.run(_flush_flows(timescale_dsn, [(0, 7, "10.0.0.2", 1000)], start))
    link = (ROUTER_ID.__str__(), "transit:10.0.0.2")
    edges = asyncio.run(refresh())
    # The exporter is a monitored node, so the hop 10.0.0.1 is its node id.
    assert edges[link]["flow_status"] == "confirmed"
    assert "flow_status" not in edges[("transit:10.0.0.2", str(APP_ID))]

    # Traffic now leaves via 10.0.0.6 only: the traceroute link no longer matches.
    run_sql(lambda conn: conn.execute("DELETE FROM flow_path_observations"))
    asyncio.run(_flush_flows(timescale_dsn, [(0, 8, "10.0.0.6", 1000)], start))
    edges = asyncio.run(refresh())
    assert edges[link]["flow_status"] == "differs"
    assert "10.0.0.6" in edges[link]["flow_detail"]


def test_rca_records_flow_evidence(run_sql, timescale_dsn):
    from app.services import rca_engine

    _seed(run_sql)
    start = datetime.datetime.now(datetime.timezone.utc).timestamp() - 60
    # Traffic seen just now, i.e. after probes started failing.
    asyncio.run(_flush_flows(timescale_dsn, [(0, 7, "10.0.0.2", 1000)], start))

    async def run():
        engine, factory = _session_factory(timescale_dsn)
        failure = {"hops": [{"hop": 1, "ip": "10.0.0.1", "rtt_ms": 1.0}, {"hop": 2, "ip": None}]}
        try:
            async with factory() as db:
                with patch.object(rca_engine, "run_throttled_traceroute", return_value=failure), \
                        patch.object(rca_engine.driver_manager, "get_event_broker", return_value=None):
                    result = await rca_engine.run_differential_rca(APP_ID, db=db)
                await db.commit()
            return result
        finally:
            await engine.dispose()

    result = asyncio.run(run())
    evidence = result["flow_evidence"]
    assert evidence["traffic_after_failure"] is True
    assert evidence["paths"][0]["exporter_hostname"] == "edge-router"
    assert evidence["paths"][0]["out_if_name"] == "ISP-A"

    (row,) = run_sql(lambda conn: conn.fetch("SELECT rca_summary, flow_evidence FROM endpoint_rca_incidents"))
    assert row["rca_summary"]  # the incident itself was recorded
    stored = json.loads(row["flow_evidence"])
    assert stored["paths"][0]["next_hop"] == "10.0.0.2"


def test_rca_without_flow_data_has_no_evidence(run_sql, timescale_dsn):
    from app.services import rca_engine

    _seed(run_sql)

    async def run():
        engine, factory = _session_factory(timescale_dsn)
        try:
            async with factory() as db:
                with patch.object(rca_engine, "run_throttled_traceroute", return_value={"hops": []}), \
                        patch.object(rca_engine.driver_manager, "get_event_broker", return_value=None):
                    result = await rca_engine.run_differential_rca(APP_ID, db=db)
                await db.commit()
            return result
        finally:
            await engine.dispose()

    assert asyncio.run(run())["flow_evidence"] is None
    (row,) = run_sql(lambda conn: conn.fetch("SELECT flow_evidence FROM endpoint_rca_incidents"))
    assert row["flow_evidence"] is None
