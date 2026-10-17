from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import struct
from typing import Any, List, Optional

from monitoring.flow.ipfix_parser import IPFIXParser
from monitoring.flow.v5_parser import parse_v5_packet
from monitoring.flow.v9_parser import V9Parser

logger = logging.getLogger(__name__)

STREAM_NETFLOW_RAW = "stream:netflow:raw"
MAX_QUEUE_SIZE = 50000


class FlowUDPProtocol(asyncio.DatagramProtocol):
    """
    High-throughput asyncio UDP Datagram Protocol handling incoming NetFlow/IPFIX packets.
    """

    def __init__(self, collector: FlowCollector, port: int):
        self.collector = collector
        self.port = port
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.collector.handle_packet(data, addr, self.port)

    def error_received(self, exc: Exception) -> None:
        logger.warning("FlowUDPProtocol on port %d received error: %s", self.port, exc)


class FlowCollector:
    """
    Dual-port UDP Flow Collector listening on UDP 2055 (NetFlow) and UDP 4739 (IPFIX).
    Buffers and flushes flow records in batches to Redis Stream 'stream:netflow:raw'.
    """

    def __init__(
        self,
        redis_client: Any,
        bind_host: str = "0.0.0.0",
        netflow_port: int = 2055,
        ipfix_port: int = 4739,
        batch_size: int = 1000,
        flush_interval_seconds: float = 0.1,
    ):
        self.redis = redis_client
        self.bind_host = bind_host
        self.netflow_port = netflow_port
        self.ipfix_port = ipfix_port
        self.batch_size = batch_size
        self.flush_interval = flush_interval_seconds

        self.v9_parser = V9Parser()
        self.ipfix_parser = IPFIXParser()

        self._queue: List[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._running = False
        self._flush_task: Optional[asyncio.Task] = None
        self._transports: List[asyncio.DatagramTransport] = []

    def handle_packet(self, data: bytes, addr: tuple[str, int], listen_port: int) -> None:
        exporter_ip = addr[0]
        try:
            ip_obj = ipaddress.ip_address(exporter_ip)
            if ip_obj.is_multicast or (ip_obj.is_loopback and str(ip_obj) != "127.0.0.1") or ip_obj.is_unspecified or ip_obj.is_reserved:
                return
        except ValueError:
            return

        if len(data) < 2:
            return

        version = struct.unpack_from("!H", data, 0)[0]
        flows: List[dict[str, Any]] = []

        try:
            if version == 5:
                flows = parse_v5_packet(data, exporter_ip)
            elif version == 9:
                flows = self.v9_parser.parse_packet(data, exporter_ip)
            elif version == 10:
                flows = self.ipfix_parser.parse_packet(data, exporter_ip)
            else:
                logger.debug("Received unsupported flow version %d from %s", version, exporter_ip)
                return
        except Exception as e:
            logger.debug("Decoding failure for flow packet from %s: %s", exporter_ip, e)
            return

        if flows:
            if len(self._queue) >= MAX_QUEUE_SIZE:
                logger.warning(
                    "FlowCollector: in-memory queue full (%d items). Dropping %d incoming flows due to backpressure.",
                    len(self._queue),
                    len(flows),
                )
                return
            space = MAX_QUEUE_SIZE - len(self._queue)
            if len(flows) > space:
                logger.warning(
                    "FlowCollector: queue near capacity (%d/%d). Accepting %d of %d flows.",
                    len(self._queue),
                    MAX_QUEUE_SIZE,
                    space,
                    len(flows),
                )
                flows = flows[:space]

            self._queue.extend(flows)
            if len(self._queue) >= self.batch_size:
                asyncio.create_task(self.flush())

    async def flush(self) -> None:
        async with self._lock:
            if not self._queue:
                return
            to_flush = self._queue
            self._queue = []

        if not self.redis:
            return

        try:
            pipe = self.redis.pipeline()
            for f in to_flush:
                # Encode dict values as strings for Redis Stream
                stream_entry = {
                    "v": str(f.get("version", 0)),
                    "exp": str(f.get("exporter_ip", "")),
                    "src": str(f.get("src_ip", "")),
                    "dst": str(f.get("dst_ip", "")),
                    "sport": str(f.get("src_port", 0)),
                    "dport": str(f.get("dst_port", 0)),
                    "proto": str(f.get("protocol", 0)),
                    "b": str(f.get("bytes", 0)),
                    "p": str(f.get("packets", 0)),
                    "flags": str(f.get("tcp_flags", 0)),
                    "in_if": str(f.get("input_if", 0)),
                    "out_if": str(f.get("output_if", 0)),
                    "ts_start": f"{f.get('start_time', 0.0):.3f}",
                    "ts_end": f"{f.get('end_time', 0.0):.3f}",
                }
                pipe.xadd(STREAM_NETFLOW_RAW, stream_entry, maxlen=100000, approximate=True)
            await pipe.execute()
        except Exception as e:
            logger.error("Failed to push batch of %d flows to Redis: %s", len(to_flush), e)

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self.flush()

    def _create_socket(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            # Set 4MB socket receive buffer
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except Exception as e:
            logger.warning("Could not set SO_RCVBUF to 4MB: %s", e)
        sock.setblocking(False)
        sock.bind((self.bind_host, port))
        return sock

    async def start(self) -> None:
        self._running = True
        loop = asyncio.get_running_loop()

        # Listen on UDP 2055 (NetFlow)
        try:
            sock_nf = self._create_socket(self.netflow_port)
            transport_nf, _ = await loop.create_datagram_endpoint(
                lambda: FlowUDPProtocol(self, self.netflow_port), sock=sock_nf
            )
            self._transports.append(transport_nf)
            logger.info("FlowCollector: Listening on UDP port %d (NetFlow)", self.netflow_port)
        except Exception as e:
            logger.error("FlowCollector: Failed to bind UDP %d: %s", self.netflow_port, e)

        # Listen on UDP 4739 (IPFIX)
        try:
            sock_ipfix = self._create_socket(self.ipfix_port)
            transport_ipfix, _ = await loop.create_datagram_endpoint(
                lambda: FlowUDPProtocol(self, self.ipfix_port), sock=sock_ipfix
            )
            self._transports.append(transport_ipfix)
            logger.info("FlowCollector: Listening on UDP port %d (IPFIX)", self.ipfix_port)
        except Exception as e:
            logger.error("FlowCollector: Failed to bind UDP %d: %s", self.ipfix_port, e)

        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass

        for t in self._transports:
            t.close()
        self._transports.clear()
        await self.flush()
        logger.info("FlowCollector stopped cleanly.")
