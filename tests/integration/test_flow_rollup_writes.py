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


async def _ingest_and_flush(dsn, batches, tamper=None):
    from app.core.orm import AsyncSession, async_sessionmaker, create_async_engine
    from monitoring.flow.aggregator import FlowAggregator

    class Correlator:
        def correlate(self, exporter_ip, src_ip, dst_ip):
            return EXPORTER_ID, None, (HOST_ID if dst_ip == "10.9.9.9" else None)

    engine = create_async_engine(dsn.replace("postgresql://", "postgresql+asyncpg://", 1), pool_size=2, max_overflow=0)
    try:
        factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        agg = FlowAggregator(store_client=None, correlator=Correlator(), db_session_factory=factory, memory_limit_mb=64)
        for n, flows in enumerate(batches):
            agg.ingest(flows)
            if tamper is not None:
                tamper(agg, n)  # reach past ingest's own checks, into what is about to be written
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


BIGINT_MAX = 2 ** 63 - 1


def _stored(seeded):
    async def read(conn):
        return [r["src"] for r in await conn.fetch("SELECT host(src_ip) AS src FROM flow_minute_rollups ORDER BY src_ip")]

    return seeded(read)


def test_a_row_the_driver_cannot_send_is_left_out_and_the_rest_stored(timescale_dsn, seeded):
    """First-phase review H1: one value outside BIGINT failed the whole write, the batch was kept
    "for the next flush" and failed again every time, so flowd stored nothing until a restart."""
    def tamper(agg, n):
        if n == 0:
            key = next(k for k in agg._accumulator if k[4] == "10.1.0.3")
            agg._accumulator[key][0] = 2 ** 64  # what many records at the ingest limit could add up to

    agg = asyncio.run(_ingest_and_flush(
        timescale_dsn, [[_flow(i) for i in range(20)], [_flow(100 + i) for i in range(5)]], tamper))

    stored = _stored(seeded)
    assert "10.1.0.3" not in stored and len(stored) == 19 + 5  # the later flush went in too
    assert agg.rows_rejected == 1 and agg.rows_stored == 24
    assert agg._accumulator == {} and agg.last_flush_error is None


def test_a_row_the_database_refuses_is_left_out_and_the_rest_stored(timescale_dsn, seeded):
    """The other way to the same fault: each value fits, but added to the stored row the sum does
    not, and PostgreSQL refuses it inside the transaction ("bigint out of range")."""
    def tamper(agg, n):
        if n == 0:
            key = next(k for k in agg._accumulator if k[4] == "10.1.0.3")
            agg._accumulator[key][0] = BIGINT_MAX - 10

    # The second batch repeats the conversations in the same minute: 10.1.0.3 would pass BIGINT.
    agg = asyncio.run(_ingest_and_flush(
        timescale_dsn, [[_flow(i) for i in range(20)], [_flow(i) for i in range(20)]], tamper))

    async def read(conn):
        return {r["src"]: r["bytes"] for r in await conn.fetch("SELECT host(src_ip) AS src, bytes FROM flow_minute_rollups")}

    by_src = seeded(read)
    assert by_src["10.1.0.3"] == BIGINT_MAX - 10  # its second write was left out
    assert by_src["10.1.0.4"] == 2000 and len(by_src) == 20  # the others merged as usual
    assert agg.rows_rejected == 1 and agg._accumulator == {} and agg.last_flush_error is None


def test_an_interface_row_the_database_refuses_does_not_hold_back_the_flows(timescale_dsn, seeded):
    def tamper(agg, n):
        key = next(k for k in agg._interface_accumulator if k[2] == 7)
        agg._interface_accumulator[key][0] = 2 ** 64

    flows = [_flow(i, input_if=3 + (i % 5), output_if=1) for i in range(20)]
    agg = asyncio.run(_ingest_and_flush(timescale_dsn, [flows], tamper))

    async def read(conn):
        return [r["interface_idx"] for r in await conn.fetch(
            "SELECT interface_idx FROM flow_interface_minute_rollups ORDER BY interface_idx")]

    assert seeded(read) == [1, 3, 4, 5, 6]  # 7 left out
    assert len(_stored(seeded)) == 20
    assert agg.rows_rejected == 1 and agg._interface_accumulator == {} and agg._accumulator == {}


def test_a_database_that_is_away_keeps_every_flow_for_the_next_flush(timescale_dsn, seeded):
    """Not a refusal: nothing may be dropped when the write fails because the database is away."""
    from app.core.orm import DatabaseUnavailableError
    from monitoring.flow.aggregator import FlowAggregator, refused_for_its_data

    assert not refused_for_its_data(DatabaseUnavailableError("connection refused"))
    assert not refused_for_its_data(TimeoutError())
    assert refused_for_its_data(ValueError("invalid input for query argument $9"))

    class Away:
        async def __aenter__(self):
            raise DatabaseUnavailableError("connection refused")

        async def __aexit__(self, *exc):
            return False

    class Correlator:
        def correlate(self, exporter_ip, src_ip, dst_ip):
            return EXPORTER_ID, None, None

    agg = FlowAggregator(store_client=None, correlator=Correlator(), db_session_factory=Away, memory_limit_mb=64)
    agg.ingest([_flow(i) for i in range(5)])
    asyncio.run(agg._flush_rollups())
    assert len(agg._accumulator) == 5 and agg.rows_rejected == 0
    assert "connection refused" in agg.last_flush_error
