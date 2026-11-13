"""SNMP poller step 3 without a database: neighbour rows and matching, map placement by LLDP/CDP,
the RCA verdict from the parent chain, WAN link alerts and the re-trace hint.
The stored path (probe -> snmp_neighbours, claims, transit by SNMP addresses) is in
tests/integration/test_snmp_poller.py."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.services.alert_dispatcher import AlertDispatcher
from app.services.topology import TopologyGraphManager
from monitoring.snmp.poller import MatchIndex, match_neighbour, neighbour_rows, norm_mac, norm_name


def test_mac_and_name_normalising():
    assert norm_mac("0C:7D:74:B4:00:00") == norm_mac("0c7d.74b4.0000") == norm_mac("0c-7d-74-b4-00-00") == "0c7d74b40000"
    assert norm_mac("abj-sw1") is None and norm_mac("0c:7d:74") is None and norm_mac(None) is None
    assert norm_name("ABJ-SW1.branch.local") == "abj-sw1" and norm_name("10.0.0.1") == "10.0.0.1" and norm_name("") is None


def test_neighbour_rows_find_local_interfaces_and_drop_duplicates():
    lldp = {"answered": True, "mib": "LLDP-MIB", "neighbours": [
        {"local_port": "GigabitEthernet0/1", "remote_name": "abj-sw1", "remote_chassis": "0c:7d:74:b4:00:00",
         "remote_port": "Gi1/0/48", "remote_addresses": ["10.55.0.2"]},
        {"local_port": "GigabitEthernet0/1", "remote_name": "abj-sw1", "remote_chassis": "0c:7d:74:b4:00:00",
         "remote_port": "Gi1/0/48", "remote_addresses": ["10.55.0.2"]},
    ]}
    cdp = {"answered": True, "neighbours": [
        {"local_ifIndex": 2, "remote_name": "PE-A", "remote_port": "Gi0/0/3", "remote_platform": "ISR", "remote_address": "192.0.2.1"},
    ]}
    rows = neighbour_rows(lldp, cdp, {1: {"name": "Gi0/1", "descr": "GigabitEthernet0/1"}, 2: {"name": "Gi0/2"}})
    assert [(r["protocol"], r["local_if_index"], r["local_port"], r["remote_name"]) for r in rows] == \
        [("lldp", 1, "GigabitEthernet0/1", "abj-sw1"), ("cdp", 2, "Gi0/2", "PE-A")]
    assert rows[1]["remote_addresses"] == ["192.0.2.1"] and len({r["nkey"] for r in rows}) == 2
    assert neighbour_rows(None, {"answered": False}, {}) == []


def test_matching_tries_address_then_mac_then_name_and_never_itself():
    index = MatchIndex(addresses={"10.55.0.2": "sw", "10.55.0.1": "me"}, macs={"0c7d74b40000": "sw-by-mac"},
                       names={"abj-sw1": "sw-by-name", "abj-rtr": "me"})
    assert match_neighbour({"remote_addresses": ["10.55.0.2"], "remote_chassis": "0C:7D:74:B4:00:00", "remote_name": "abj-sw1"},
                           index, "me") == ("sw", "address")
    assert match_neighbour({"remote_addresses": ["10.9.9.9"], "remote_chassis": "0c7d.74b4.0000"}, index, "me") == ("sw-by-mac", "mac")
    assert match_neighbour({"remote_name": "ABJ-SW1.branch.local"}, index, "me") == ("sw-by-name", "name")
    assert match_neighbour({"remote_addresses": ["10.55.0.1"], "remote_name": "abj-rtr"}, index, "me") == (None, None)


def test_switches_traceroute_cannot_place_are_placed_by_their_neighbours():
    root = "root"
    eps = {e: {"device_type": "Switch"} for e in ("rtr", "core", "access", "ap", "lonely", "fw", "sw2")}
    eps["fw"]["device_type"] = "Firewall"
    edges = {("transit:10.0.0.1", "rtr")} | {(root, e) for e in ("core", "access", "ap", "lonely", "fw", "sw2")}
    children = {root: {"core", "access", "ap", "lonely", "fw", "sw2"}}
    adjacency = {
        "rtr": {"core": "LLDP"}, "core": {"rtr": "LLDP", "access": "CDP"}, "access": {"core": "CDP", "ap": "LLDP"},
        "ap": {"access": "LLDP"},
        # A group with nothing placed: the firewall stays under the root, sw2 hangs from it.
        "fw": {"sw2": "LLDP"}, "sw2": {"fw": "LLDP"},
    }
    placed = TopologyGraphManager.place_by_neighbours(edges, children, eps, root, adjacency)
    assert placed == {("rtr", "core"): "LLDP", ("core", "access"): "CDP", ("access", "ap"): "LLDP", ("fw", "sw2"): "LLDP"}
    assert (root, "core") not in edges and ("rtr", "core") in edges and (root, "lonely") in edges and (root, "fw") in edges
    assert children[root] == {"lonely", "fw"}
    # Nothing to do without neighbours.
    assert TopologyGraphManager.place_by_neighbours({(root, "x")}, {}, {"x": {}}, root, {}) == {}


def test_rca_names_the_nearest_parent_that_is_down():
    from app.services import rca_engine
    from app.services.topology import topology_manager

    nodes = {"core": {"label": "abj-core", "ip_address": "10.55.0.1", "state": "UP"},
             "access": {"label": "abj-sw1", "ip_address": "10.55.0.2", "state": "DOWN"},
             "ap": {"label": "abj-ap", "ip_address": "10.55.0.3", "state": "DOWN"}}
    with patch.object(topology_manager, "_l2_parent", {"ap": "access", "access": "core"}), \
         patch.object(topology_manager, "_nodes", nodes):
        failed, good, summary = rca_engine._l2_upstream_verdict("ap")
        assert failed == "10.55.0.2" and "abj-sw1" in summary and "down too" in summary
        assert topology_manager.l2_children("access") == ["ap"] and topology_manager.l2_parent_chain("ap") == ["access", "core"]
        nodes["access"]["state"] = "UP"
        failed, good, summary = rca_engine._l2_upstream_verdict("ap")
        assert failed is None and good == "10.55.0.2" and "still answers" in summary
        assert rca_engine._l2_upstream_verdict("core") is None


def _channel(filters):
    return SimpleNamespace(id=uuid4(), name="ch", channel_type="GENERIC_WEBHOOK", config="x",
                           endpoint_ids=[], subnet_filters=[], severity_filters=filters)


def _event(kind="down", role="wan", minutes_ago=0, host="abj-rtr"):
    return {"id": 1, "endpoint_id": uuid4(), "if_index": 1, "if_name": "Gi0/1", "kind": kind, "detail": None,
            "at": datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), "hostname": host, "ip": "10.55.0.1",
            "alias": {"name": "ISP-A", "role": role}}


def test_wan_link_alerts_go_only_to_channels_that_ticked_them():
    d = AlertDispatcher()
    sent = []

    async def fake_send(**kw):
        sent.append(kw)
        return "DELIVERED", 200, "ok"

    channels = [_channel(["DOWN", "RECOVERED"]), _channel([]), _channel(["LINK_DOWN", "LINK_UP"])]
    with patch.object(d, "_is_alerting_enabled", AsyncMock(return_value=True)), \
         patch.object(d, "_load_enabled_channels", AsyncMock(return_value=channels)), \
         patch.object(d, "_send_to_provider", side_effect=fake_send), \
         patch.object(d, "_parse_channel_config", return_value={}), \
         patch.object(d, "_record_delivery_log", AsyncMock()):
        asyncio.run(d._dispatch_link_event(_event("down")))
        asyncio.run(d._dispatch_link_event(_event("up")))
    assert [s["severity"] for s in sent] == ["LINK_DOWN", "LINK_UP"]
    assert sent[0]["details"]["summary"] == "abj-rtr ISP-A: WAN link down"
    assert sent[1]["details"]["summary"] == "abj-rtr ISP-A: WAN link back up" and sent[0]["event_type"] == "LINK_ALERT"


def test_the_link_loop_alerts_wan_links_and_hints_every_fresh_change():
    d = AlertDispatcher()
    events = [_event("down"), _event("down", role="lan"), _event("down", minutes_ago=30), _event("up", host=None)]

    async def claim():
        d._stop_event.set()  # one round
        return events

    dispatched, hinted = [], []

    async def fake_dispatch(ev):
        dispatched.append(ev)

    async def fake_hint(ev):
        hinted.append(ev)

    async def go():
        d.LINK_CHECK_SECONDS = 0.01
        d._stop_event.clear()
        with patch.object(d, "_claim_link_events", side_effect=claim), \
             patch.object(d, "_dispatch_link_event", side_effect=fake_dispatch), \
             patch.object(d, "_publish_link_hint", side_effect=fake_hint):
            await d._link_event_loop()

    asyncio.run(go())
    assert dispatched == [events[0]]  # WAN and fresh; the LAN one is not alerted
    assert hinted == [events[0], events[1]]  # both fresh ones re-trace; the stale and deleted ones nothing


def test_the_relay_turns_a_link_hint_into_a_transit_retrace():
    from app.services.telemetry_relay import TelemetryRelay

    relay = TelemetryRelay()
    with patch.object(relay, "_maybe_trace_dependents", AsyncMock(return_value=[])) as trace:
        asyncio.run(relay._handle_route_hint({"type": "SNMP_LINK", "endpoint_id": "e1", "state": "DOWN", "ip_address": "10.0.0.1"}))
    trace.assert_awaited_once_with("e1", "DOWN", "10.0.0.1")
