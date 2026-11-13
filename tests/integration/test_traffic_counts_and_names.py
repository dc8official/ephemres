"""The Traffic page and the PDF count, place and name traffic the same way (real TimescaleDB).

The v3.3.6 Traffic page review (docs/architecture/traffic-page-review-2026-11-13.md) found:
- P2: traffic seen by two routers counted twice in Applications and Traffic zones, once in
  Conversations. ERP traffic from Abuja to Lagos crosses both branch routers and both export it.
- P3: "internal" meant a monitored endpoint for the tiles and "monitored or private" for the
  zones, so users' PCs (not endpoints) were "transit".
- C2: "Bulk" was decided by packet size alone, so every Microsoft 365 download was bulk.
- P5: UDP 443 was named HTTPS, and 1433, 8801, 873 and 23 had no name.

The network here: hq-rtr1 and abj-rtr1 both export flows. The Abuja PC 10.1.20.12 (no endpoint)
talks to the Lagos ERP server 10.0.10.5 (an endpoint) over TCP 1433; both routers see it.
"""
import asyncio
import datetime
import itertools
import uuid

import pytest

from app.services.report_compiler import compile_bandwidth_report_data

HQ_ID = uuid.UUID("55555555-0000-0000-0000-0000000000e1")
ABJ_ID = uuid.UUID("55555555-0000-0000-0000-0000000000e2")
ERP_ID = uuid.UUID("55555555-0000-0000-0000-0000000000a1")
PC_IP, ERP_IP, MS365_IP = "10.1.20.12", "10.0.10.5", "52.97.146.2"
TCP, UDP = 6, 17
MB = 1_000_000


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)


def _seed(run_sql, flows):
    """Endpoints plus `flows`: (minutes ago, exporter, src_ep, dst_ep, src, dst, proto, port,
    bytes, packets, flow_count)."""
    now = _now()

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status,
                                   monitoring_enabled, device_role, flow_exporter_ips, site)
            VALUES ($1, '10.0.0.1', 'hq-rtr1', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER', '{10.0.0.1}', 'Lagos HQ'),
                   ($2, '10.1.0.1', 'abj-rtr1', 'Router', 'ACTIVE', false, 'FLOW_EXPORTER', '{10.1.0.1}', 'Abuja'),
                   ($3, $4::inet, 'hq-erp1', 'Server', 'ACTIVE', true, 'ACTIVE_HOST', '{}', 'Lagos HQ')
            """,
            HQ_ID, ABJ_ID, ERP_ID, ERP_IP,
        )
        await conn.executemany(
            """
            INSERT INTO flow_minute_rollups (bucket, exporter_id, src_endpoint_id, dst_endpoint_id,
                src_ip, dst_ip, protocol, dst_port, bytes, packets, flow_count, tcp_flags, tos)
            VALUES ($1, $2, $3, $4, $5::inet, $6::inet, $7, $8, $9, $10, $11, 24, 0)
            """,
            [(now - datetime.timedelta(minutes=ago), *rest) for ago, *rest in flows],
        )

    run_sql(seed)
    return now


def _erp_flows():
    """ERP traffic both routers report (Abuja's report slightly smaller), a QUIC download only
    Abuja sees, and a PC with no endpoint sending to the Internet."""
    return [
        (5, HQ_ID, None, ERP_ID, PC_IP, ERP_IP, TCP, 1433, 10 * MB, 10_000, 4),
        (5, ABJ_ID, None, ERP_ID, PC_IP, ERP_IP, TCP, 1433, 9 * MB, 9_000, 4),
        (5, HQ_ID, ERP_ID, None, ERP_IP, PC_IP, TCP, 1433, 20 * MB, 15_000, 4),
        (5, ABJ_ID, ERP_ID, None, ERP_IP, PC_IP, TCP, 1433, 20 * MB, 15_000, 4),
        (6, ABJ_ID, None, None, MS365_IP, PC_IP, UDP, 443, 4 * MB, 3_000, 2),
        (6, ABJ_ID, None, None, PC_IP, MS365_IP, UDP, 443, 1 * MB, 1_000, 2),
    ]


def _data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _with_session(timescale_dsn, fn):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

    async def run():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(run())


@pytest.mark.parametrize("window", ["1h", "24h"])
def test_traffic_seen_by_two_routers_counts_once_in_applications_and_zones(api_client, run_sql, window):
    # 24h reads the hourly conversation tier (real-time part: nothing here refreshes it, see
    # test_conversation_hourly_tier.py), 1h the minute table.
    _seed(run_sql, _erp_flows())
    data = _data(api_client.get("/api/v1/bandwidth/applications", params={"window": window}))

    apps = {(a["protocol_name"], a["port"]): a for a in data["applications"]}
    # The largest report per conversation: 10 MB + 20 MB, not 10 + 9 + 20 + 20.
    assert apps[("TCP", 1433)]["total_bytes"] == 30 * MB
    assert apps[("TCP", 1433)]["service_label"] == "MS SQL"
    assert apps[("UDP", 443)]["service_label"] == "QUIC (HTTP/3)"
    assert data["total_bytes"] == 35 * MB

    zones = {z["zone"]: z["total_bytes"] for z in data["traffic_zones"]}
    assert zones["East-West (Internal)"] == 30 * MB
    # The PC is no endpoint, but private: its Internet traffic crosses the edge (P3).
    assert zones["North-South Ingress"] == 4 * MB
    assert zones["North-South Egress"] == 1 * MB
    assert zones["External / Transit"] == 0

    # Conversations counts the same rows, so the totals agree (they used to differ by 29 MB).
    convs = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": window}))
    assert sum(c["total_bytes"] for c in convs["items"]) == data["total_bytes"]


def test_one_router_still_shows_its_own_report(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    data = _data(api_client.get("/api/v1/bandwidth/applications", params={"window": "1h", "exporter_id": str(ABJ_ID)}))
    apps = {(a["protocol_name"], a["port"]): a["total_bytes"] for a in data["applications"]}
    assert apps[("TCP", 1433)] == 29 * MB


def test_pdf_counts_once_and_names_every_router(timescale_dsn, run_sql):
    now = _seed(run_sql, _erp_flows())
    data = _with_session(
        timescale_dsn,
        lambda s: compile_bandwidth_report_data(s, [HQ_ID, ABJ_ID], now - datetime.timedelta(hours=1), now),
    )
    zones = {z["zone"]: z["total_bytes"] for z in data["traffic_zones"]}
    assert zones["East-West (Internal)"] == 30 * MB
    top = data["conversations"][0]
    assert (top["src_display"], top["dst_display"], top["port_label"]) == (ERP_IP, PC_IP, "1433 / MS SQL")
    assert top["router_name"] == "abj-rtr1, hq-rtr1"
    assert top["pct_of_transit"] == pytest.approx(20 / 35 * 100)
    assert "443 / QUIC (HTTP/3)" in {c["port_label"] for c in data["conversations"]}


def test_overview_and_all_flows_use_the_zones_rule(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    ov = _data(api_client.get("/api/v1/bandwidth/overview"))
    # Over the 5-minute window: the ERP rows (5 min ago) are in, the QUIC rows (6 min) are not.
    assert ov["total_internal_bps"] == pytest.approx(30 * MB * 8 / 300)
    assert ov["total_transit_bps"] == 0

    series = _data(api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "1h"}))
    totals = {k: sum(p[k] for p in series["points"]) * 60 / 8 for k in ("ingress_bps", "egress_bps", "internal_bps", "transit_bps")}
    assert totals["internal_bps"] == pytest.approx(30 * MB, abs=10)
    assert totals["ingress_bps"] == pytest.approx(4 * MB, abs=10)
    assert totals["egress_bps"] == pytest.approx(1 * MB, abs=10)
    assert totals["transit_bps"] == 0


def test_top_talkers_are_placed_by_site(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    data = _data(api_client.get("/api/v1/bandwidth/top-talkers", params={"window": "1h"}))
    talkers = {t["ip_address"]: t for t in data["top_endpoints"]}
    # An endpoint has its own site; other addresses take the sites of the routers that saw them.
    assert talkers[ERP_IP]["sites"] == ["Lagos HQ"]
    assert talkers[PC_IP]["sites"] == ["Abuja", "Lagos HQ"]
    assert talkers[MS365_IP]["sites"] == ["Abuja"]
    # From the host's side: the ERP server received 10 MB and sent 20 MB.
    assert (talkers[ERP_IP]["ingress_bytes"], talkers[ERP_IP]["egress_bytes"]) == (10 * MB, 20 * MB)


def _conversations(api_client, **params):
    page = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": "1h", **params}))
    return {(c["src_ip"].split("/")[0], c["dst_ip"].split("/")[0], c["dst_port"]): c for c in page["items"]}


def test_conversations_name_the_sites_and_routers_that_saw_them(api_client, run_sql):
    """Review C1: "52.97.146.2 → 10.1.20.12" is Abuja, but the table did not say so."""
    _seed(run_sql, _erp_flows())
    convs = _conversations(api_client)
    erp = convs[(PC_IP, ERP_IP, 1433)]
    # Both routers reported it (its bytes counted once, at the larger report), by site.
    assert [(r["hostname"], r["site"]) for r in erp["routers"]] == [("abj-rtr1", "Abuja"), ("hq-rtr1", "Lagos HQ")]
    assert erp["total_bytes"] == 10 * MB
    assert [r["hostname"] for r in convs[(MS365_IP, PC_IP, 443)]["routers"]] == ["abj-rtr1"]

    # The shared filter keeps the routers in scope, in the table and in each row.
    abuja = _conversations(api_client, site="Abuja")
    assert [r["hostname"] for r in abuja[(PC_IP, ERP_IP, 1433)]["routers"]] == ["abj-rtr1"]
    assert abuja[(PC_IP, ERP_IP, 1433)]["total_bytes"] == 9 * MB


SCANNER_IP = "185.220.101.4"


@pytest.mark.parametrize("window", ["1h", "24h"])
def test_a_suspicious_row_says_when_and_where_it_arrived(api_client, run_sql, window):
    """Review S1: the scan's row said neither which link it arrived on nor since when. The flow
    tables keep conversations per router, not per interface, so the link is named only when the
    router has a single WAN link (Kenneth, 2026-11-13)."""
    now = _seed(run_sql, [])

    async def scans(conn):
        # Abuja has one WAN link, Lagos two.
        await conn.execute(
            """UPDATE endpoints SET flow_interface_aliases = $2::jsonb WHERE id = $1""",
            ABJ_ID, '{"1": {"name": "Glo MPLS", "role": "wan"}, "2": {"name": "LAN"}}',
        )
        await conn.execute(
            """UPDATE endpoints SET flow_interface_aliases = $2::jsonb WHERE id = $1""",
            HQ_ID, '{"1": {"name": "MTN fibre", "role": "wan"}, "4": {"name": "Airtel backup", "role": "wan"}}',
        )
        # SYN-only probes of telnet, from 40 to 10 minutes ago: one scanner through each router.
        await conn.executemany(
            """
            INSERT INTO flow_minute_rollups (bucket, exporter_id, src_ip, dst_ip, protocol, dst_port,
                                             bytes, packets, flow_count, tcp_flags, tos)
            VALUES ($1, $2, $3::inet, $4::inet, 6, 23, 120, 3, 3, 2, 0)
            """,
            [
                (now - datetime.timedelta(minutes=m), router, SCANNER_IP, target)
                for m in range(10, 41)
                for router, target in ((ABJ_ID, "10.1.0.1"), (HQ_ID, "10.0.0.1"))
            ],
        )

    run_sql(scans)
    convs = _conversations(api_client, window=window, quick="anomalies", sort="flows")
    via_abuja = convs[(SCANNER_IP, "10.1.0.1", 23)]
    via_lagos = convs[(SCANNER_IP, "10.0.0.1", 23)]
    assert [(r["hostname"], r["wan_link"]) for r in via_abuja["routers"]] == [("abj-rtr1", "Glo MPLS")]
    assert [(r["hostname"], r["wan_link"]) for r in via_lagos["routers"]] == [("hq-rtr1", None)]

    first = datetime.datetime.fromisoformat(via_abuja["first_seen"])
    last = datetime.datetime.fromisoformat(via_abuja["last_seen"])
    if window == "1h":
        # To the minute: the start of the first minute and the end of the last.
        assert (first, last) == (now - datetime.timedelta(minutes=40), now - datetime.timedelta(minutes=9))
    else:
        # Beyond 2 hours, the hourly tier: the hour it started in, the end of the hour it was
        # last seen in, never later than now.
        assert first == (now - datetime.timedelta(minutes=40)).replace(minute=0)
        assert now - datetime.timedelta(minutes=9) <= last <= datetime.datetime.now(datetime.timezone.utc)


def test_bulk_needs_a_steady_flow(api_client, run_sql):
    backup = [
        # An rsync job between two private hosts, ~8 Mb/s for 55 minutes.
        (m, HQ_ID, None, None, "10.0.0.30", "10.0.0.40", TCP, 873, 60 * MB, 43_000, 1)
        for m in range(2, 57)
    ]
    download = [
        # A Microsoft 365 download: full-size packets, 500 MB, but 10 minutes only.
        (m, ABJ_ID, None, None, MS365_IP, PC_IP, TCP, 443, 50 * MB, 35_000, 2)
        for m in range(2, 12)
    ]
    _seed(run_sql, backup + download)
    page = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": "1h"}))
    profiles = {c["dst_port"]: c["traffic_profile"] for c in page["items"]}
    assert profiles == {873: "Bulk Transfer", 443: "Download"}
    assert page["counts"]["bulk"] == 1
    bulk = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": "1h", "quick": "bulk"}))
    assert [(c["dst_port"], c["service_name"]) for c in bulk["items"]] == [(873, "rsync")]


@pytest.mark.parametrize("window", ["24h", "7d"])
def test_a_nightly_backup_is_bulk_and_all_day_browsing_is_not(api_client, run_sql, window):
    # The rate is over the time a conversation was seen. Measured over the window, a two-hour
    # backup was "Download" in a day and in a week, and an office's all-day Microsoft 365
    # traffic was "Bulk".
    backup = [
        # Two hours a night, ~12 Mb/s, on three nights.
        (night * 1440 + m, HQ_ID, None, None, "10.0.10.10", "41.58.10.5", TCP, 22, 90 * MB, 64_000, 1)
        for night in range(3)
        for m in range(600, 720)
    ]
    browsing = [
        # ~1.5 Mb/s in full-size packets, all day for three days.
        (m, ABJ_ID, None, None, MS365_IP, PC_IP, TCP, 443, 11 * MB, 8_000, 3)
        for m in range(2, 3 * 1440)
    ]
    _seed(run_sql, backup + browsing)
    page = _data(api_client.get("/api/v1/bandwidth/conversations", params={"window": window}))
    profiles = {c["dst_port"]: c["traffic_profile"] for c in page["items"]}
    assert profiles == {22: "Bulk Transfer", 443: "Download"}


def test_a_year_reads_the_last_90_days(api_client, run_sql):
    _seed(run_sql, _erp_flows())
    data = _data(api_client.get("/api/v1/bandwidth/applications", params={"window": "1y"}))
    start = datetime.datetime.fromisoformat(data["window_start"])
    assert datetime.timedelta(days=89, hours=23) < _now() - start <= datetime.timedelta(days=90, minutes=1)
    assert data["zones_window_start"] == data["qos_window_start"] == data["window_start"]
    assert data["total_bytes"] == 35 * MB


ADDRESSES = [
    "10.0.1.10", "10.0.1.10/32", "172.16.0.5/24", "172.32.0.1", "192.168.1.10", "192.169.0.1",
    "127.0.0.1", "169.254.3.4", "100.64.0.1", "8.8.8.8", "52.97.146.2", "203.0.113.5",
    "fd00::1", "fe80::1", "::1", "2001:4860::8888",
]


def test_sql_rules_match_python(run_sql):
    from app.services import flow_metrics as fm

    values = ", ".join(f"('{a}'::inet)" for a in ADDRESSES)
    rows = run_sql(lambda conn: conn.fetch(
        f"SELECT host(a) AS ip, masklen(a) AS bits, {fm.internal_sql('a', 'NULL::uuid')} AS internal "
        f"FROM (VALUES {values}) AS t(a)"
    ))
    for r in rows:
        assert r["internal"] == fm.is_internal_address(r["ip"], None), r["ip"]
    assert fm.is_internal_address("8.8.8.8", "an-endpoint")

    pairs = list(itertools.product(ADDRESSES[:6] + ADDRESSES[7:10], repeat=2))
    values = ", ".join(f"('{s}'::inet, '{d}'::inet)" for s, d in pairs)
    zone = fm.zone_sql("s", "NULL::uuid", "d", "NULL::uuid")
    rows = run_sql(lambda conn: conn.fetch(f"SELECT host(s) AS s, host(d) AS d, {zone} AS zone FROM (VALUES {values}) AS t(s, d)"))
    for r in rows:
        expected = fm.classify_zone(fm.is_internal_address(r["s"], None), fm.is_internal_address(r["d"], None))
        assert r["zone"] == expected, (r["s"], r["d"])

    # Straddles every threshold: 1100 B packets, 1800 s seen, 5 Mb/s over 1800 s (1.125 GB).
    grid = list(itertools.product((1099, 1100, 1400), (1799, 1800, 3600), (1_124_999_999, 1_125_000_000, 2_250_000_000)))
    values = ", ".join(f"({a}, {seen}, {b})" for a, seen, b in grid)
    rows = run_sql(lambda conn: conn.fetch(
        f"SELECT a, seen, b, {fm.bulk_sql('a', 'seen', 'b')} AS bulk FROM (VALUES {values}) AS t(a, seen, b)"
    ))
    assert len(rows) == len(grid)
    for r in rows:
        assert r["bulk"] == fm.is_bulk(r["a"], r["seen"], r["b"]), dict(r)
    assert {r["bulk"] for r in rows} == {True, False}
