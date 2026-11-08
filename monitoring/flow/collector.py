from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import struct
from typing import Any, Callable, Dict, List, Optional

from app.services.runtime_settings import get_runtime_settings
from monitoring.flow.ipfix_parser import IPFIXParser
from monitoring.flow.v5_parser import parse_v5_packet
from monitoring.flow.v9_parser import V9Parser
from monitoring.sockbuf import set_receive_buffer

logger = logging.getLogger(__name__)

# Where flowd buffered flows in FastStore before this release; flowd deletes it on start.
LEGACY_STREAM_KEY = "stream:netflow:raw"


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

    Each datagram is parsed and its flows handed to `sink` (FlowAggregator.ingest) at once, in
    the same process. When flowd can't keep up, the kernel drops datagrams at the socket; those
    drops are read from /proc/net/udp (udp_drops()) and logged by flowd every minute.
    """

    def __init__(
        self,
        sink: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        bind_host: str = "0.0.0.0",
        netflow_port: int = 2055,
        ipfix_port: int = 4739,
        *,
        exporter_filter: Optional[Callable[[str, int], bool]] = None,
    ):
        self.sink = sink
        # Called with (exporter_ip, records in the packet) before a packet is parsed; False drops
        # it. flowd passes FlowCorrelator.admit_exporter, so only enrolled exporters reach the
        # aggregator and the database, and spoofed senders can't fill the v9/IPFIX template caches.
        self.exporter_filter = exporter_filter
        self.bind_host = bind_host
        self.netflow_port = netflow_port
        self.ipfix_port = ipfix_port

        self.v9_parser = V9Parser()
        self.ipfix_parser = IPFIXParser()

        # Counters since start, logged by flowd every minute.
        self.packets_received = 0
        self.flows_received = 0
        self._sockets: List[socket.socket] = []
        self._transports: List[asyncio.DatagramTransport] = []

    def handle_packet(self, data: bytes, addr: tuple[str, int], listen_port: int) -> None:
        self.packets_received += 1
        exporter_ip = addr[0]
        try:
            ip_obj = ipaddress.ip_address(exporter_ip)
            if ip_obj.is_multicast or (ip_obj.is_loopback and str(ip_obj) != "127.0.0.1") or ip_obj.is_unspecified or ip_obj.is_reserved:
                return
        except ValueError:
            return

        if len(data) < 4:
            return

        version = struct.unpack_from("!H", data, 0)[0]
        if self.exporter_filter is not None and version in (5, 9, 10):
            # v5 and v9 headers carry the record count; IPFIX only a length, so count 1 per packet.
            records = struct.unpack_from("!H", data, 2)[0] if version in (5, 9) else 1
            if not self.exporter_filter(exporter_ip, records):
                return
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
            # Admin-configured sampling multiplier for exporters that don't report their own rate
            # (NetFlow v9 / IPFIX, and v5 packets with a zero sampling interval).
            multiplier = get_runtime_settings().flow_sampling_multiplier
            if multiplier > 1:
                for f in flows:
                    if not f.get("header_sampled"):
                        f["bytes"] = int(f.get("bytes", 0) or 0) * multiplier
                        f["packets"] = int(f.get("packets", 0) or 0) * multiplier

            self.flows_received += len(flows)
            if self.sink is not None:
                try:
                    self.sink(flows)
                except Exception as e:
                    logger.error("FlowCollector: could not hand %d flows from %s to the aggregator: %s",
                                 len(flows), exporter_ip, e)

    def udp_drops(self) -> int:
        """Datagrams the kernel dropped on flowd's sockets because they were not read in time."""
        inodes = set()
        for sock in self._sockets:
            try:
                inodes.add(str(os.fstat(sock.fileno()).st_ino))
            except OSError:
                pass
        drops = 0
        for table in ("/proc/net/udp", "/proc/net/udp6"):
            try:
                with open(table) as fh:
                    next(fh, None)
                    for line in fh:
                        fields = line.split()
                        if len(fields) >= 13 and fields[9] in inodes:
                            drops += int(fields[-1])
            except OSError:
                continue
        return drops

    def _create_socket(self, port: int) -> socket.socket:
        # No SO_REUSEADDR: UDP has no TIME_WAIT to work around, and on Linux it would let any other
        # local process that also sets it bind the same port and receive the unicast flow datagrams.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        set_receive_buffer(sock, f"flow socket UDP {port}")
        sock.setblocking(False)
        sock.bind((self.bind_host, port))
        self._sockets.append(sock)
        return sock

    async def start(self) -> None:
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


    async def stop(self) -> None:
        for t in self._transports:
            t.close()
        self._transports.clear()
        self._sockets.clear()
        logger.info("FlowCollector stopped cleanly.")
