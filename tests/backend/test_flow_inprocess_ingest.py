"""flowd hands flows from the collector to the aggregator in memory.

Every flow used to be written to a FastStore stream, read back and trimmed. The trim scanned
the whole stream every 100 writes; above about 1,000 flows/s it starved the reader and then
deleted flows nobody had read, without a log line (65 % stored at 1,000 flows/s, 20 % at
1,500). Loss can now only happen in two visible places: the kernel socket buffer when flowd is
too slow, and the memory cap when rollups can't be written fast enough (database down, or more
new conversations than it can take). Both are logged every minute, with the cause.
"""
from __future__ import annotations

import asyncio
import socket
from unittest.mock import MagicMock

from monitoring.flow.aggregator import FlowAggregator
from monitoring.flow.collector import FlowCollector


def _aggregator(**kw) -> FlowAggregator:
    correlator = MagicMock()
    correlator.correlate.return_value = (None, None, None)
    return FlowAggregator(store_client=None, correlator=correlator, memory_limit_mb=64, **kw)


def test_ingest_cleans_up_parser_output_like_the_stream_consumer_did():
    agg = _aggregator()
    agg.ingest([
        {"exporter_ip": "192.0.2.1", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "src_port": 40000,
         "dst_port": 443, "protocol": 6, "bytes": 1500, "packets": 3, "start_time": 1700000000.0,
         "input_if": 4_294_967_295, "output_if": 7, "next_hop": "not-an-ip", "tcp_flags": 0x1FF},
        {"exporter_ip": "192.0.2.1", "src_ip": "", "dst_ip": "10.0.0.2"},           # no source
        {"exporter_ip": "192.0.2.1", "src_ip": "10.0.0.1", "dst_ip": "10.0.0.2", "bytes": "x"},  # malformed
    ])
    assert agg.flows_ingested == 1
    (key, vals), = agg._accumulator.items()
    assert vals[:3] == [1500, 3, 1]
    assert vals[3] == 0xFF  # flags masked to 8 bits


def test_kernel_socket_drops_are_counted():
    collector = FlowCollector(bind_host="127.0.0.1", netflow_port=0)
    sock = collector._create_socket(0)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    port = sock.getsockname()[1]
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert collector.udp_drops() == 0
        for _ in range(500):  # nobody reads: the small buffer overflows
            sender.sendto(b"\x00" * 1200, ("127.0.0.1", port))
        assert collector.udp_drops() > 0
    finally:
        sender.close()
        sock.close()


def test_loss_is_logged_every_minute(monkeypatch):
    from monitoring.flow import daemon

    collector = FlowCollector()
    agg = _aggregator()
    drops = iter([0, 40])
    collector.udp_drops = lambda: next(drops)
    sleeps = []

    async def fake_sleep(seconds):
        if sleeps:
            raise asyncio.CancelledError
        sleeps.append(seconds)
        collector.flows_received = 9000
        agg.rows_stored = 1200
        agg.flows_dropped_memory = 5

    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)
    log = MagicMock()
    monkeypatch.setattr(daemon, "logger", log)
    try:
        asyncio.run(daemon._log_flow_stats(collector, agg))
    except asyncio.CancelledError:
        pass
    assert sleeps == [daemon.STATS_INTERVAL_SECONDS]
    fmt, *args = log.warning.call_args[0]
    msg = fmt % tuple(args)
    assert "9000 flows received" in msg and "LOST 40 datagrams" in msg and "5 flows" in msg


# --- S4: a full buffer names its real cause ---------------------------------------------------
# The buffer filled, and the log said "database down?", also when the database was fine and
# rollup writes simply could not keep up with new conversations (about 15,000 to 20,000 rows/s
# on one TimescaleDB, mostly the upsert and its indexes).


def _fill(agg, n, salt=0):
    agg.ingest([
        {"exporter_ip": "192.0.2.1", "src_ip": f"10.{salt}.{i // 250}.{i % 250}", "dst_ip": "10.255.0.1",
         "dst_port": 443, "protocol": 6, "bytes": 100, "packets": 1, "start_time": 1700000000.0}
        for i in range(n)
    ])


def _run_stats_once(monkeypatch, collector, agg, set_counters):
    from monitoring.flow import daemon

    calls = []

    async def fake_sleep(seconds):
        if calls:
            raise asyncio.CancelledError
        calls.append(seconds)
        set_counters()

    monkeypatch.setattr(daemon.asyncio, "sleep", fake_sleep)
    log = MagicMock()
    monkeypatch.setattr(daemon, "logger", log)
    try:
        asyncio.run(daemon._log_flow_stats(collector, agg))
    except asyncio.CancelledError:
        pass
    return log


def test_full_buffer_while_the_database_refuses_writes_says_so(monkeypatch):
    agg = _aggregator()
    agg.max_accumulator_entries = 10
    agg.last_flush_error = "ConnectionRefusedError: [Errno 111] Connection refused"
    log = MagicMock()
    monkeypatch.setattr("monitoring.flow.aggregator.logger", log)
    _fill(agg, 15)
    assert agg.flows_dropped_memory == 5
    msg = log.error.call_args[0][0] % tuple(log.error.call_args[0][1:])
    assert "database is not accepting rollup writes" in msg and "Connection refused" in msg


def test_full_buffer_while_writes_succeed_blames_throughput_not_the_database(monkeypatch):
    collector = FlowCollector()
    collector.udp_drops = lambda: 0
    agg = _aggregator()
    agg.last_flush_rows, agg.last_flush_seconds = 180_000, 11.2

    def counters():
        collector.flows_received = 900_000
        agg.rows_stored = 600_000
        agg.flows_dropped_memory = 300_000

    log = _run_stats_once(monkeypatch, collector, agg, counters)
    msg = log.warning.call_args[0][0] % tuple(log.warning.call_args[0][1:])
    assert "not keeping up with new conversations" in msg and "180000 rows in 11.2 s" in msg
    assert "database down" not in msg


def test_slow_flushes_are_warned_about_before_anything_is_lost(monkeypatch):
    collector = FlowCollector()
    collector.udp_drops = lambda: 0
    agg = _aggregator()
    agg.last_flush_rows, agg.last_flush_seconds = 150_000, 14.0  # flushes are 10 s apart

    def counters():
        collector.flows_received = 500_000
        agg.rows_stored = 450_000

    log = _run_stats_once(monkeypatch, collector, agg, counters)
    msg = log.warning.call_args[0][0] % tuple(log.warning.call_args[0][1:])
    assert "falling behind" in msg and "150000 rows in 14.0 s" in msg


def test_buffer_full_error_is_logged_once_a_minute_at_most(monkeypatch):
    agg = _aggregator()
    agg.max_accumulator_entries = 5
    log = MagicMock()
    monkeypatch.setattr("monitoring.flow.aggregator.logger", log)
    clock = [1000.0]
    monkeypatch.setattr("monitoring.flow.aggregator.time.monotonic", lambda: clock[0])
    _fill(agg, 20, salt=1)
    clock[0] += 30
    _fill(agg, 20, salt=2)
    assert log.error.call_count == 1
    clock[0] += 31
    _fill(agg, 20, salt=3)
    assert log.error.call_count == 2
