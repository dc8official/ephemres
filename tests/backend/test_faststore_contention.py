"""FastStore when several processes share one file.

D3: a process that opened the file while another held its write lock deleted the database and
its WAL/SHM files and created new ones. The processes already running kept the deleted file,
or the deleted file with the new WAL ("no such table: kv_store"), and one API worker refused
every second sign-in until it was restarted.

D2: every read ran a DELETE of expired keys first, so reads needed SQLite's write lock too.
flowd writes every flow into the same file, and under a flood sign-ins waited 10 s for the
lock and failed with 503. The raw flow stream now has its own file and reads never write.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import sqlite3
import time
from unittest.mock import AsyncMock

import pytest

from app.core import faststore
from app.core.faststore import FastStoreClient, FastStoreError, _SharedSQLiteStore, _get_db_path


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", str(tmp_path / "fs"))
    used = []

    def client(db):
        _SharedSQLiteStore._instances.pop(db, None)
        used.append(db)
        return FastStoreClient(db=db, decode_responses=True)

    yield client
    for db in used:
        inst = _SharedSQLiteStore._instances.pop(db, None)
        if inst is not None and inst._conn is not None:
            inst._conn.close()


def _run(coro):
    return asyncio.run(coro)


# --- D3: never delete a file other processes use ------------------------------------------

def test_locked_database_at_startup_is_waited_for_not_deleted(store_dir, monkeypatch):
    first = store_dir(140)
    _run(first.set("user_sessions:alice", "jti-1"))
    path = _get_db_path(140)
    inode = os.stat(path).st_ino

    real_open = _SharedSQLiteStore._open_connection
    calls = []

    def locked_once(self):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_open(self)

    monkeypatch.setattr(_SharedSQLiteStore, "_open_connection", locked_once)
    second = _SharedSQLiteStore(140)  # another process starting at the same moment

    assert len(calls) == 2
    assert os.stat(path).st_ino == inode
    assert _run(first.get("user_sessions:alice")) == "jti-1"
    row = second.execute_sync(lambda conn: conn.execute("SELECT v FROM kv_store").fetchone())
    assert row[0] == b"jti-1"
    second._conn.close()


def test_lock_that_never_clears_raises_instead_of_purging(store_dir, monkeypatch):
    store_dir(141)
    path = _get_db_path(141)
    monkeypatch.setattr(faststore, "_OPEN_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(
        _SharedSQLiteStore, "_open_connection",
        lambda self: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _SharedSQLiteStore(141)
    assert os.path.exists(path)


def _corrupt(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"this is not a sqlite database" * 100)
    os.chmod(path, 0o600)


def test_corrupt_file_in_use_by_another_process_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("EPHEMRES_FASTSTORE_DIR", str(tmp_path / "fs"))
    path = _get_db_path(142)
    _corrupt(path)
    # Another service has the store open: it holds the shared lock on the sidecar.
    other = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(other, fcntl.LOCK_SH)
    try:
        with pytest.raises(FastStoreError, match="other processes still use it"):
            _SharedSQLiteStore(142)
        with open(path, "rb") as f:
            assert f.read(4) == b"this"
    finally:
        os.close(other)


def test_corrupt_file_nobody_uses_is_recreated(store_dir):
    path = _get_db_path(143)
    _corrupt(path)
    client = store_dir(143)
    _run(client.set("k", "v"))
    assert _run(client.get("k")) == "v"


# --- D2: reads never need the write lock ---------------------------------------------------

def test_reads_answer_while_another_process_holds_the_write_lock(store_dir):
    client = store_dir(144)

    async def prepare():
        await client.set("user_sessions:alice", '["jti-1"]', ex=3600)
        await client.set("lock:203.0.113.7:admin", "1", ex=900)
        await client.sadd("flow:unmatched:set", "192.0.2.1")
        await client.set("gone", "x", ex=1)

    _run(prepare())
    time.sleep(1.1)

    # flowd (or anything else) in the middle of a long write transaction on the same file.
    writer = sqlite3.connect(_get_db_path(144), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        async def reads():
            return (
                await client.get("user_sessions:alice"),
                await client.exists("lock:203.0.113.7:admin"),
                await client.smembers("flow:unmatched:set"),
                await client.ttl("user_sessions:alice"),
                await client.get("gone"),
                sorted(await client.keys("*")),
            )

        t0 = time.monotonic()
        got = _run(reads())
        elapsed = time.monotonic() - t0
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert elapsed < 1.0
    session, locked, unmatched, ttl, gone, keys = got
    assert session == '["jti-1"]'
    assert locked == 1
    assert unmatched == {"192.0.2.1"}
    assert 3500 < ttl <= 3600
    assert gone is None
    assert "gone" not in keys


def test_expired_set_is_hidden_from_reads_and_removed_by_a_later_write(store_dir):
    client = store_dir(145)

    async def scenario():
        await client.sadd("flow:exporter:x:interfaces", "1", "2")
        await client.expire("flow:exporter:x:interfaces", 1)
        await asyncio.sleep(1.1)
        hidden = (
            await client.smembers("flow:exporter:x:interfaces"),
            await client.scard("flow:exporter:x:interfaces"),
            await client.sismember("flow:exporter:x:interfaces", "1"),
            await client.exists("flow:exporter:x:interfaces"),
        )
        # Writing the key again starts a fresh set instead of reviving the expired members.
        await client.sadd("flow:exporter:x:interfaces", "3")
        return hidden, await client.smembers("flow:exporter:x:interfaces")

    hidden, after = _run(scenario())
    assert hidden == (set(), 0, False, 0)
    assert after == {"3"}


def test_expired_counter_restarts_from_one(store_dir):
    client = store_dir(146)

    async def scenario():
        await client.incr("failed:ip:user")
        await client.expire("failed:ip:user", 1)
        await asyncio.sleep(1.1)
        return await client.incr("failed:ip:user")

    assert _run(scenario()) == 1


# --- D1/D2: flowd keeps flows out of FastStore altogether ----------------------------------

def test_collector_feeds_the_aggregator_without_touching_faststore(store_dir):
    """Flows go from the collector to the aggregator in memory, so the
    shared FastStore file sees none of the per-flow write load."""
    import socket
    import struct

    from monitoring.flow.aggregator import FlowAggregator
    from monitoring.flow.collector import FlowCollector

    shared = store_dir(147)
    correlator = AsyncMock()
    correlator.correlate = lambda exp, src, dst: (None, None, None)
    aggregator = FlowAggregator(store_client=shared, correlator=correlator, db_session_factory=AsyncMock(),
                                memory_limit_mb=512)
    collector = FlowCollector(sink=aggregator.ingest)
    hdr = struct.pack("!HHIIIIBBH", 5, 2, 5000, int(time.time()), 0, 0, 0, 0, 0)
    rec = struct.pack(
        "!IIIHHIIIIHHBBBBHHBBH",
        struct.unpack("!I", socket.inet_aton("10.0.0.1"))[0], struct.unpack("!I", socket.inet_aton("10.0.0.2"))[0],
        0, 1, 2, 3, 1500, 3000, 4000, 40000, 443, 0, 0x18, 6, 0, 0, 0, 24, 24, 0,
    )
    collector.handle_packet(hdr + rec + rec, ("192.0.2.1", 2055), 2055)

    assert aggregator.flows_ingested == 2
    assert sum(v[2] for v in aggregator._accumulator.values()) == 2
    assert _run(shared.keys("*")) == []


def test_flowd_has_no_flow_stream():
    import inspect

    from monitoring.flow import aggregator, daemon

    src = inspect.getsource(daemon.run_flow_daemon)
    assert "sink=aggregator.ingest" in src
    assert "await store_client.delete(LEGACY_STREAM_KEY)" in src
    agg_src = inspect.getsource(aggregator)
    for gone in ("xreadgroup", "xack", "xtrim", "xgroup_create"):
        assert gone not in agg_src
