"""SNMP poller step 1 on real PostgreSQL (v3.3.6 build plan 8.3): credential sets through the
API, and ephemres-snmpd's DevicePoller against the local fake agent: probe, inventory, status
events, reboot, renumbered interfaces carrying their manual names, failures.
"""
import asyncio
import json
import threading
import uuid

import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from monitoring.snmp import ber, mib
from monitoring.snmp.poller import DevicePoller, SnmpPoller, Store, renumbered, remap_aliases

from tests.backend.snmp_fake_agent import FakeAgent, start_agent
from tests.backend.test_snmp_core import interface_mib

IF_TABLE_LAST_CHANGE = ber.parse_oid("1.3.6.1.2.1.31.1.5.0")


def device_mib(count=3):
    m = interface_mib(count)
    m[IF_TABLE_LAST_CHANGE] = (ber.TIMETICKS, 100)
    return m


def factory(dsn):
    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
    return engine, async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


def add_endpoint(run_sql, cred_id, aliases=None, ip="127.0.0.1"):
    ep = uuid.uuid4()

    async def go(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, monitoring_enabled,
                                      flow_interface_aliases, snmp_credential_id)
               VALUES ($1, $4::inet, 'abj-rtr-' || host($4::inet), 'Router', 'ACTIVE', false, $2::jsonb, $3)""",
            ep, json.dumps(aliases or {}), cred_id, ip)

    run_sql(go)
    return ep


def make_set(api_client, port, **extra):
    body = {"name": f"lab-{uuid.uuid4().hex[:6]}", "version": "2c", "community": "public", "port": port,
            "timeout_ms": 500, "retries": 0, **extra}
    res = api_client.post("/api/v1/snmp/credentials", json=body)
    assert res.status_code == 200, res.text
    return res.json()["data"]


def rows(run_sql, sql, *args):
    async def go(conn):
        return [dict(r) for r in await conn.fetch(sql, *args)]
    return run_sql(go)


def test_credential_sets_are_masked_validated_and_assignable(api_client, run_sql):
    data = make_set(api_client, 161, name="Branch v2")
    assert data["has_community"] is True and "community" not in data and data["level"] == "v2c"
    stored = rows(run_sql, "SELECT secrets FROM snmp_credentials")[0]["secrets"]
    assert stored.startswith("ENC:v1:") and "public" not in stored

    v3 = api_client.post("/api/v1/snmp/credentials", json={
        "name": "Meraki", "version": "3", "username": "eph", "auth_protocol": "sha", "priv_protocol": "aes",
        "priv_same": True, "auth_password": "onepassword1"})
    assert v3.status_code == 200, v3.text
    assert v3.json()["data"]["has_priv_password"] is False and v3.json()["data"]["level"] == "authPriv"

    assert api_client.post("/api/v1/snmp/credentials", json={"name": "Branch v2", "version": "2c", "community": "x"}).status_code == 409
    bad = api_client.post("/api/v1/snmp/credentials", json={"name": "Short", "version": "3", "username": "u",
                                                           "auth_protocol": "sha", "auth_password": "short"})
    assert bad.status_code == 422 and "8 characters" in bad.text
    assert api_client.post("/api/v1/snmp/credentials", json={"name": "Bad priv", "version": "3", "username": "u",
                                                            "priv_protocol": "aes"}).status_code == 422

    # Update keeps a secret that is not sent; renaming works.
    upd = api_client.patch(f"/api/v1/snmp/credentials/{data['id']}", json={"name": "Branch v2c", "port": 1161})
    assert upd.status_code == 200 and upd.json()["data"]["has_community"] and upd.json()["data"]["port"] == 1161

    # Assign by endpoint PATCH and in bulk, then delete the set: SNMP off for its endpoints.
    ep1 = add_endpoint(run_sql, None)
    ep2 = add_endpoint(run_sql, None, ip="127.0.0.2")
    assert api_client.patch(f"/api/v1/endpoints/{ep1}", json={"snmp_credential_id": data["id"]}).status_code == 200
    assert api_client.patch(f"/api/v1/endpoints/{ep1}", json={"snmp_credential_id": str(uuid.uuid4())}).status_code == 400
    bulk = api_client.post("/api/v1/endpoints/bulk-update", json={"endpoint_ids": [str(ep2)], "snmp_credential_id": data["id"]})
    assert bulk.status_code == 200, bulk.text
    listed = api_client.get("/api/v1/snmp/credentials").json()["data"]
    assert {d["name"]: d["endpoints"] for d in listed} == {"Branch v2c": 2, "Meraki": 0}
    gone = api_client.delete(f"/api/v1/snmp/credentials/{data['id']}")
    assert gone.json()["data"]["endpoints_turned_off"] == 2
    assert rows(run_sql, "SELECT COUNT(*) AS n FROM endpoints WHERE snmp_credential_id IS NOT NULL")[0]["n"] == 0
    audit = [r["action"] for r in rows(run_sql, "SELECT action FROM audit_logs WHERE action LIKE 'SNMP%' ORDER BY created_at")]
    assert audit[:2] == ["SNMP_CREDENTIALS:CREATE", "SNMP_CREDENTIALS:CREATE"] and audit[-1] == "SNMP_CREDENTIALS:DELETE"
    details = json.dumps([r["details"] for r in rows(run_sql, "SELECT details FROM audit_logs")])
    assert "onepassword1" not in details and "public" not in details


def test_probe_inventory_status_reboot_and_renumbering(api_client, run_sql, timescale_dsn):
    async def go():
        agent = await start_agent(mib=device_mib(3))
        port = agent.port
        return agent, port

    # The agent must live in a loop that keeps running while the API (its own loop) and the
    # poller run, so it gets a thread of its own.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    agent, port = asyncio.run_coroutine_threadsafe(go(), loop).result(5)
    try:
        cred = make_set(api_client, port)
        ep = add_endpoint(run_sql, cred["id"], aliases={"2": {"name": "WAN-Fiber", "speed_mbps": 50, "role": "wan"}})

        async def poll(times=1, mutate=None):
            engine, sf = factory(timescale_dsn)
            try:
                store = Store(sf)
                target = [t for t in await store.load_targets() if t.endpoint_id == str(ep)][0]
                d = DevicePoller(target, store)
                out = []
                for _ in range(times):
                    out.append(await d.run_once())
                d.close()
                return out
            finally:
                await engine.dispose()

        # First contact: a probe; profile, interfaces and state stored.
        assert asyncio.run(poll()) == ["ok"]
        st = rows(run_sql, "SELECT status, profile, sys_uptime FROM snmp_device_state")[0]
        assert st["status"] == "ok" and json.loads(st["profile"])["vendor"] == "Cisco" and st["sys_uptime"] == 8_640_000
        ifs = rows(run_sql, "SELECT if_index, if_name, speed_mbps, oper_status, hc_counters FROM snmp_interfaces ORDER BY if_index")
        assert [(r["if_index"], r["if_name"], r["speed_mbps"], r["oper_status"]) for r in ifs] == \
            [(1, "Gi0/1", 1000, "up"), (2, "Gi0/2", 1000, "up"), (3, "Gi0/3", 1000, "down")]

        # A link goes down: a status poll records it.
        agent.mib[mib.IF_TABLE["ifOperStatus"] + (2,)] = (ber.INTEGER, 2)
        assert asyncio.run(poll()) == ["ok"]
        ev = rows(run_sql, "SELECT kind, if_index, if_name FROM snmp_interface_events ORDER BY id")
        assert ev == [{"kind": "down", "if_index": 2, "if_name": "Gi0/2"}]
        assert rows(run_sql, "SELECT oper_status FROM snmp_interfaces WHERE if_index = 2")[0]["oper_status"] == "down"

        # The device renumbers Gi0/2 to 12 and says so (ifTableLastChange): the manual entry
        # follows its interface.
        for name, col in {**mib.IF_TABLE, **mib.IF_X_TABLE}.items():
            if col + (2,) in agent.mib:
                agent.mib[col + (12,)] = agent.mib.pop(col + (2,))
        agent.mib[IF_TABLE_LAST_CHANGE] = (ber.TIMETICKS, 200)
        agent.order = sorted(agent.mib)
        assert asyncio.run(poll()) == ["ok"]
        aliases = rows(run_sql, "SELECT flow_interface_aliases AS a FROM endpoints WHERE id = $1", ep)[0]["a"]
        assert json.loads(aliases) == {"12": {"name": "WAN-Fiber", "speed_mbps": 50, "role": "wan"}}
        ev = [r["kind"] for r in rows(run_sql, "SELECT kind FROM snmp_interface_events ORDER BY id")]
        assert ev == ["down", "renumbered"]

        # A reboot (uptime goes back): recorded, and the device is probed again.
        agent.mib[mib.SYSTEM["sysUpTime"]] = (ber.TIMETICKS, 500)
        agent.order = sorted(agent.mib)
        assert asyncio.run(poll()) == ["ok"]
        assert rows(run_sql, "SELECT last_reboot_at IS NOT NULL AS r, sys_uptime FROM snmp_device_state")[0] == {"r": True, "sys_uptime": 500}
        assert "reboot" in [r["kind"] for r in rows(run_sql, "SELECT kind FROM snmp_interface_events")]

        # What the API shows for the endpoint.
        view = api_client.get(f"/api/v1/snmp/endpoints/{ep}").json()["data"]
        assert view["credential_name"] == cred["name"] and view["state"]["status"] == "ok"
        assert [i["if_index"] for i in view["interfaces"]] == [1, 3, 12] and view["events"][0]["kind"] == "reboot"
        assert api_client.post(f"/api/v1/snmp/endpoints/{ep}/probe").json()["data"]["probe_requested"] is True

        # Test through the API with the stored set and with a wrong community.
        ok = api_client.post("/api/v1/snmp/test", json={"address": "127.0.0.1", "credential_id": cred["id"]}).json()["data"]
        assert ok["ok"] and ok["interfaces"] == 3 and "64-bit counters on 3 of 3" in ok["text"]
        bad = api_client.post("/api/v1/snmp/test", json={"address": "127.0.0.1", "credential": {
            "version": "2c", "community": "wrong", "port": port, "timeout_ms": 500, "retries": 0}}).json()["data"]
        assert bad["ok"] is False and "no reply" in bad["error"]
    finally:
        loop.call_soon_threadsafe(agent.transport.close)
        loop.call_soon_threadsafe(loop.stop)


def test_failures_are_reported_and_the_scheduler_spreads_devices(api_client, run_sql, timescale_dsn):
    cred = make_set(api_client, 9, timeout_ms=500)  # port 9 (discard): nothing answers
    v3 = api_client.post("/api/v1/snmp/credentials", json={
        "name": "v3-wrong", "version": "3", "username": "eph", "auth_protocol": "sha", "auth_password": "wrongpassword",
        "port": 9, "timeout_ms": 500, "retries": 0}).json()["data"]
    ep1 = add_endpoint(run_sql, cred["id"])

    async def go():
        engine, sf = factory(timescale_dsn)
        try:
            state = {"on": True}
            poller = SnmpPoller(Store(sf), enabled=lambda: state["on"], interval=lambda: 60.0)
            await poller.reload()
            assert list(poller.devices) == [str(ep1)]
            return await poller.devices[str(ep1)].run_once()
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == "no_reply"
    st = rows(run_sql, "SELECT status, last_error FROM snmp_device_state")[0]
    assert st["status"] == "no_reply" and "no reply" in st["last_error"]
    assert v3["level"] == "authNoPriv"


def test_renumbering_rules():
    assert renumbered({1: "Gi0/1", 2: "Gi0/2"}, {1: "Gi0/1", 5: "Gi0/2"}) == [(2, 5, "Gi0/2")]
    # Two ports with the same name are never matched.
    assert renumbered({1: "Port", 2: "Port"}, {3: "Port", 4: "Port"}) == []
    # A swap moves both entries; a move onto an entry that stays is skipped.
    assert remap_aliases({"1": "a", "2": "b"}, [(1, 2, "x"), (2, 1, "y")])[0] == {"2": "a", "1": "b"}
    assert remap_aliases({"1": "a", "2": "b"}, [(1, 2, "x")]) == ({"1": "a", "2": "b"}, [])


def test_snmp_names_and_speeds_fill_what_was_not_typed(api_client, run_sql, timescale_dsn):
    """Manual wins; SNMP fills the rest; the editors get SNMP's values apart from the manual ones."""
    from app.services import snmp_inventory, traffic_alerts

    ep = add_endpoint(run_sql, None, aliases={
        "1": {"name": "WAN-Fiber", "speed_mbps": 50, "role": "wan"},  # all typed
        "2": {"role": "wan"},  # WAN, nothing else typed: SNMP gives name and speed
    })

    async def seed(conn):
        await conn.executemany(
            """INSERT INTO snmp_interfaces (endpoint_id, if_index, if_name, if_alias, if_descr, speed_mbps, oper_status)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            [(ep, 1, "Gi0/1", "ISP-A circuit", "GigabitEthernet0/1", 1000, "up"),
             (ep, 2, "Gi0/2", None, "GigabitEthernet0/2", 100, "up"),
             (ep, 3, "Gi0/3", None, "GigabitEthernet0/3", 1000, "down")])

    run_sql(seed)
    items = {i["interface_idx"]: i for i in
             api_client.get(f"/api/v1/bandwidth/interfaces?exporter_id={ep}&window=1h").json()["data"]["interfaces"]}
    assert sorted(items) == [1, 2]  # 3 is down and has no manual entry and no traffic
    one, two = items[1], items[2]
    assert (one["name"], one["speed_mbps"], one["name_source"], one["speed_source"]) == ("WAN-Fiber", 50, "manual", "manual")
    assert one["snmp_name"] == "ISP-A circuit" and one["snmp_speed_mbps"] == 1000
    assert (two["name"], two["speed_mbps"], two["name_source"], two["speed_source"]) == ("Gi0/2", 100, "snmp", "snmp")
    assert two["speed_configured"] is True and two["role"] == "wan" and two["oper_status"] == "up"

    async def watched():
        engine, sf = factory(timescale_dsn)
        try:
            async with sf() as db:
                return await traffic_alerts.watched_links(db, 80, 15)
        finally:
            await engine.dispose()

    links = {l.interface_idx: (l.name, l.speed_mbps) for l in asyncio.run(watched())}
    assert links == {1: ("WAN-Fiber", 50), 2: ("Gi0/2", 100)}  # 2 is watched thanks to SNMP's speed

    assert snmp_inventory.merge({"5": "Legacy name"}, {5: {"name": "Gi0/5", "speed_mbps": 10}}) == \
        {"5": {"name": "Legacy name", "speed_mbps": 10}}


# --- step 2: interface traffic from counters ------------------------------------------------------

def test_counters_become_minutes_and_carry_on_after_a_restart(api_client, run_sql, timescale_dsn):
    async def go():
        return await start_agent(mib=device_mib(3))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    agent = asyncio.run_coroutine_threadsafe(go(), loop).result(5)
    in_oct = mib.IF_X_TABLE["ifHCInOctets"] + (1,)
    uptime = mib.SYSTEM["sysUpTime"]
    try:
        cred = make_set(api_client, agent.port)
        ep = add_endpoint(run_sql, cred["id"])

        async def turns(n):
            engine, sf = factory(timescale_dsn)
            try:
                store = Store(sf)
                target = [t for t in await store.load_targets() if t.endpoint_id == str(ep)][0]
                d = DevicePoller(target, store)  # a fresh poller each call: a restarted snmpd
                out = [await d.run_once() for _ in range(n)]
                d.close()
                return out
            finally:
                await engine.dispose()

        def advance(octets, seconds=60):
            agent.mib[in_oct] = (ber.COUNTER64, agent.mib[in_oct][1] + octets)
            agent.mib[uptime] = (ber.TIMETICKS, agent.mib[uptime][1] + seconds * 100)

        assert asyncio.run(turns(2)) == ["ok", "ok"]  # probe, then the first readings
        advance(7_500_000)
        assert asyncio.run(turns(1)) == ["ok"]  # after a "restart": the saved readings still count
        total = lambda: rows(run_sql, "SELECT COALESCE(SUM(in_bytes), 0) AS b, COUNT(*) AS n FROM snmp_interface_minute_rollups "
                                       "WHERE interface_idx = 1")[0]
        assert total()["b"] == 7_500_000
        advance(1_000_000)
        assert asyncio.run(turns(1)) == ["ok"]
        assert total()["b"] == 8_500_000
        # A counter going back (the device reset it) adds nothing.
        agent.mib[in_oct] = (ber.COUNTER64, 10)
        agent.mib[uptime] = (ber.TIMETICKS, agent.mib[uptime][1] + 6000)
        assert asyncio.run(turns(1)) == ["ok"]
        assert total()["b"] == 8_500_000
        # Idle interfaces cost no rows.
        assert rows(run_sql, "SELECT COUNT(*) AS n FROM snmp_interface_minute_rollups WHERE interface_idx <> 1")[0]["n"] == 0
    finally:
        loop.call_soon_threadsafe(agent.transport.close)
        loop.call_soon_threadsafe(loop.stop)


def test_snmp_minutes_win_over_flow_minutes_everywhere(api_client, run_sql, timescale_dsn):
    from datetime import datetime, timedelta, timezone
    from app.services import report_compiler, wan_edge

    ep = add_endpoint(run_sql, None, aliases={"1": {"name": "WAN-1", "speed_mbps": 100, "role": "wan"}})
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    minutes = [now - timedelta(minutes=m) for m in (20, 19, 18, 17)]
    flow = {m: 1000 for m in minutes}
    snmp = {minutes[2]: 9000, minutes[3]: 9000}

    async def seed(conn):
        await conn.executemany(
            """INSERT INTO flow_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                   in_packets, out_packets, flow_count) VALUES ($1, $2, 1, $3, 0, 1, 0, 5)""",
            [(m, ep, b) for m, b in flow.items()])
        await conn.executemany(
            """INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                   in_packets, out_packets) VALUES ($1, $2, 1, $3, 0, 2, 0)""",
            [(m, ep, b) for m, b in snmp.items()])

    run_sql(seed)
    expected = 1000 + 1000 + 9000 + 9000

    item = api_client.get(f"/api/v1/bandwidth/interfaces?exporter_id={ep}&window=1h").json()["data"]["interfaces"][0]
    assert (item["in_bytes"], item["flow_count"], item["traffic_source"]) == (expected, 20, "snmp")

    series = api_client.get(f"/api/v1/bandwidth/traffic-series?exporter_id={ep}&window=1h").json()["data"]["points"]
    assert round(sum(p["ingress_bps"] for p in series) * 60 / 8) == expected

    # The hourly tier (windows over 7 days) takes an hour from SNMP when it has any SNMP minute.
    hours = {}
    for m in minutes:
        h = m.replace(minute=0)
        hours.setdefault(h, {"snmp": 0, "flow": 0, "has_snmp": False})
        hours[h]["flow"] += flow[m]
        if m in snmp:
            hours[h]["snmp"] += snmp[m]
            hours[h]["has_snmp"] = True
    hourly_expected = sum(v["snmp"] if v["has_snmp"] else v["flow"] for v in hours.values())
    item30 = api_client.get(f"/api/v1/bandwidth/interfaces?exporter_id={ep}&window=30d").json()["data"]["interfaces"][0]
    assert item30["in_bytes"] == hourly_expected and item30["traffic_source"] == "snmp"

    async def services():
        engine, sf = factory(timescale_dsn)
        try:
            async with sf() as db:
                wans = await wan_edge.list_wan_interfaces(db)
                totals = await wan_edge.wan_link_totals(db, wans, now - timedelta(hours=1), now)
                pdf = await report_compiler._interface_totals(db, [ep], now - timedelta(hours=1), now)
                return totals, pdf
        finally:
            await engine.dispose()

    totals, pdf = asyncio.run(services())
    assert totals[(ep, 1)]["in_bytes"] == expected
    assert [(int(r[2]), int(r[4])) for r in pdf] == [(expected, 9000)]


def test_a_traffic_alert_from_snmp_counters_alone(run_sql, timescale_dsn):
    """A WAN link that exports no flows at all still alerts when its counters say it is busy."""
    from datetime import datetime, timedelta, timezone
    from app.services import traffic_alerts as ta

    ep = add_endpoint(run_sql, None, aliases={"1": {"name": "WAN1", "speed_mbps": 10, "role": "wan"}})
    now = datetime(2026, 11, 12, 15, 0, 30, tzinfo=timezone.utc)
    end = datetime(2026, 11, 12, 14, 58, tzinfo=timezone.utc)

    async def seed(conn):
        await conn.executemany(
            "INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes) VALUES ($1, $2, 1, $3)",
            [(end - timedelta(minutes=i), ep, 67_500_000) for i in range(15, 0, -1)])  # 90% of 10 Mb/s

    run_sql(seed)

    async def go():
        engine, sf = factory(timescale_dsn)
        try:
            async with sf() as db:
                events = await ta.evaluate(db, default_pct=80, default_minutes=15, flow_collection_on=True, now=now)
                await db.commit()
                return events
        finally:
            await engine.dispose()

    events = asyncio.run(go())
    assert [(e.severity, e.link.name, e.direction) for e in events] == [("TRAFFIC_HIGH", "WAN1", "in")]


# --- step 3: neighbours, link events, transit by SNMP addresses -----------------------------------

def test_a_probe_stores_and_matches_neighbours(api_client, run_sql, timescale_dsn):
    from app.services.topology import TopologyGraphManager

    async def go():
        return await start_agent(mib=device_mib(3))

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    agent = asyncio.run_coroutine_threadsafe(go(), loop).result(5)
    try:
        cred = make_set(api_client, agent.port)
        ep = add_endpoint(run_sql, cred["id"])
        sw = add_endpoint(run_sql, None, ip="10.55.0.2")  # the LLDP neighbour, monitored but not polled

        announced = []

        async def announce(reason, endpoint_id):
            announced.append((reason, endpoint_id))

        async def probe():
            engine, sf = factory(timescale_dsn)
            try:
                store = Store(sf, announce=announce)
                target = [t for t in await store.load_targets() if t.endpoint_id == str(ep)][0]
                d = DevicePoller(target, store)
                status = await d.run_once()
                d.close()
                async with sf() as db:
                    adjacency = await TopologyGraphManager._load_neighbour_adjacency(db, {str(ep): {}, str(sw): {}})
                return status, adjacency
            finally:
                await engine.dispose()

        status, adjacency = asyncio.run(probe())
        # The map is told once, when the matched neighbours changed (none -> abj-sw1).
        assert announced == [("snmp_neighbours", str(ep))]
        assert status == "ok"
        nbs = rows(run_sql, """SELECT protocol, local_if_index, remote_name, remote_addresses, matched_endpoint_id, matched_by
                               FROM snmp_neighbours ORDER BY protocol""")
        assert [(n["protocol"], n["remote_name"], n["matched_endpoint_id"], n["matched_by"]) for n in nbs] == \
            [("cdp", "PE-A", None, None), ("lldp", "abj-sw1", sw, "address")]
        assert nbs[0]["local_if_index"] == 2 and nbs[1]["local_if_index"] == 1  # "Gi0/1" found by name
        state = rows(run_sql, "SELECT addresses, sys_name FROM snmp_device_state")[0]
        assert state["addresses"] == ["10.55.0.1"] and state["sys_name"] == "abj-rtr"
        assert adjacency == {str(ep): {str(sw): "LLDP"}, str(sw): {str(ep): "LLDP"}}

        view = api_client.get(f"/api/v1/snmp/endpoints/{ep}").json()["data"]["neighbours"]
        lldp = [n for n in view if n["protocol"] == "lldp"][0]
        assert lldp["matched_endpoint_id"] == str(sw) and lldp["matched_hostname"] == "abj-rtr-10.55.0.2"
        assert [n for n in view if n["protocol"] == "cdp"][0]["remote_addresses"] == ["192.0.2.1"]
    finally:
        loop.call_soon_threadsafe(agent.transport.close)
        loop.call_soon_threadsafe(loop.stop)


def test_link_events_are_claimed_once(run_sql, timescale_dsn):
    from unittest.mock import patch
    from app.services.alert_dispatcher import AlertDispatcher

    ep = add_endpoint(run_sql, None, aliases={"1": {"name": "ISP-A", "role": "wan"}})

    async def seed(conn):
        await conn.execute("INSERT INTO snmp_interface_events (endpoint_id, if_index, if_name, kind) VALUES ($1, 1, 'Gi0/1', 'down')", ep)
        await conn.execute("INSERT INTO snmp_interface_events (endpoint_id, if_index, if_name, kind) VALUES ($1, 2, 'Gi0/2', 'added')", ep)

    run_sql(seed)

    async def claim_twice():
        engine, sf = factory(timescale_dsn)
        try:
            with patch("app.services.alert_dispatcher.AsyncSessionLocal", sf):
                d = AlertDispatcher()
                return await d._claim_link_events(), await d._claim_link_events()
        finally:
            await engine.dispose()

    first, second = asyncio.run(claim_twice())
    assert [(c["kind"], c["if_index"], c["alias"]["role"], c["hostname"]) for c in first] == [("down", 1, "wan", "abj-rtr-127.0.0.1")]
    assert second == []
    assert rows(run_sql, "SELECT COUNT(*) AS n FROM snmp_interface_events WHERE handled_at IS NOT NULL")[0]["n"] == 1


def test_transit_retrace_recognises_a_router_by_its_snmp_addresses(run_sql, timescale_dsn):
    """A path that crosses the router through another of its interfaces (an address SNMP
    reported, not the monitored one) still counts as going through it."""
    from unittest.mock import patch
    from app.services import baseline_route
    from app.services.telemetry_relay import TelemetryRelay

    rtr = add_endpoint(run_sql, None, ip="10.30.0.1")
    behind = add_endpoint(run_sql, None, ip="10.40.0.9")

    async def seed(conn):
        await conn.execute("INSERT INTO snmp_device_state (endpoint_id, addresses) VALUES ($1, ARRAY['10.30.0.1', '10.31.0.1'])", rtr)
        path = json.dumps([{"hop": 1, "ip": "10.1.0.1"}, {"hop": 2, "ip": "10.31.0.1"}, {"hop": 3, "ip": "10.40.0.9"}])
        await conn.execute("INSERT INTO endpoint_baseline_routes (endpoint_id, total_hops, hops, normal_hops) VALUES ($1, 3, $2::jsonb, $2::jsonb)",
                           behind, path)
        await conn.execute("UPDATE endpoints SET enable_scheduled_discovery = TRUE WHERE id = $1", behind)

    run_sql(seed)

    async def go():
        engine, sf = factory(timescale_dsn)
        relay = TelemetryRelay()
        try:
            with patch("app.services.telemetry_relay.AsyncSessionLocal", sf), \
                 patch.object(baseline_route, "discovery_route_queue", asyncio.Queue()), \
                 patch.object(baseline_route, "discovery_in_progress", set()):
                looked_at = await relay._maybe_trace_dependents(str(rtr), "DOWN", "10.30.0.1", delay=0)
                for t in list(relay._transit_tasks):
                    t.cancel()
                return looked_at
        finally:
            await engine.dispose()

    assert asyncio.run(go()) == [str(behind)]
