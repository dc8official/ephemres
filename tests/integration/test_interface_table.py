"""The Traffic page's Interfaces table across devices (real TimescaleDB).

v3.3.6 build plan 7.5 step 6 (review I1, I2, I3): GET /bandwidth/interfaces without a device
lists the interfaces of every router the shared filter picks, WAN links first, then the busiest
others, shut-down ports last. Each row has "now" (last five complete minutes) beside the
window's average / P95 / busiest minute; a polled router's ports take their status from SNMP,
with the time they were down in the window; flow-only routers keep "traffic seen" / "idle".

The network (rates steady over the last two hours):
- hq-rtr1 (Lagos HQ), polled over SNMP: 1 "MTN fibre" WAN 100 Mb/s from SNMP at 40 Mb/s (its
  flows say 30: counters win), 2 Gi0/1 shut down, 3 "Core LAN" at 50 Mb/s, 4 "Airtel backup"
  WAN typed 20 Mb/s at 2 Mb/s, down 40 minutes within the hour;
- abj-rtr1 (Abuja), flows only: 1 "Glo MPLS" WAN 20 Mb/s at 12.4 Mb/s (62 %), 2 "LAN" no speed
  at 3 Mb/s;
- ph-rtr1 (Port Harcourt), flows only: 1 "WAN" with no speed at 7 Mb/s, 3 "Spare" typed in
  with no traffic.
"""
import datetime
import json
import uuid

import pytest

from tests.integration.test_wan_links import (
    ENDPOINT_SQL,
    FLOW_IF_SQL,
    MB,
    SNMP_IF_SQL,
    _data,
    _minute_bytes,
    _now_minute,
)

HQ = uuid.UUID("55555555-0000-0000-0000-0000000000c1")
ABJ = uuid.UUID("55555555-0000-0000-0000-0000000000c2")
PH = uuid.UUID("55555555-0000-0000-0000-0000000000c3")


def _seed(run_sql):
    now = _now_minute()
    # Two hours of minutes up to the current one, so the hour is covered whole and the five
    # minutes of "now" are full even if the clock moves on before the request.
    minutes = [now - datetime.timedelta(minutes=m) for m in range(121)]

    async def seed(conn):
        await conn.execute(ENDPOINT_SQL, HQ, "10.0.0.1", "hq-rtr1", json.dumps({
            "1": {"role": "wan"},
            "4": {"name": "Airtel backup", "speed_mbps": 20, "role": "wan"},
        }), "Lagos HQ")
        await conn.execute(ENDPOINT_SQL, ABJ, "10.1.0.1", "abj-rtr1", json.dumps({
            "1": {"name": "Glo MPLS", "speed_mbps": 20, "role": "wan"},
            "2": {"name": "LAN"},
        }), "Abuja")
        await conn.execute(ENDPOINT_SQL, PH, "10.3.0.1", "ph-rtr1", json.dumps({
            "1": {"name": "WAN", "role": "wan"},
            "3": {"name": "Spare"},
        }), "Port Harcourt")
        await conn.execute("INSERT INTO snmp_device_state (endpoint_id, status, last_ok_at) VALUES ($1, 'ok', $2)", HQ, now)
        await conn.executemany(
            """INSERT INTO snmp_interfaces (endpoint_id, if_index, if_name, if_alias, speed_mbps, admin_status, oper_status)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            [
                (HQ, 1, "Gi0/0", "MTN fibre", 100, "up", "up"),
                (HQ, 2, "Gi0/1", "", 1000, "down", "down"),
                (HQ, 3, "Gi0/2", "Core LAN", 1000, "up", "up"),
                (HQ, 4, "Gi0/3", "", 1000, "up", "up"),
                # A port SNMP sees down with nothing typed and no traffic: not listed.
                (HQ, 9, "Gi0/9", "", 1000, "up", "down"),
            ],
        )
        await conn.executemany(
            "INSERT INTO snmp_interface_events (endpoint_id, if_index, at, kind) VALUES ($1, 4, $2, $3)",
            [
                (HQ, now - datetime.timedelta(hours=5), "down"),  # before the hour: not counted
                (HQ, now - datetime.timedelta(hours=4), "up"),
                (HQ, now - datetime.timedelta(minutes=50), "down"),
                (HQ, now - datetime.timedelta(minutes=10), "up"),
            ],
        )
        await conn.executemany(SNMP_IF_SQL, [(m, HQ, 1, _minute_bytes(40), _minute_bytes(9)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, HQ, 1, _minute_bytes(30), _minute_bytes(7)) for m in minutes])
        await conn.executemany(SNMP_IF_SQL, [(m, HQ, 3, _minute_bytes(50), _minute_bytes(20)) for m in minutes])
        await conn.executemany(SNMP_IF_SQL, [(m, HQ, 4, _minute_bytes(2), _minute_bytes(1)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, ABJ, 1, _minute_bytes(12.4), _minute_bytes(2)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, ABJ, 2, _minute_bytes(1), _minute_bytes(3)) for m in minutes])
        await conn.executemany(FLOW_IF_SQL, [(m, PH, 1, _minute_bytes(7), _minute_bytes(1)) for m in minutes])

    run_sql(seed)
    return now


def _table(api_client, **params):
    return _data(api_client.get("/api/v1/bandwidth/interfaces", params={"window": "1h", **params}))


def test_every_routers_interfaces_wan_first_then_busiest_shut_down_last(api_client, run_sql):
    _seed(run_sql)
    data = _table(api_client)
    assert data["exporter_id"] is None and data["devices"] == 3
    rows = data["interfaces"]
    assert [(r["exporter_hostname"], r["name"]) for r in rows] == [
        # WAN links, busiest against their speed first; the one without a speed after them.
        ("abj-rtr1", "Glo MPLS"),
        ("hq-rtr1", "MTN fibre"),
        ("hq-rtr1", "Airtel backup"),
        ("ph-rtr1", "WAN"),
        # Then the others by traffic, then the port an admin shut down.
        ("hq-rtr1", "Core LAN"),
        ("abj-rtr1", "LAN"),
        ("ph-rtr1", "Spare"),
        ("hq-rtr1", "Gi0/1"),
    ]
    by_name = {r["name"]: r for r in rows}
    assert {r["site"] for r in rows} == {"Lagos HQ", "Abuja", "Port Harcourt"}

    # Now beside the window: five complete minutes at the steady rate.
    glo = by_name["Glo MPLS"]
    assert (glo["now_in_bps"], glo["now_out_bps"]) == (pytest.approx(12.4 * MB), pytest.approx(2 * MB))
    assert glo["utilization_percentage"] == pytest.approx(62.0, abs=1.5)  # the hour's average
    assert glo["p95_utilization_percentage"] == pytest.approx(62.0)
    assert glo["in_peak_bps"] == pytest.approx(12.4 * MB)
    assert (glo["traffic_source"], glo["status"], glo["down_spans"]) == ("flow", "traffic_seen", [])

    # The fibre: SNMP's counters over its flows, and SNMP's status.
    mtn = by_name["MTN fibre"]
    assert (mtn["now_in_bps"], mtn["traffic_source"], mtn["status"]) == (pytest.approx(40 * MB), "snmp", "up")
    assert (mtn["speed_mbps"], mtn["speed_source"]) == (100, "snmp")

    # The backup was down 40 minutes within the hour (the earlier outage is outside it).
    airtel = by_name["Airtel backup"]
    assert (airtel["status"], airtel["down_seconds"]) == ("up", 2400)
    assert len(airtel["down_spans"]) == 1 and airtel["down_spans"][0]["end"] is not None

    # Flow-only: traffic seen, or idle for a typed interface with none; no speed, no utilisation.
    assert (by_name["LAN"]["status"], by_name["LAN"]["utilization_percentage"]) == ("traffic_seen", None)
    assert (by_name["Spare"]["status"], by_name["Spare"]["now_in_bps"]) == ("idle", 0)
    assert (by_name["Gi0/1"]["status"], by_name["Gi0/1"]["admin_status"]) == ("shut_down", "down")
    # A port SNMP sees down with no traffic, name or event in the window is left out.
    assert "Gi0/9" not in by_name


def test_one_device_by_index_and_the_filter_applies(api_client, run_sql):
    _seed(run_sql)
    one = _table(api_client, device_id=str(HQ))
    assert one["exporter_id"] == str(HQ) and one["devices"] == 1
    # One device: by index as before, the shut-down port last.
    assert [r["interface_idx"] for r in one["interfaces"]] == [1, 3, 4, 2]
    assert [r["name"] for r in _table(api_client, site="Abuja")["interfaces"]] == ["Glo MPLS", "LAN"]
    assert _table(api_client, site="Abuja", device_id=str(HQ))["interfaces"] == []
    assert _table(api_client, tag="no-such-tag")["interfaces"] == []


def test_a_router_that_stops_answering_snmp(api_client, run_sql):
    """While the router doesn't reply, SNMP's last view of a port can't be trusted: its ports
    read "SNMP not answering", flow-only routers are unaffected."""
    _seed(run_sql)

    async def silence(conn):
        await conn.execute("UPDATE snmp_device_state SET status = 'no_reply' WHERE endpoint_id = $1", HQ)

    run_sql(silence)
    statuses = {(r["exporter_hostname"], r["interface_idx"]): r["status"] for r in _table(api_client)["interfaces"]}
    assert statuses[("hq-rtr1", 1)] == statuses[("hq-rtr1", 2)] == "not_answering"
    assert statuses[("abj-rtr1", 1)] == "traffic_seen"
