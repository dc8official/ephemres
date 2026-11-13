"""The Traffic page scope: the shared filter plus one optional device (real TimescaleDB).

v3.3.6 build plan 7.5 step 2 (review P4). Every /bandwidth read takes the phase 2 filter (site,
location, tag, type, q) and an optional `device_id`; both resolve through
services/endpoint_filter.traffic_scope to the routers whose traffic is shown. Before, the page
took one exporter at a time, ignored the filter, and never listed a router polled over SNMP
that exports no flows.

The network: hq-rtr1 (Lagos HQ) and abj-rtr1 (Abuja) both export flows and both see the ERP
traffic between an Abuja PC and the Lagos ERP server; abj-rtr1 alone sees a QUIC download.
ph-rtr1 (Port Harcourt) exports nothing: it is polled over SNMP and its WAN link has counters.
"""
import datetime
import json
import uuid

import pytest

from tests.integration.test_traffic_counts_and_names import (
    ABJ_ID,
    HQ_ID,
    MB,
    _data,
    _erp_flows,
    _now,
    _seed,
    _with_session,
)

PH_ID = uuid.UUID("55555555-0000-0000-0000-0000000000e3")
# ph-rtr1's WAN link: 6 MB in and 3 MB out in each of the three minutes before the last.
PH_IN, PH_OUT = 6 * MB, 3 * MB


def _seed_snmp_router(run_sql, api_client, *, with_credential=True):
    """ph-rtr1: polled over SNMP (a credential set, or only poll state left by ephemres-snmpd),
    no flows, one WAN link with counters for minutes 2-4 ago."""
    cred_id = None
    if with_credential:
        res = api_client.post("/api/v1/snmp/credentials", json={
            "name": f"ph-{uuid.uuid4().hex[:6]}", "version": "2c", "community": "public",
            "port": 161, "timeout_ms": 500, "retries": 0,
        })
        assert res.status_code in (200, 201), res.text
        cred_id = uuid.UUID(res.json()["data"]["id"])
    now = _now()

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status, monitoring_enabled,
                                      flow_interface_aliases, snmp_credential_id, site)
               VALUES ($1, '10.3.0.1', 'ph-rtr1', 'Router', 'ACTIVE', false, $2::jsonb, $3, 'Port Harcourt')""",
            PH_ID, json.dumps({"1": {"name": "MTN 50M", "speed_mbps": 50, "role": "wan"}}), cred_id,
        )
        if not with_credential:
            await conn.execute("INSERT INTO snmp_device_state (endpoint_id) VALUES ($1)", PH_ID)
        await conn.executemany(
            """INSERT INTO snmp_interface_minute_rollups (bucket, exporter_id, interface_idx, in_bytes, out_bytes,
                   in_packets, out_packets) VALUES ($1, $2, 1, $3, $4, 1000, 500)""",
            [(now - datetime.timedelta(minutes=m), PH_ID, PH_IN, PH_OUT) for m in (2, 3, 4)],
        )

    run_sql(seed)


def _without_times(data):
    """A response without the request's own timestamps (window starts move between requests)."""
    return {k: v for k, v in data.items() if not k.endswith("window_start")}


def _apps_total(api_client, **params):
    return _data(api_client.get("/api/v1/bandwidth/applications", params={"window": "1h", **params}))["total_bytes"]


def _conversations_total(api_client, **params):
    items = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": "1h", "limit": 200, **params}))["items"]
    return sum(c["total_bytes"] for c in items)


@pytest.mark.parametrize("params, expected", [
    # Every router: the ERP traffic once at the larger report (30 MB) plus Abuja's QUIC (5 MB).
    ({}, 35 * MB),
    # Abuja's router: its own report of the ERP traffic (29 MB) and the QUIC download.
    ({"site": "Abuja"}, 34 * MB),
    # Matched case-insensitively, like everywhere the filter is used.
    ({"site": "lagos hq"}, 30 * MB),
    # Two sites: the routers of either, the ERP traffic still counted once.
    ({"site": ["Abuja", "Lagos HQ"]}, 35 * MB),
    # A filter matching no router shows nothing, not everything.
    ({"tag": "no-such-tag"}, 0),
    # Search picks routers too (hostname contains "abj").
    ({"q": "abj"}, 34 * MB),
])
def test_the_filter_picks_routers_and_their_traffic(api_client, run_sql, params, expected):
    _seed(run_sql, _erp_flows())
    assert _apps_total(api_client, **params) == expected
    # Conversations reads the same rows under the same scope.
    assert _conversations_total(api_client, **params) == expected


def test_the_filter_covers_the_routers_the_dashboard_matches(api_client, run_sql, timescale_dsn):
    """The 4.5 rule: one filter, the same endpoints everywhere. The Traffic scope is exactly the
    list /endpoints/match gives the dashboard, the reports and the map."""
    from app.services.endpoint_filter import EndpointFilter, traffic_scope

    _seed(run_sql, _erp_flows())
    for params in ({"site": "Abuja"}, {"q": "rtr"}, {"type": "Router"}, {"site": "Lagos HQ", "tag": "x"}):
        matched = _data(api_client.get("/api/v1/endpoints/match", params=params))["ids"]
        f = EndpointFilter.build(site=params.get("site"), tag=params.get("tag"), type=params.get("type"), q=params.get("q"))
        scope = _with_session(timescale_dsn, lambda s: traffic_scope(s, f))
        assert sorted(str(i) for i in scope) == sorted(matched), params


def test_a_device_counts_only_inside_the_filter(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    assert _apps_total(api_client, device_id=str(ABJ_ID)) == 34 * MB
    assert _apps_total(api_client, device_id=str(ABJ_ID), site="Abuja") == 34 * MB
    # The filter excludes the device: nothing, rather than ignoring the filter.
    assert _apps_total(api_client, device_id=str(ABJ_ID), site="Lagos HQ") == 0
    overview = _data(api_client.get("/api/v1/bandwidth/overview", params={"device_id": str(ABJ_ID), "site": "Lagos HQ"}))
    assert overview["active_exporters_count"] == 0 and overview["exporter_id"] is None


def test_old_exporter_id_links_still_work(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    for path, extra in (
        ("applications", {"window": "1h"}),
        ("conversations", {"window": "1h"}),
        ("top-talkers", {"window": "1h"}),
        ("traffic-series", {"window": "1h"}),
        ("interfaces", {"window": "1h"}),
    ):
        old = _data(api_client.get(f"/api/v1/bandwidth/{path}", params={**extra, "exporter_id": str(ABJ_ID)}))
        new = _data(api_client.get(f"/api/v1/bandwidth/{path}", params={**extra, "device_id": str(ABJ_ID)}))
        assert _without_times(old) == _without_times(new), path
    overview = _data(api_client.get("/api/v1/bandwidth/overview", params={"exporter_id": str(ABJ_ID)}))
    assert overview["exporter_id"] == str(ABJ_ID) and overview["active_exporters_count"] == 1
    assert _apps_total(api_client, exporter_id=str(ABJ_ID)) == 34 * MB
    # Without a device, /interfaces lists every router's (step 6, test_interface_table.py).
    assert _data(api_client.get("/api/v1/bandwidth/interfaces", params={"window": "1h"}))["exporter_id"] is None


@pytest.mark.parametrize("with_credential", [True, False])
def test_an_snmp_only_router_is_listed_and_its_wan_traffic_counts(api_client, run_sql, with_credential):
    _seed(run_sql, _erp_flows())
    _seed_snmp_router(run_sql, api_client, with_credential=with_credential)

    devices = _data(api_client.get("/api/v1/bandwidth/exporters"))["exporters"]
    # Grouped by site in the picker: sorted by site, then name.
    assert [(d["site"], d["hostname"]) for d in devices] == [
        ("Abuja", "abj-rtr1"), ("Lagos HQ", "hq-rtr1"), ("Port Harcourt", "ph-rtr1"),
    ]
    ph = devices[2]
    assert (ph["snmp_polled"], ph["sends_flows"]) == (True, False)
    assert all(d["sends_flows"] and not d["snmp_polled"] for d in devices[:2])

    # The filter narrows the list the picker offers.
    listed = _data(api_client.get("/api/v1/bandwidth/exporters", params={"site": "Port Harcourt"}))["exporters"]
    assert [d["hostname"] for d in listed] == ["ph-rtr1"]

    # Its WAN link counts in its site's scope, from the counters, though it sends no flows.
    expected_in_bps = 3 * PH_IN * 8 / 300  # three minutes of the overview's five
    for params in ({"site": "Port Harcourt"}, {"device_id": str(PH_ID)}, {}):
        overview = _data(api_client.get("/api/v1/bandwidth/overview", params=params))
        links = {(l["exporter_hostname"], l["name"]): l for l in overview["wan_links"]}
        assert links[("ph-rtr1", "MTN 50M")]["in_bps"] == pytest.approx(expected_in_bps), params
    scoped = _data(api_client.get("/api/v1/bandwidth/overview", params={"site": "Port Harcourt"}))
    assert scoped["wan_in_bps"] == pytest.approx(expected_in_bps)
    assert scoped["wan_out_bps"] == pytest.approx(3 * PH_OUT * 8 / 300)
    assert scoped["total_flows_count"] == 0

    # The WAN chart for the site: the counters, minute by minute.
    series = _data(api_client.get(
        "/api/v1/bandwidth/traffic-series", params={"window": "1h", "wan_only": "true", "site": "Port Harcourt"}
    ))
    assert series["wan_only"] is True
    assert round(sum(p["ingress_bps"] for p in series["points"]) * 60 / 8) == 3 * PH_IN

    # Abuja's scope has no Port Harcourt link.
    abuja = _data(api_client.get("/api/v1/bandwidth/overview", params={"site": "Abuja"}))
    assert [l["exporter_hostname"] for l in abuja["wan_links"]] == []
