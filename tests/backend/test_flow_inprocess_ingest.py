"""flowd hands flows from the collector to the aggregator in memory.

Every flow used to be written to a FastStore stream, read back and trimmed. The trim scanned
the whole stream every 100 writes; above about 1,000 flows/s it starved the reader and then
deleted flows nobody had read, without a log line (65 % stored at 1,000 flows/s, 20 % at
1,500). Loss can now only happen in two visible places: the kernel socket buffer when flowd is
too slow, and the memory cap while the database is down. Both are logged every minute.
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
