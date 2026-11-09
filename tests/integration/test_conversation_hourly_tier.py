"""Long-window conversation queries on the hourly tier.

Top talkers, conversations, the anomaly summary and QoS used to scan flow_minute_rollups for every
window: with about 1,400 conversations a minute, 24 hours took 26 s and 7 days hit the query
timeout. Windows over two hours now read flow_conversation_hourly_rollups for their whole hours and
the minute table for the partial first hour. The results must match what the minute table says.
"""
import asyncio
import datetime
import os
import uuid

import pytest

R1 = uuid.UUID("55555555-0000-0000-0000-0000000000a1")
R2 = uuid.UUID("55555555-0000-0000-0000-0000000000a2")


def _now_hour():
    return datetime.datetime.now(datetime.timezone.utc).replace(minute=0, second=0, microsecond=0)


def _row(bucket, exporter, src="10.0.0.1", dst="10.9.9.9", port=443, nbytes=1000, flags=0x18, tos=0):
    return (bucket, exporter, src, dst, 6, port, nbytes, 10, 1, flags, tos)


ROUTERS_SQL = """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
    VALUES ($1, '10.0.0.254', 'r1', 'Router', 'ACTIVE'), ($2, '10.0.1.254', 'r2', 'Router', 'ACTIVE')"""
INSERT_SQL = """INSERT INTO flow_minute_rollups (bucket, exporter_id, src_ip, dst_ip, protocol, dst_port,
                                                 bytes, packets, flow_count, tcp_flags, tos)
    VALUES ($1, $2, $3::inet, $4::inet, $5, $6, $7, $8, $9, $10, $11)"""


@pytest.fixture
def load(run_sql):
    async def seed(conn):
        await conn.execute(ROUTERS_SQL, R1, R2)

    run_sql(seed)

    def insert(rows):
        async def go(conn):
            await conn.executemany(INSERT_SQL, rows)

        run_sql(go)

    return insert


def _with_db(dsn, fn):
    async def inner():
        from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine

        engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)() as db:
                return await fn(db)
        finally:
            await engine.dispose()

    return asyncio.run(inner())


def _totals(dsn, start):
    from app.services import conversations as cq

    async def run(db):
        return await cq.query_conversations(db, start, limit=50)

    page = _with_db(dsn, run)
    return {(i.src_ip, i.dst_ip, i.dst_port): (i.total_bytes, i.flow_count) for i in page["items"]}, page["counts"]


def test_short_window_stays_on_the_minute_table():
    from app.services.conversations import build_filters, counted_once_cte

    now = datetime.datetime.now(datetime.timezone.utc)
    where, params = build_filters(now - datetime.timedelta(hours=1))
    cte, out = counted_once_cte(where, params)
    assert "flow_conversation_hourly_rollups" not in cte and "hour_start" not in out

    where, params = build_filters(now - datetime.timedelta(hours=6))
    cte, out = counted_once_cte(where, params)
    assert "flow_conversation_hourly_rollups" in cte and "flow_minute_rollups" in cte
    assert out["hour_start"].minute == 0 and out["hour_start"] >= params["start"]


def test_window_start_is_exact_inside_the_partial_first_hour(timescale_dsn, load):
    """A window starting mid-hour counts only the minutes after its start in that hour."""
    hour = _now_hour() - datetime.timedelta(hours=10)
    rows = [_row(hour + datetime.timedelta(minutes=m), R1) for m in range(60)]         # partial first hour
    rows += [_row(hour + datetime.timedelta(hours=1, minutes=m), R1) for m in range(60)]  # a whole hour
    load(rows)

    items, _ = _totals(timescale_dsn, hour + datetime.timedelta(minutes=45))
    assert items[("10.0.0.1", "10.9.9.9", 443)] == ((15 + 60) * 1000, 15 + 60)


def _two_router_rows(hour):
    rows = []
    for m in range(60):
        when = hour + datetime.timedelta(minutes=m)
        rows.append(_row(when, R1))                              # both routers report it...
        rows.append(_row(when, R2, nbytes=900))                  # ...the second a little lower
        rows.append(_row(when, R1, src="10.0.0.2", nbytes=50))   # one router only
    return rows


def _assert_counted_once(dsn, hour):
    items, counts = _totals(dsn, hour)
    assert items[("10.0.0.1", "10.9.9.9", 443)] == (60 * 1000, 60)
    assert items[("10.0.0.2", "10.9.9.9", 443)] == (60 * 50, 60)
    assert counts["all"] == 2


def test_conversation_seen_by_two_routers_counts_once_on_the_real_time_tier(timescale_dsn, load):
    hour = _now_hour() - datetime.timedelta(hours=5)
    load(_two_router_rows(hour))
    _assert_counted_once(timescale_dsn, hour)


def test_conversation_seen_by_two_routers_counts_once_when_materialized():
    """In a private database: a refresh moves the aggregate's watermark, and rows the other tests
    insert below it would stay hidden until the next refresh."""
    import asyncpg

    from tests.integration.conftest import DSN_ENV, _create_and_migrate, _drop

    admin_dsn = os.environ.get(DSN_ENV)
    if not admin_dsn:
        pytest.skip(f"{DSN_ENV} not set")
    name = f"ephemres_it_{uuid.uuid4().hex[:8]}"
    dsn = asyncio.run(_create_and_migrate(admin_dsn, name))
    hour = _now_hour() - datetime.timedelta(hours=5)

    async def seed_and_refresh():
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(ROUTERS_SQL, R1, R2)
            await conn.executemany(INSERT_SQL, _two_router_rows(hour))
            # The policy job starts right after the migration and refuses a concurrent refresh.
            for attempt in range(30):
                try:
                    await conn.execute(
                        "CALL refresh_continuous_aggregate('flow_conversation_hourly_rollups', NULL, NOW() - INTERVAL '1 hour')"
                    )
                    break
                except asyncpg.exceptions.LockNotAvailableError:
                    if attempt == 29:
                        raise
                    await asyncio.sleep(1)
            return await conn.fetchval("SELECT count(*) FROM flow_conversation_hourly_rollups")
        finally:
            await conn.close()

    try:
        # Per exporter and hour: R1 twice (two conversations), R2 once.
        assert asyncio.run(seed_and_refresh()) == 3
        _assert_counted_once(dsn, hour)
    finally:
        asyncio.run(_drop(admin_dsn, name))


def test_top_talkers_and_qos_read_the_tier_through_the_api(api_client, timescale_dsn, load):
    hour = _now_hour() - datetime.timedelta(hours=20)
    rows = []
    for h in range(10):
        for m in range(0, 60, 10):
            when = hour + datetime.timedelta(hours=h, minutes=m)
            rows.append(_row(when, R1, tos=184))
            rows.append(_row(when, R2, src="10.0.0.2", dst="10.9.9.8", port=80, nbytes=500))
    load(rows)

    talkers = api_client.get("/api/v1/bandwidth/top-talkers", params={"window": "24h"}).json()["data"]
    by_ip = {t["ip_address"]: t for t in talkers["top_endpoints"]}
    assert by_ip["10.0.0.1"]["egress_bytes"] == 60 * 1000
    assert by_ip["10.9.9.9"]["ingress_bytes"] == 60 * 1000
    assert by_ip["10.0.0.2"]["total_bytes"] == 60 * 500

    conv = api_client.get("/api/v1/bandwidth/conversations", params={"window": "7d", "ip": "10.0.0.2"}).json()["data"]
    assert [(c["src_ip"], c["total_bytes"]) for c in conv["items"]] == [("10.0.0.2", 60 * 500)]

    apps = api_client.get("/api/v1/bandwidth/applications", params={"window": "7d"}).json()["data"]
    assert apps["qos_source"] in ("flow_conversation_hourly_rollups", "flow_hourly_rollups")
    assert {c["dscp"]: c["total_bytes"] for c in apps["qos_classes"]} == {46: 60 * 1000, 0: 60 * 500}


def test_query_timeout_answers_504_with_a_clear_message():
    from app.core.asgi import TestClient
    from app.database import get_db
    from app.main import app
    from app.routers.auth import get_current_user

    class TimingOutSession:
        async def execute(self, *a, **kw):
            raise asyncio.TimeoutError()

        async def rollback(self):
            pass

    async def override_db():
        yield TimingOutSession()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid.uuid4()), "role": "ADMIN"}
    try:
        res = TestClient(app).get("/api/v1/bandwidth/top-talkers", params={"window": "7d"})
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
    assert res.status_code == 504
    assert "shorter time window" in res.json()["detail"]


def test_30d_window_keeps_direction_and_zones(api_client, load, run_sql):
    """The 30-day chart read the daily tier, which keeps no endpoints: everything showed as transit
    and traffic zones were unavailable, although the tables covering 30 days have both."""
    app_id = uuid.UUID("55555555-0000-0000-0000-0000000000b1")

    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
               VALUES ($1, '10.9.9.9', 'app', 'Server', 'ACTIVE')""",
            app_id,
        )

    run_sql(seed)
    when = _now_hour() - datetime.timedelta(days=20)

    async def insert(conn):
        await conn.execute(
            """INSERT INTO flow_minute_rollups (bucket, exporter_id, dst_endpoint_id, src_ip, dst_ip, protocol,
                                                dst_port, bytes, packets, flow_count, tcp_flags, tos)
               VALUES ($1, $2, $3, '8.8.8.8', '10.9.9.9', 6, 443, 7000, 7, 1, 24, 0)""",
            when, R1, app_id,
        )

    run_sql(insert)

    series = api_client.get("/api/v1/bandwidth/traffic-series", params={"window": "30d"}).json()["data"]
    assert series["bucket_seconds"] == 3600
    assert series["directional"] is True
    assert sum(p["ingress_bps"] for p in series["points"]) > 0
    assert sum(p["transit_bps"] for p in series["points"]) == 0

    apps = api_client.get("/api/v1/bandwidth/applications", params={"window": "30d"}).json()["data"]
    zones = {z["zone"]: z["total_bytes"] for z in apps["traffic_zones"]}
    assert sum(zones.values()) == 7000
    assert apps["zones_resolution"] == "ip"
