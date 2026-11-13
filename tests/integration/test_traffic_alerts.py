"""Traffic alerts on WAN links, on real PostgreSQL (v3.3.6 build plan 7.4).

One exporter with four links: WAN 10 Mbps on the default rule, WAN 10 Mbps with its own 95%,
WAN 10 Mbps opted out, WAN without a speed, and a LAN 10 Mbps link. Minutes are written straight
into flow_interface_minute_rollups and evaluate() runs with an explicit `now`.

Bytes per minute: 9 Mbps = 67,500,000 B (90% of 10 Mbps), 5 Mbps = 37,500,000 B (50%).
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services import traffic_alerts as ta

NOW = datetime(2026, 11, 12, 15, 0, 30, tzinfo=timezone.utc)
# evaluate() reads complete minutes only: up to 14:58 here (now - 2 min, floored).
END = datetime(2026, 11, 12, 14, 58, tzinfo=timezone.utc)
MB90 = 67_500_000
MB50 = 37_500_000

ALIASES = {
    "1": {"name": "WAN1", "speed_mbps": 10, "role": "wan"},
    "2": {"name": "WAN2", "speed_mbps": 10, "role": "wan", "alert": {"mode": "custom", "threshold_pct": 95, "minutes": 10}},
    "3": {"name": "WAN3", "speed_mbps": 10, "role": "wan", "alert": {"mode": "off"}},
    "4": {"name": "WAN-nospeed", "role": "wan"},
    "5": {"name": "LAN", "speed_mbps": 10, "role": "lan"},
}


@pytest.fixture
def exporter(run_sql):
    ep = uuid.uuid4()

    async def seed(conn):
        await conn.execute("TRUNCATE flow_interface_minute_rollups")
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                      monitoring_enabled, flow_interface_aliases)
               VALUES ($1, '10.55.0.1', 'abj-rtr', 'Router', 'ACTIVE', false, $2::jsonb)""",
            ep, json.dumps(ALIASES),
        )

    run_sql(seed)
    return ep


def put_minutes(run_sql, ep, idx, minutes, in_bytes, out_bytes=0):
    async def go(conn):
        await conn.executemany(
            """INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx,
                   in_bytes, out_bytes, in_packets, out_packets, flow_count)
               VALUES ($1, $2, $3, $4, $5, 1, 1, 1)""",
            [(m, ep, idx, in_bytes, out_bytes) for m in minutes],
        )

    run_sql(go)


def mins(first_ago, last_ago, end=END):
    """Minute starts from `first_ago` to `last_ago` minutes before `end` (inclusive)."""
    return [end - timedelta(minutes=i) for i in range(first_ago, last_ago - 1, -1)]


def evaluate(dsn, now=NOW, flows_on=True, pct=80, minutes=15):
    async def go():
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                events = await ta.evaluate(db, default_pct=pct, default_minutes=minutes,
                                           flow_collection_on=flows_on, now=now)
                await db.commit()
                return events
        finally:
            await engine.dispose()

    return asyncio.run(go())


def brief(events):
    return sorted((e.severity, e.link.name, e.direction) for e in events)


def test_only_wan_links_with_a_speed_and_not_opted_out_are_watched(timescale_dsn, exporter):
    async def go():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                return await ta.watched_links(db, 80, 15)
        finally:
            await engine.dispose()

    links = {l.name: (l.threshold_pct, l.minutes) for l in asyncio.run(go()) if l.exporter_id == exporter}
    assert links == {"WAN1": (80, 15), "WAN2": (95, 10)}


def test_high_once_per_spell_then_back_to_normal(timescale_dsn, exporter, run_sql):
    # 15 complete minutes at 90% in, and the still-incomplete minutes too.
    put_minutes(run_sql, exporter, 1, mins(15, 1) + [END, END + timedelta(minutes=1)], MB90)
    first = evaluate(timescale_dsn)
    assert brief(first) == [("TRAFFIC_HIGH", "WAN1", "in")]
    e = first[0]
    assert e.summary == "abj-rtr WAN1: inbound traffic above 80% for 15 min"
    assert e.facts["Peak"] == "90%" and e.link.exporter_ip == "10.55.0.1"

    # Still busy a minute later: nothing new (no reminders).
    put_minutes(run_sql, exporter, 1, [END + timedelta(minutes=2)], MB90)
    assert evaluate(timescale_dsn, now=NOW + timedelta(minutes=1)) == []

    # Five minutes under 70%: back to normal, with how long and the peak.
    later_end = END + timedelta(minutes=8)
    put_minutes(run_sql, exporter, 1, mins(5, 1, later_end), MB50)
    back = evaluate(timescale_dsn, now=NOW + timedelta(minutes=8))
    assert brief(back) == [("TRAFFIC_NORMAL", "WAN1", "in")]
    assert "back to normal" in back[0].summary and "peak 90%" in back[0].summary


def test_one_minute_under_the_threshold_breaks_the_run(timescale_dsn, exporter, run_sql):
    put_minutes(run_sql, exporter, 1, mins(15, 8) + mins(6, 1), MB90)
    put_minutes(run_sql, exporter, 1, [END - timedelta(minutes=7)], MB50)
    assert evaluate(timescale_dsn) == []


def test_a_link_with_its_own_rule_and_outbound(timescale_dsn, exporter, run_sql):
    # WAN2: 90% for 10 min is under its own 95%; WAN1 outbound 90% alerts on its own.
    put_minutes(run_sql, exporter, 2, mins(15, 1), MB90)
    put_minutes(run_sql, exporter, 1, mins(15, 1), 0, MB90)
    assert brief(evaluate(timescale_dsn)) == [("TRAFFIC_HIGH", "WAN1", "out")]


def test_silent_link_and_traffic_again(timescale_dsn, exporter, run_sql):
    # Seen two hours ago, nothing in the last 15 min: silent.
    put_minutes(run_sql, exporter, 1, [END - timedelta(hours=2)], 1000)
    assert brief(evaluate(timescale_dsn)) == [("TRAFFIC_SILENT", "WAN1", None)]
    # Not while flow collection is off, and never for a link never seen (WAN2).
    assert evaluate(timescale_dsn, flows_on=False) == []

    put_minutes(run_sql, exporter, 1, [END + timedelta(minutes=4)], 1000)
    back = evaluate(timescale_dsn, now=NOW + timedelta(minutes=5))
    assert brief(back) == [("TRAFFIC_RESUMED", "WAN1", None)]
    assert "traffic seen again" in back[0].summary


def test_interface_editor_round_trip_keeps_the_alert_rule(api_client, exporter):
    """Saving the alias map with a custom rule, then reading the interface list, returns it."""
    body = {
        "1": {"name": "WAN1", "speed_mbps": 10, "role": "wan", "alert": {"mode": "custom", "threshold_pct": 90, "minutes": 10}},
        "5": {"name": "LAN", "speed_mbps": 10, "role": "lan"},
    }
    res = api_client.put(f"/api/v1/bandwidth/endpoints/{exporter}/interfaces", json=body)
    assert res.status_code == 200, res.text
    stored = res.json()["data"]["flow_interface_aliases"]
    assert stored["1"]["alert"] == {"mode": "custom", "threshold_pct": 90, "minutes": 10}

    bad = api_client.put(f"/api/v1/bandwidth/endpoints/{exporter}/interfaces",
                         json={"1": {"name": "WAN1", "role": "wan", "alert": {"mode": "custom", "threshold_pct": 30}}})
    assert bad.status_code == 422
