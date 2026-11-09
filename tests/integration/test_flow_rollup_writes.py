"""flowd's minute-rollup writes against real TimescaleDB.

The rollups were written with an executemany, one upsert per row, which held flowd to about
1,900 new conversations a second. They are now one INSERT ... SELECT FROM unnest(arrays)
ON CONFLICT per 5,000 rows.
"""
import asyncio
import datetime
import uuid

import pytest

EXPORTER_ID = uuid.UUID("44444444-0000-0000-0000-0000000000e1")
HOST_ID = uuid.UUID("44444444-0000-0000-0000-0000000000e2")
T0 = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=datetime.timezone.utc).timestamp()


def _flow(i, **kw):
    f = {
        "exporter_ip": "10.0.0.1", "src_ip": f"10.1.{i // 250}.{1 + i % 250}", "dst_ip": "10.9.9.9",
        "src_port": 40000, "dst_port": 443, "protocol": 6, "bytes": 1000, "packets": 2,
        "start_time": T0 + 5, "end_time": T0 + 6, "tcp_flags": 0x10, "tos": 0,
    }
    f.update(kw)
    return f


async def _ingest_and_flush(dsn, batches):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
    from monitoring.flow.aggregator import FlowAggregator

    class Correlator:
        def correlate(self, exporter_ip, src_ip, dst_ip):
            return EXPORTER_ID, None, (HOST_ID if dst_ip == "10.9.9.9" else None)

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
    try:
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        agg = FlowAggregator(store_client=None, correlator=Correlator(), db_session_factory=factory, memory_limit_mb=64)
        for flows in batches:
            agg.ingest(flows)
            await agg.flush_to_timescaledb()
        return agg
    finally:
        await engine.dispose()


@pytest.fixture
def seeded(run_sql):
    async def seed(conn):
        await conn.execute(
            """INSERT INTO endpoints (id, ip_address, hostname, device_type, endpoint_status)
               VALUES ($1, '10.0.0.1', 'edge', 'Router', 'ACTIVE'), ($2, '10.9.9.9', 'app', 'Server', 'ACTIVE')""",
            EXPORTER_ID, HOST_ID,
        )

    run_sql(seed)
    return run_sql


def test_rollups_cross_the_chunk_boundary_and_merge_on_conflict(timescale_dsn, seeded):
    first = [_flow(i) for i in range(6000)]  # more than one 5,000-row statement
    # The same conversations again in the same minute: merged into the stored rows.
    second = [_flow(i, bytes=500, packets=1, tcp_flags=0x02, tos=184) for i in range(10)]
    agg = asyncio.run(_ingest_and_flush(timescale_dsn, [first, second]))

    async def read(conn):
        totals = await conn.fetchrow(
            "SELECT count(*) AS n, sum(flow_count) AS flows, sum(bytes) AS bytes FROM flow_minute_rollups")
        merged = await conn.fetchrow(
            """SELECT bytes, packets, flow_count, tcp_flags, tos, src_endpoint_id, dst_endpoint_id,
                      host(src_ip) AS src, exporter_id
               FROM flow_minute_rollups WHERE src_ip = '10.1.0.1'""")
        return totals, merged

    totals, merged = seeded(read)
    assert totals["n"] == 6000
    assert totals["flows"] == 6010
    assert totals["bytes"] == 6000 * 1000 + 10 * 500
    assert (merged["bytes"], merged["packets"], merged["flow_count"]) == (1500, 3, 2)
    assert merged["tcp_flags"] == 0x12 and merged["tos"] == 184
    assert merged["src_endpoint_id"] is None and merged["dst_endpoint_id"] == HOST_ID
    assert merged["exporter_id"] == EXPORTER_ID
    assert agg.rows_stored == 6010
    assert agg._accumulator == {}
