"""Site, location, tags and the shared filter on real PostgreSQL (v3.3.6 build plan, phase 2).

The rule: any value within one field, all fields together, case-insensitive for site and
location, tags lower case. These tests seed a small fleet and check:

* each field alone, combinations, case, search, and the empty filter;
* every consumer returns the same set for the same filter: the endpoint list, /match, the fleet
  summary, the PDF and CSV scope (resolve_scope);
* create and update normalise and reuse the spelling in use;
* bulk update and rename change what they should and write audit entries;
* migration 0023 kept existing locations and gave everyone empty tags.
"""
import asyncio
import uuid

import pytest

from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
from app.services.endpoint_filter import EndpointFilter, matching_ids, resolve_scope

FLEET = [
    # hostname, site, location, tags, device_type
    ("abj-core", "Abuja", "Building A", ["mpls", "core"], "Router"),
    ("abj-sw", "Abuja", "Building B", ["access"], "Switch"),
    ("lag-core", "Lagos", "Building A", ["mpls"], "Router"),
    ("lag-fw", "lagos", "DMZ", ["firewall", "core"], "Firewall"),
    ("old-host", None, "Rack 5 (old text)", [], "Server"),
]


@pytest.fixture
def fleet(run_sql):
    ids = {}

    async def seed(conn):
        for i, (host, site, loc, tags, dtype) in enumerate(FLEET):
            ep_id = uuid.uuid4()
            ids[host] = str(ep_id)
            await conn.execute(
                """INSERT INTO endpoints (id, ip_address, hostname, device_type, site, location, tags, endpoint_status)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, 'ACTIVE')""",
                ep_id, f"10.9.0.{i + 1}", host, dtype, site, loc, tags,
            )

    run_sql(seed)
    return ids


def _ids(timescale_dsn, f):
    async def _inner():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                return [str(i) for i in await matching_ids(db, f)]
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def _hosts(fleet, ids):
    by_id = {v: k for k, v in fleet.items()}
    return sorted(by_id[i] for i in ids)


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, ["abj-core", "abj-sw", "lag-core", "lag-fw", "old-host"]),
        ({"site": ["abuja"]}, ["abj-core", "abj-sw"]),
        ({"site": ["LAGOS"]}, ["lag-core", "lag-fw"]),  # "Lagos" and "lagos" are one site
        ({"site": ["Abuja", "Lagos"]}, ["abj-core", "abj-sw", "lag-core", "lag-fw"]),
        ({"location": ["building a"]}, ["abj-core", "lag-core"]),
        ({"tag": ["MPLS"]}, ["abj-core", "lag-core"]),
        ({"tag": ["mpls", "core"]}, ["abj-core", "lag-core", "lag-fw"]),  # any of the tags
        ({"site": ["Abuja"], "tag": ["mpls"]}, ["abj-core"]),  # all fields together
        ({"site": ["Lagos"], "location": ["Building A"]}, ["lag-core"]),
        ({"type": ["router"]}, ["abj-core", "lag-core"]),
        ({"q": "RACK 5"}, ["old-host"]),
        ({"q": "dmz"}, ["lag-fw"]),
        ({"q": "firewall"}, ["lag-fw"]),  # search covers tags
        ({"q": "10.9.0.2"}, ["abj-sw"]),
        ({"q": "50%"}, []),  # % taken literally, not as a wildcard
        ({"site": ["Kano"]}, []),
    ],
)
def test_filter_rules(timescale_dsn, fleet, kwargs, expected):
    assert _hosts(fleet, _ids(timescale_dsn, EndpointFilter.build(**kwargs))) == expected


def test_deleted_endpoints_never_match(timescale_dsn, fleet, run_sql):
    async def delete(conn):
        await conn.execute("UPDATE endpoints SET endpoint_status = 'DELETED' WHERE hostname = 'abj-sw'")

    run_sql(delete)
    assert _hosts(fleet, _ids(timescale_dsn, EndpointFilter.build(site=["Abuja"]))) == ["abj-core"]


def test_every_consumer_returns_the_same_set(timescale_dsn, fleet, api_client):
    """List, /match, fleet summary and the report/export scope, for one filter."""
    # Repeated keys, as the browser sends them.
    qs = "site=abuja&tag=mpls&tag=access"
    expected = sorted([fleet["abj-core"], fleet["abj-sw"]])

    listed = api_client.get(f"/api/v1/endpoints/?{qs}").json()["data"]
    matched = api_client.get(f"/api/v1/endpoints/match?{qs}").json()["data"]
    summary = api_client.get(
        f"/api/v1/reports/fleet-summary?{qs}&start_date=2026-01-01T00:00:00Z&end_date=2026-01-02T00:00:00Z"
    ).json()["data"]

    assert sorted(e["id"] for e in listed) == expected
    assert sorted(matched["ids"]) == expected
    assert matched["description"] == "Site abuja · Tags mpls, access"
    assert sorted(str(e["id"]) for e in summary["endpoints"]) == expected
    assert summary["filter_description"] == "Site abuja · Tags mpls, access"
    assert {e["site"] for e in summary["endpoints"]} == {"Abuja"}

    async def scope():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with factory() as db:
                f = EndpointFilter.build(site=["abuja"], tag=["mpls", "access"])
                everything = await resolve_scope(db, f, None)
                picked = await resolve_scope(db, f, [uuid.UUID(fleet["abj-sw"]), uuid.UUID(fleet["lag-core"])])
                return [str(i) for i in everything], [str(i) for i in picked]
        finally:
            await engine.dispose()

    everything, picked = asyncio.run(scope())
    assert sorted(everything) == expected
    # Picked and filtered: only the picked ones the filter matches.
    assert picked == [fleet["abj-sw"]]


def test_pdf_with_a_filter_prints_it(fleet, api_client):
    res = api_client.post("/api/v1/reports/pdf", json={
        "template_type": "availability",
        "filter": {"site": ["Lagos"]},
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-02T00:00:00Z",
    })
    assert res.status_code == 200, res.text
    assert res.content.startswith(b"%PDF")
    from app.core.pdf import NativePdfReader

    text = NativePdfReader(res.content).pages[0].extract_text()
    assert "Filter: Site Lagos" in text
    assert "lag-core" in text and "abj-core" not in text

    empty = api_client.post("/api/v1/reports/pdf", json={
        "template_type": "availability",
        "filter": {"site": ["Kano"]},
        "start_date": "2026-01-01T00:00:00Z",
        "end_date": "2026-01-02T00:00:00Z",
    })
    assert empty.status_code == 400


def test_bad_tag_is_refused(api_client, fleet):
    res = api_client.get('/api/v1/endpoints/match?tag=a%22b')
    assert res.status_code == 400


def test_create_and_update_normalise_and_reuse_spelling(api_client, fleet, run_sql):
    res = api_client.post("/api/v1/endpoints/", json={
        "ip_address": "10.9.1.1", "hostname": "new-one", "device_type": "Router",
        "site": "  ABUJA ", "location": "building   a", "tags": ["MPLS", "mpls", " Edge "],
    })
    assert res.status_code == 201, res.text
    new_id = res.json()["data"]["id"]

    async def read(conn):
        return await conn.fetchrow("SELECT site, location, tags FROM endpoints WHERE id = $1", uuid.UUID(new_id))

    row = run_sql(read)
    assert (row["site"], row["location"], list(row["tags"])) == ("Abuja", "Building A", ["mpls", "edge"])

    # Clearing: an empty site removes it; tags left out stay as they were.
    res = api_client.patch(f"/api/v1/endpoints/{new_id}", json={"site": ""})
    assert res.status_code == 200, res.text
    row = run_sql(read)
    assert row["site"] is None and list(row["tags"]) == ["mpls", "edge"]


def test_suggestions_count_one_spelling_per_value(api_client, fleet):
    data = api_client.get("/api/v1/endpoints/suggestions").json()["data"]
    sites = {s["value"].lower(): s["count"] for s in data["sites"]}
    assert sites == {"abuja": 2, "lagos": 2}
    assert {t["value"]: t["count"] for t in data["tags"]}["core"] == 2
    within = api_client.get("/api/v1/endpoints/suggestions", params={"site": "lagos"}).json()["data"]
    assert sorted(l["value"] for l in within["locations"]) == ["Building A", "DMZ"]


def test_bulk_update_and_rename_change_endpoints_and_are_audited(api_client, fleet, run_sql):
    res = api_client.post("/api/v1/endpoints/bulk-update", json={
        "filter": {"location": ["Rack 5 (old text)"]}, "site": "abuja", "add_tags": ["Legacy"],
    })
    assert res.status_code == 200, res.text
    assert res.json()["data"] == {"matched": 1, "changed": 1}

    res = api_client.post("/api/v1/endpoints/bulk-update", json={
        "endpoint_ids": [fleet["abj-core"], fleet["lag-core"]], "remove_tags": ["mpls"],
    })
    assert res.json()["data"]["changed"] == 2

    # Merge "lagos" into "Lagos" (one spelling), then rename a tag.
    res = api_client.post("/api/v1/endpoints/rename", json={"field": "site", "from_value": "lagos", "to_value": "Lagos Island"})
    assert res.json()["data"] == {"changed": 2, "to": "Lagos Island"}
    res = api_client.post("/api/v1/endpoints/rename", json={"field": "tag", "from_value": "core", "to_value": "backbone"})
    assert res.json()["data"]["changed"] == 2

    async def read(conn):
        rows = await conn.fetch("SELECT hostname, site, tags FROM endpoints ORDER BY hostname")
        audits = await conn.fetch(
            "SELECT action FROM audit_logs WHERE action IN ('ENDPOINT:BULK_UPDATE', 'ENDPOINT:RENAME_LABEL')"
        )
        return {r["hostname"]: (r["site"], list(r["tags"])) for r in rows}, [a["action"] for a in audits]

    state, audits = run_sql(read)
    assert state["old-host"] == ("Abuja", ["legacy"])  # site took the existing spelling
    assert state["abj-core"] == ("Abuja", ["backbone"])
    assert state["lag-core"] == ("Lagos Island", [])
    assert state["lag-fw"] == ("Lagos Island", ["firewall", "backbone"])
    assert audits.count("ENDPOINT:BULK_UPDATE") >= 2
    assert audits.count("ENDPOINT:RENAME_LABEL") >= 2


def test_bulk_update_refuses_an_empty_scope(api_client, fleet):
    res = api_client.post("/api/v1/endpoints/bulk-update", json={"filter": {}, "site": "Abuja"})
    assert res.status_code == 400


def test_migration_kept_locations_and_defaulted_tags(run_sql):
    """Rows inserted the pre-0023 way (no site, no tags) read back with empty tags."""
    async def check(conn):
        await conn.execute(
            "INSERT INTO endpoints (ip_address, hostname, device_type, location, endpoint_status) "
            "VALUES ('10.9.2.1', 'pre', 'Router', 'HQ', 'ACTIVE')"
        )
        return await conn.fetchrow("SELECT site, location, tags FROM endpoints WHERE hostname = 'pre'")

    row = run_sql(check)
    assert row["site"] is None and row["location"] == "HQ" and list(row["tags"]) == []
