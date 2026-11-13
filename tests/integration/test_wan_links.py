"""The Traffic page's "Now" area: GET /bandwidth/wan-links (real TimescaleDB).

v3.3.6 build plan 7.5 step 4 (review P1, O1, O4). One row per WAN link of the routers in scope:
the last five complete minutes against the link's speed, the usual range for this hour, today's
busiest minute, what SNMP says of the port and any open traffic alert. The seeded cases follow
the scratch week the review was made on:

- abj-rtr1 (Abuja): "Glo MPLS", 20 Mb/s typed, at 12.4 Mb/s in: 62 %, with a traffic alert open;
- hq-rtr1 (Lagos HQ), polled over SNMP: "MTN fibre 100M" with its speed from SNMP, and "Airtel
  backup", typed as 20 Mb/s on a port SNMP reports at 1 Gb/s, down since 40 minutes ago;
- ph-rtr1 (Port Harcourt): a WAN link with no speed, so no utilisation.

Also here: the Security tiles' summary follows the picked window (/bandwidth/anomalies), where
the overview counts five minutes.

The usual range and today's peak read the hourly aggregates, so they are tested in a private
database (see test_link_hours: another test refreshes the shared one's aggregate).
"""
import asyncio
import datetime
import json
import os
import uuid

import pytest

UTC = datetime.timezone.utc
MB = 1_000_000
HQ = uuid.UUID("55555555-0000-0000-0000-0000000000d1")
ABJ = uuid.UUID("55555555-0000-0000-0000-0000000000d2")
PH = uuid.UUID("55555555-0000-0000-0000-0000000000d3")


def _minute_bytes(mbps):
    """Bytes in one minute at `mbps` megabits per second."""
    return int(mbps * MB * 60 / 8)


def _data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


ENDPOINT_SQL = """
    INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, monitoring_enabled,
                           device_role, flow_exporter_ips, flow_interface_aliases, site)
    VALUES ($1, $2::inet, $3, 'Router', 'ACTIVE', false, 'FLOW_EXPORTER', ARRAY[$2]::inet[], $4::jsonb, $5)
"""
FLOW_IF_SQL = """
    INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                                               in_packets, out_packets, flow_count)
    VALUES ($1, $2, $3, $4, $5, 1000, 1000, 5)
"""
SNMP_IF_SQL = """
    INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                                               in_packets, out_packets)
    VALUES ($1, $2, $3, $4, $5, 1000, 1000)
"""


def _now_minute():
    return datetime.datetime.now(UTC).replace(second=0, microsecond=0)


def _seed_network(run_sql):
    """The three routers, five minutes of traffic and the SNMP view of HQ's ports. Minutes 0 to 6
    ago carry the same rate, so the five complete minutes before the request hold it even if the
    clock moves to the next minute in between."""
    now = _now_minute()
    minutes = [now - datetime.timedelta(minutes=m) for m in range(7)]
    down_at = now - datetime.timedelta(minutes=40)

    async def seed(conn):
        await conn.execute(ENDPOINT_SQL, ABJ, "10.1.0.1", "abj-rtr1",
                           json.dumps({"1": {"name": "Glo MPLS", "speed_mbps": 20, "role": "wan"}}), "Abuja")
        # HQ: the fibre's name and speed come from SNMP; the backup's are typed.
        await conn.execute(ENDPOINT_SQL, HQ, "10.0.0.1", "hq-rtr1", json.dumps({
            "1": {"role": "wan"},
            "2": {"name": "Airtel backup", "speed_mbps": 20, "role": "wan"},
            "3": {"name": "LAN", "speed_mbps": 1000, "role": "lan"},
        }), "Lagos HQ")
        await conn.execute(ENDPOINT_SQL, PH, "10.3.0.1", "ph-rtr1",
                           json.dumps({"1": {"name": "WAN", "role": "wan"}}), "Port Harcourt")
        await conn.execute(
            "INSERT INTO snmp_device_state (endpoint_id, status, last_ok_at) VALUES ($1, 'ok', $2)", HQ, now)
        await conn.executemany(
            """INSERT INTO snmp_interfaces (endpoint_id, if_index, if_name, if_alias, speed_mbps, admin_status, oper_status)
               VALUES ($1, $2, $3, $4, $5, 'up', $6)""",
            [(HQ, 1, "Gi0/0", "MTN fibre 100M", 100, "up"), (HQ, 2, "Gi0/1", "", 1000, "down")],
        )
        # The backup went down, came back, and went down again 40 minutes ago: that is "since".
        await conn.executemany(
            "INSERT INTO snmp_interface_events (endpoint_id, if_index, if_name, at, kind) VALUES ($1, 2, 'Gi0/1', $2, $3)",
            [(HQ, now - datetime.timedelta(hours=3), "down"), (HQ, now - datetime.timedelta(hours=2), "up"), (HQ, down_at, "down")],
        )
        await conn.execute(
            """INSERT INTO traffic_alert_state (exporter_id, interface_idx, kind, active, since, peak_pct)
               VALUES ($1, 1, 'high_in', true, $2, 71.5), ($1, 1, 'silent', false, NULL, NULL)""",
            ABJ, now - datetime.timedelta(minutes=12),
        )
        await conn.executemany(FLOW_IF_SQL, [(m, ABJ, 1, _minute_bytes(12.4), _minute_bytes(2)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, PH, 1, _minute_bytes(7), _minute_bytes(1)) for m in minutes])
        # HQ's fibre has counters (and flows, which the counters replace).
        await conn.executemany(SNMP_IF_SQL, [(m, HQ, 1, _minute_bytes(40), _minute_bytes(9)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, HQ, 1, _minute_bytes(30), _minute_bytes(7)) for m in minutes])

    run_sql(seed)
    return now, down_at


def _links(api_client, **params):
    return _data(api_client.get("/api/v1/bandwidth/wan-links", params=params))["links"]


def test_each_wan_link_against_its_capacity(api_client, run_sql):
    _, down_at = _seed_network(run_sql)
    links = _links(api_client)

    # Grouped by site in name order; within a site the busiest link first. LAN ports are not here.
    assert [(l["site"], l["name"]) for l in links] == [
        ("Abuja", "Glo MPLS"),
        ("Lagos HQ", "MTN fibre 100M"),
        ("Lagos HQ", "Airtel backup"),
        ("Port Harcourt", "WAN"),
    ]
    abj, fibre, backup, ph = links

    # Abuja at 12.4 of 20 Mb/s, from flows, with its traffic alert open.
    assert (abj["in_bps"], abj["out_bps"]) == (pytest.approx(12.4 * MB), pytest.approx(2 * MB))
    assert abj["utilization_percentage"] == pytest.approx(62.0)
    assert (abj["speed_mbps"], abj["speed_source"], abj["traffic_source"]) == (20, "manual", "flow")
    assert abj["link_state"] is None  # not polled over SNMP
    assert [(a["kind"], a["direction"], a["peak_pct"]) for a in abj["alerts"]] == [("high", "in", 71.5)]

    # HQ's fibre: name and speed from SNMP; its counters, not its flows, give "now".
    assert (fibre["speed_mbps"], fibre["speed_source"]) == (100, "snmp")
    assert (fibre["in_bps"], fibre["traffic_source"]) == (pytest.approx(40 * MB), "snmp")
    assert fibre["utilization_percentage"] == pytest.approx(40.0)
    assert (fibre["link_state"], fibre["down_since"]) == ("up", None)

    # The backup: the typed 20 Mb/s wins over the port's 1 Gb/s, which is still reported; down
    # since the last down event, not the first.
    assert (backup["speed_mbps"], backup["speed_source"], backup["snmp_speed_mbps"]) == (20, "manual", 1000)
    assert (backup["in_bps"], backup["utilization_percentage"]) == (0, 0)
    assert backup["link_state"] == "down"
    assert datetime.datetime.fromisoformat(backup["down_since"]) == down_at

    # Port Harcourt: no speed, so traffic but no utilisation (the page offers "Set speed").
    assert (ph["speed_mbps"], ph["speed_source"], ph["utilization_percentage"]) == (None, "default", None)
    assert ph["in_bps"] == pytest.approx(7 * MB)


def test_the_filter_and_device_pick_the_links(api_client, run_sql):
    _seed_network(run_sql)
    assert [l["name"] for l in _links(api_client, site="Abuja")] == ["Glo MPLS"]
    assert [l["name"] for l in _links(api_client, device_id=str(HQ))] == ["MTN fibre 100M", "Airtel backup"]
    # The old parameter name still works.
    assert [l["name"] for l in _links(api_client, exporter_id=str(PH))] == ["WAN"]
    # A device outside the filter shows nothing, as on the rest of the page.
    assert _links(api_client, site="Abuja", device_id=str(PH)) == []
    assert _links(api_client, tag="no-such-tag") == []


def test_security_tiles_follow_the_window(api_client, run_sql):
    """A scan 3 minutes ago and another 30 minutes ago: the overview's five minutes see one, the
    Security tiles over the last hour see both (review P1)."""
    now = _now_minute()

    async def seed(conn):
        await conn.execute(ENDPOINT_SQL, HQ, "10.0.0.1", "hq-rtr1", json.dumps({}), "Lagos HQ")
        await conn.executemany(
            """INSERT INTO flow_minute_rollups (bucket, exporter_id, src_endpoint_id, dst_endpoint_id, src_ip, dst_ip,
                   protocol, dst_port, bytes, packets, flow_count, tcp_flags, tos)
               VALUES ($1, $2, NULL, NULL, $3::inet, '10.0.0.1', 6, 23, 600, 10, $4, $5, 0)""",
            [
                (now - datetime.timedelta(minutes=3), HQ, "203.0.113.9", 10, 2),   # SYN only: a scan
                (now - datetime.timedelta(minutes=30), HQ, "203.0.113.7", 6, 2),   # an earlier scan
                (now - datetime.timedelta(minutes=30), HQ, "198.51.100.4", 4, 24),  # ordinary traffic
            ],
        )

    run_sql(seed)
    assert _data(api_client.get("/api/v1/bandwidth/overview"))["anomaly_conversations_count"] == 1
    hour = _data(api_client.get("/api/v1/bandwidth/anomalies", params={"window": "1h"}))
    assert (hour["conversations"], hour["flows"], hour["all_flows"]) == (2, 16, 20)
    assert (hour["syn_conversations"], hour["rst_conversations"]) == (2, 0)
    # Scoped like the table: a filter matching no router finds none.
    assert _data(api_client.get("/api/v1/bandwidth/anomalies", params={"window": "1h", "site": "Abuja"}))["conversations"] == 0
    # A window the conversation tables don't keep is refused, as for the table.
    assert api_client.get("/api/v1/bandwidth/anomalies", params={"window": "1y"}).status_code == 400


# --- The usual range and today's peak (private database) -----------------------------------------

@pytest.fixture(scope="module")
def private_dsn():
    from tests.integration.conftest import DSN_ENV, _create_and_migrate, _drop

    admin_dsn = os.environ.get(DSN_ENV)
    if not admin_dsn:
        pytest.skip(f"{DSN_ENV} not set")
    name = f"ephemres_it_{uuid.uuid4().hex[:8]}"
    dsn = asyncio.run(_create_and_migrate(admin_dsn, name))
    try:
        yield dsn
    finally:
        asyncio.run(_drop(admin_dsn, name))


def _private(dsn, seed, read):
    import asyncpg
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    from tests.integration.conftest import reset_interface_traffic, settle_interface_hourly

    async def go():
        conn = await asyncpg.connect(dsn)
        try:
            await reset_interface_traffic(conn)
            await seed(conn)
            # The private database's refresh policy may have moved the watermark: settle it.
            await settle_interface_hourly(conn)
        finally:
            await conn.close()
        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)() as db:
                return await read(db)
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_usual_range_and_todays_peak(private_dsn):
    """Yesterday at 12:30 UTC, Abuja's link at 15 Mb/s against a usual 8-12 Mb/s at that hour on
    the three days before of the same kind (weekday or weekend): 25 % above usual. Its busiest
    minute today was a 19 Mb/s burst at 09:xx; a 50 Mb/s minute at 23:10 the night before is not
    today's in UTC, but is in Lagos (UTC+1), where that minute was 00:10."""
    from zoneinfo import ZoneInfo

    from app.services.traffic_usual import Band
    from app.services.wan_links import wan_links_now

    day = (datetime.datetime.now(UTC) - datetime.timedelta(days=1)).date()
    now = datetime.datetime(day.year, day.month, day.day, 12, 30, tzinfo=UTC)
    weekend = day.weekday() >= 5
    past, d = [], day
    while len(past) < 3:
        d -= datetime.timedelta(days=1)
        if (d.weekday() >= 5) == weekend:
            past.append(d)
    at = lambda d, h, m=0: datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)

    async def seed(conn):
        await conn.execute(ENDPOINT_SQL, ABJ, "10.1.0.1", "abj-rtr1",
                           json.dumps({"1": {"name": "Glo MPLS", "speed_mbps": 20, "role": "wan"}}), "Abuja")
        rows = []
        for d, rate in zip(past, (8, 10, 12)):
            rows += [(at(d, 12, m), ABJ, 1, _minute_bytes(rate), 0) for m in range(60)]
        rows += [(at(day, 9, m), ABJ, 1, _minute_bytes(19 if m == 17 else 5), 0) for m in range(60)]
        rows += [(at(day, 12, m), ABJ, 1, _minute_bytes(15), 0) for m in range(20, 30)]
        rows.append((at(day - datetime.timedelta(days=1), 23, 10), ABJ, 1, _minute_bytes(50), 0))
        await conn.executemany(FLOW_IF_SQL, rows)

    async def read(db):
        return await wan_links_now(db, None, now=now, tz=UTC), await wan_links_now(db, None, now=now, tz=ZoneInfo("Africa/Lagos"))

    utc, lagos = _private(private_dsn, seed, read)
    (link,) = utc.links
    assert link.utilization_pct == pytest.approx(75.0)
    assert link.usual_in == Band(8 * MB, 12 * MB, 3)
    assert link.above_usual_in_pct == pytest.approx(25.0)
    assert link.usual_out == Band(0.0, 0.0, 3) and link.above_usual_out_pct is None
    assert (link.peak_in_bps, link.peak_in_hour) == (pytest.approx(19 * MB), at(day, 9))
    assert utc.day_start == at(day, 0) and utc.tz_name == "UTC"

    (local,) = lagos.links
    assert local.peak_in_bps == pytest.approx(50 * MB)
    assert lagos.day_start == at(day - datetime.timedelta(days=1), 23)


# --- One chart per link (step 5) ------------------------------------------------------------------

def test_charts_come_with_the_links_when_asked(api_client, run_sql):
    """?window=1h&series=1: every link carries its minutes, aligned column by column, plus the
    spans SNMP saw the port down: the backup's 40 minutes so far, still open. Without series=1
    the rows have none."""
    now, down_at = _seed_network(run_sql)
    assert all(l["series"] is None for l in _links(api_client))

    data = _data(api_client.get("/api/v1/bandwidth/wan-links", params={"window": "1h", "series": 1}))
    assert data["window"] == "1h"
    by_name = {l["name"]: l["series"] for l in data["links"]}
    abj = by_name["Glo MPLS"]
    assert abj["bucket_seconds"] == 60 and len(abj["times"]) == 60
    columns = ("in_bps", "out_bps", "peak_in_bps", "usual_in_low", "usual_out_high")
    assert {len(abj[c]) for c in columns} == {60}
    # The last complete minute is the newest; minutes without rows are gaps, not zeros.
    last = datetime.datetime.fromisoformat(abj["times"][-1])
    assert now - datetime.timedelta(minutes=1) <= last <= now
    assert abj["in_bps"][-1] == pytest.approx(12.4 * MB, rel=1e-6) and abj["in_bps"][0] is None
    assert set(abj["peak_in_bps"]) == {None}  # minutes are their own peak
    assert abj["window_peak_percentage"] == pytest.approx(62.0)
    # The fibre from its counters, not its flows.
    assert by_name["MTN fibre 100M"]["in_bps"][-1] == pytest.approx(40 * MB, rel=1e-6)
    # Down 3 h ago and up 2 h ago: outside the hour. Down again 40 minutes ago: open.
    assert [(datetime.datetime.fromisoformat(s["start"]), s["end"]) for s in by_name["Airtel backup"]["down_spans"]] == [(down_at, None)]
    assert by_name["Glo MPLS"]["down_spans"] == []
    assert by_name["WAN"]["window_peak_percentage"] is None  # no speed: ranked by bits/s
    assert api_client.get("/api/v1/bandwidth/wan-links", params={"window": "2h", "series": 1}).status_code == 400


def test_hourly_charts_with_peaks_usual_range_and_outages(private_dsn):
    """A 24h chart at 12:30 UTC: hourly averages with each hour's busiest minute, the usual range
    of each hour (from the three days before), and an outage from before the window into it
    starting at the window's start, while one inside it is drawn from down to up."""
    from app.services.wan_links import wan_links_now

    day = (datetime.datetime.now(UTC) - datetime.timedelta(days=1)).date()
    now = datetime.datetime(day.year, day.month, day.day, 12, 30, tzinfo=UTC)
    at = lambda d, h, m=0: datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)
    weekend = day.weekday() >= 5
    past, d = [], day
    while len(past) < 3:
        d -= datetime.timedelta(days=1)
        if (d.weekday() >= 5) == weekend:
            past.append(d)
    window_start = at(day, 12) - datetime.timedelta(hours=24)

    async def seed(conn):
        await conn.execute(ENDPOINT_SQL, HQ, "10.0.0.1", "hq-rtr1",
                           json.dumps({"2": {"name": "Airtel backup", "speed_mbps": 20, "role": "wan"}}), "Lagos HQ")
        rows = []
        for d, rate in zip(past, (4, 6, 8)):
            rows += [(at(d, 9, m), HQ, 2, _minute_bytes(rate), 0) for m in range(60)]
        # Today 09:00: 5 Mb/s with one 18 Mb/s minute.
        rows += [(at(day, 9, m), HQ, 2, _minute_bytes(18 if m == 30 else 5), 0) for m in range(60)]
        await conn.executemany(FLOW_IF_SQL, rows)
        await conn.executemany(
            "INSERT INTO snmp_interface_events (endpoint_id, if_index, at, kind) VALUES ($1, 2, $2, $3)",
            [
                (HQ, window_start - datetime.timedelta(hours=2), "down"),  # down before the window...
                (HQ, window_start + datetime.timedelta(hours=1), "up"),     # ...up an hour into it
                (HQ, at(day, 10, 0), "down"),
                (HQ, at(day, 10, 40), "up"),
            ],
        )

    async def read(db):
        return await wan_links_now(db, None, now=now, tz=UTC, window=datetime.timedelta(hours=24))

    (link,) = _private(private_dsn, seed, read).links
    s = link.series
    assert s.bucket_seconds == 3600 and len(s.times) == 24
    assert (s.times[0], s.times[-1]) == (window_start, at(day, 11))
    nine = s.times.index(at(day, 9))
    assert s.in_bps[nine] == pytest.approx((59 * 5 + 18) / 60 * MB)
    assert s.peak_in_bps[nine] == pytest.approx(18 * MB)
    assert (s.usual_in_low[nine], s.usual_in_high[nine]) == (4 * MB, 8 * MB)
    assert s.in_bps[nine + 1] is None  # no rows at 10:00: a gap
    assert s.down_spans == [(window_start, window_start + datetime.timedelta(hours=1)), (at(day, 10), at(day, 10, 40))]
    assert s.window_peak_pct == pytest.approx(90.0)  # the 18 Mb/s minute on a 20 Mb/s link
