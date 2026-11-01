"""Per-cycle probe history (migration 0015) against real TimescaleDB.

Endpoint A gets 100 one-minute samples with rtt_avg_ms = 1..100 ms (5 of 5 replies each) and
one fully lost cycle (0 of 5), so:
- loss = 5 / 505 sent = 0.99 %
- reply-weighted mean RTT = 50.5 ms; p95 of the per-cycle averages = 95.05 ms
Endpoint B has no samples (like a passive exporter) and must not appear in the summary.
"""
import asyncio
import datetime
import uuid

import pytest

HOST_A = uuid.UUID("22222222-0000-0000-0000-0000000000a1")
HOST_B = uuid.UUID("22222222-0000-0000-0000-0000000000b2")


def _seed(run_sql):
    now = datetime.datetime.now(datetime.timezone.utc).replace(second=0, microsecond=0)
    lost_at = now - datetime.timedelta(minutes=101)

    async def seed(conn):
        await conn.execute(
            """
            INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
            VALUES ($1, '10.9.0.1', 'probe-a', 'Server', 'ACTIVE'),
                   ($2, '10.9.0.2', 'probe-b', 'Server', 'ACTIVE')
            """,
            HOST_A, HOST_B,
        )
        rows = [
            (HOST_A, now - datetime.timedelta(minutes=i), 5, 5, i - 0.5, float(i), i + 0.5, 0.25)
            for i in range(1, 101)
        ]
        rows.append((HOST_A, lost_at, 5, 0, None, None, None, None))
        await conn.executemany(
            """
            INSERT INTO endpoint_probe_samples
                (endpoint_id, sampled_at, sent, received, rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            rows,
        )

    run_sql(seed)
    return now


def _data(resp):
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_probe_summary(api_client, run_sql):
    now = _seed(run_sql)
    summary = {s["endpoint_id"]: s for s in _data(api_client.get("/api/v1/endpoints/probe-summary"))}
    assert set(summary) == {str(HOST_A)}

    a = summary[str(HOST_A)]
    assert a["sample_count"] == 101
    assert a["loss_pct"] == pytest.approx(5 / 505 * 100, abs=0.01)
    assert a["rtt_avg_ms"] == pytest.approx(50.5)
    assert a["rtt_p95_ms"] == pytest.approx(95.05)
    assert a["jitter_ms"] == pytest.approx(0.25)
    assert datetime.datetime.fromisoformat(a["last_probe_at"]) == now - datetime.timedelta(minutes=1)

    line = a["rtt_sparkline"]
    assert len(line) == 24
    assert line[-1] is not None or line[-2] is not None  # the latest hour(s) have replies
    assert line[0] is None  # nothing 23 hours ago


def test_probe_series_raw_resolution(api_client, run_sql):
    _seed(run_sql)
    series = _data(api_client.get(f"/api/v1/endpoints/{HOST_A}/probe-series"))
    assert (series["bucket_seconds"], series["source"]) == (60, "endpoint_probe_samples")
    points = series["points"]
    assert len(points) == 101
    lost = points[0]
    assert (lost["sent"], lost["received"], lost["loss_pct"], lost["rtt_avg_ms"]) == (5, 0, 100.0, None)
    latest = points[-1]
    assert (latest["rtt_min_ms"], latest["rtt_avg_ms"], latest["rtt_max_ms"]) == (0.5, 1.0, 1.5)
    assert latest["loss_pct"] == 0.0


def test_probe_series_coarser_resolutions(api_client, run_sql):
    now = _seed(run_sql)
    end = (now + datetime.timedelta(minutes=1)).isoformat()

    week = _data(api_client.get(
        f"/api/v1/endpoints/{HOST_A}/probe-series",
        params={"start_date": (now - datetime.timedelta(days=7)).isoformat(), "end_date": end},
    ))
    assert week["bucket_seconds"] == 600
    assert sum(p["sample_count"] for p in week["points"]) == 101
    assert sum(p["sent"] for p in week["points"]) == 505

    # Older than 8 days: the hourly continuous aggregate. It is real-time, so the fresh samples
    # are already visible without waiting for the refresh policy.
    month = _data(api_client.get(
        f"/api/v1/endpoints/{HOST_A}/probe-series",
        params={"start_date": (now - datetime.timedelta(days=30)).isoformat(), "end_date": end},
    ))
    assert (month["bucket_seconds"], month["source"]) == (3600, "endpoint_probe_hourly")
    assert sum(p["sample_count"] for p in month["points"]) == 101
    assert sum(p["received"] for p in month["points"]) == 500


def test_probe_series_rejects_inverted_range(api_client, run_sql):
    _seed(run_sql)
    resp = api_client.get(
        f"/api/v1/endpoints/{HOST_A}/probe-series",
        params={"start_date": "2026-10-26T12:00:00+00:00", "end_date": "2026-10-26T11:00:00+00:00"},
    )
    assert resp.status_code == 400


def test_probe_storage_policies(run_sql):
    async def fetch(conn):
        hyper = await conn.fetchval(
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'endpoint_probe_samples'"
        )
        cagg = await conn.fetchval(
            "SELECT materialized_only FROM timescaledb_information.continuous_aggregates "
            "WHERE view_name = 'endpoint_probe_hourly'"
        )
        jobs = await conn.fetch(
            "SELECT hypertable_name, proc_name, config FROM timescaledb_information.jobs "
            "WHERE hypertable_name IN ('endpoint_probe_samples') OR proc_name = 'policy_refresh_continuous_aggregate'"
        )
        return hyper, cagg, jobs

    compression, materialized_only, jobs = run_sql(fetch)
    assert compression is True
    assert materialized_only is False
    retention = {j["hypertable_name"]: j["config"] for j in jobs if j["proc_name"] == "policy_retention"}
    assert '"30 days"' in retention["endpoint_probe_samples"]


def test_engine_insert_statement_round_trips(timescale_dsn, run_sql):
    """The engine's INSERT (monitoring/probe_samples.py) works against the real table."""
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
    from monitoring.ping import result_from_rtts
    from monitoring.probe_samples import _INSERT_SAMPLE, sample_params

    _seed(run_sql)
    sampled_at = datetime.datetime(2026, 10, 26, 9, 30, 15, tzinfo=datetime.timezone.utc)
    result = result_from_rtts([10.0, 14.0, 12.0], count=5)

    async def insert_twice():
        engine = create_async_engine(timescale_dsn.replace("postgresql://", "postgresql+asyncpg://", 1))
        try:
            async with async_sessionmaker(bind=engine, class_=AsyncSession)() as db:
                params = sample_params(HOST_B, sampled_at, result)
                await db.execute(_INSERT_SAMPLE, params)
                await db.execute(_INSERT_SAMPLE, params)  # same cycle twice: ignored
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(insert_twice())
    rows = run_sql(lambda conn: conn.fetch(
        "SELECT sent, received, rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms "
        "FROM endpoint_probe_samples WHERE endpoint_id = $1", HOST_B
    ))
    assert len(rows) == 1
    row = rows[0]
    assert (row["sent"], row["received"]) == (5, 3)
    assert (row["rtt_min_ms"], row["rtt_avg_ms"], row["rtt_max_ms"]) == (10.0, 12.0, 14.0)
    assert row["jitter_ms"] == pytest.approx(3.0)  # (|14-10| + |12-14|) / 2
