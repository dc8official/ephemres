"""ICMP echo probing for the monitoring engine, written against the standard library only.

run_ping_cycle tries three ways to ping, in order:
1. one raw socket shared by every probe (needs CAP_NET_RAW, which ephemres-engine.service grants);
2. an unprivileged ICMP datagram socket, which Linux allows for groups in net.ipv4.ping_group_range;
3. the system ping command, parsed from its output.
classify_ping_result then turns a cycle's result into the UP/DOWN states.
"""
from __future__ import annotations

import array
import asyncio
from dataclasses import dataclass
import errno
import ipaddress
import logging
import os
import re
import socket
import struct
import sys
import time
from typing import Dict, List, Optional, Tuple
import weakref

from monitoring.sockbuf import set_receive_buffer

logger = logging.getLogger(__name__)

# RFC 792 (ICMPv4) & RFC 4443 (ICMPv6) constants
ICMP_ECHO_REPLY = 0
ICMP_ECHO_REQUEST = 8
ICMP_TIME_EXCEEDED = 11

ICMPV6_ECHO_REQUEST = 128
ICMPV6_ECHO_REPLY = 129
ICMPV6_TIME_EXCEEDED = 3


@dataclass
class PingResult:
    success_count: int
    failed_count: int
    avg_rtt_ms: Optional[float]
    # Per-cycle spread, None when no reply arrived or the individual RTTs are unknown.
    min_rtt_ms: Optional[float] = None
    max_rtt_ms: Optional[float] = None
    # Mean absolute difference between consecutive replies (RFC 3550 style, unsmoothed).
    jitter_ms: Optional[float] = None
    # TTL the replies arrived with (the most common one in the cycle), v3.3.6. It drops by one
    # per router on the way back, so a change that lasts means the return path changed length
    # (monitoring/ttl_watch.py). Raw IPv4 reads it from the IP header; raw IPv6 (the hop limit)
    # and the unprivileged ping socket, which get no header, ask the kernel for it as ancillary
    # data (enable_reply_ttl). None from the system ping fallback, or if the kernel refuses.
    reply_ttl: Optional[int] = None

    @property
    def health_score(self) -> float:
        total = self.success_count + self.failed_count
        if total == 0:
            return 0.0
        return round((self.success_count / total) * 100.0, 2)

    @property
    def total_count(self) -> int:
        return self.success_count + self.failed_count


def _most_common(values: List[int]) -> Optional[int]:
    if not values:
        return None
    return max(set(values), key=values.count)


def result_from_rtts(rtts: List[float], count: int, ttls: Optional[List[int]] = None) -> PingResult:
    """Build a cycle result from the RTTs (ms) of the replies that arrived out of `count` probes,
    and the TTLs they arrived with when the socket shows them."""
    success_count = len(rtts)
    failed_count = max(0, count - success_count)
    if success_count == 0:
        return PingResult(success_count=0, failed_count=failed_count, avg_rtt_ms=None)
    jitter = None
    if success_count > 1:
        jitter = round(sum(abs(b - a) for a, b in zip(rtts, rtts[1:])) / (success_count - 1), 3)
    return PingResult(
        success_count=success_count,
        failed_count=failed_count,
        avg_rtt_ms=round(sum(rtts) / success_count, 3),
        min_rtt_ms=round(min(rtts), 3),
        max_rtt_ms=round(max(rtts), 3),
        jitter_ms=jitter,
        reply_ttl=_most_common(list(ttls or [])),
    )


# At most 25 ping subprocesses at once, so a fleet on the last fallback cannot start thousands of
# processes in the same second.
_subprocess_ping_semaphore = asyncio.Semaphore(25)

# The raw-socket fallback is per cycle; log it once instead of once per endpoint per minute.
_raw_fallback_logged = False

# ICMP echo identifier, a new one for each endpoint's cycle. A raw socket receives every ICMP
# packet the host gets, including replies to other programs' pings and late replies to our own
# earlier cycles, and (ident, seq) is how a reply finds its probe. Starting from the process ID
# makes two engine workers unlikely to start on the same numbers, but they do overlap as the
# counters advance; the source address and timestamp checks in _handle_incoming_packet catch
# a reply that matches the wrong probe.
_ident_counter = os.getpid() & 0xFFFF


# Room for one int of ancillary data (the TTL or hop limit) per received datagram.
_TTL_CMSG_SPACE = socket.CMSG_SPACE(struct.calcsize("i"))
# Not every Python build exports IP_RECVTTL (3.12 here doesn't, 3.14 does); 12 is its Linux value
# (<linux/in.h>). Off Linux, without the constant, IPv4 ping-socket replies have no TTL.
_IP_RECVTTL = getattr(socket, "IP_RECVTTL", 12 if sys.platform.startswith("linux") else None)


def enable_reply_ttl(sock: socket.socket, is_ipv6: bool) -> bool:
    """Asks the kernel to hand over each reply's TTL (IPv4) or hop limit (IPv6) as ancillary
    data, for sockets that receive no IP header: raw ICMPv6, and the unprivileged ping socket in
    both families. Until v3.3.6 only raw IPv4 replies had a TTL, so the TTL route trigger never ran
    for IPv6 endpoints or on servers without CAP_NET_RAW. False if the kernel refuses."""
    try:
        if is_ipv6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_RECVHOPLIMIT, 1)
        else:
            if _IP_RECVTTL is None:
                return False
            sock.setsockopt(socket.IPPROTO_IP, _IP_RECVTTL, 1)
        return True
    except (OSError, AttributeError):
        return False


def ttl_from_ancdata(ancdata: List[Tuple[int, int, bytes]]) -> Optional[int]:
    """The TTL / hop limit from recvmsg's ancillary data, or None if it isn't there."""
    for level, kind, data in ancdata or ():
        if (level == socket.IPPROTO_IP and kind == socket.IP_TTL) or (
            level == socket.IPPROTO_IPV6 and kind == socket.IPV6_HOPLIMIT
        ):
            if len(data) >= struct.calcsize("i"):
                return struct.unpack("i", data[: struct.calcsize("i")])[0]
            if data:
                return data[0]  # some kernels pass a single byte for IP_TTL
    return None


def _next_ident() -> int:
    """The next identifier: wraps at 16 bits (the size of the field) and skips 0."""
    global _ident_counter
    _ident_counter = (_ident_counter + 1) & 0xFFFF
    if _ident_counter == 0:
        _ident_counter = 1
    return _ident_counter


def icmp_checksum(data: bytes | bytearray | memoryview) -> int:
    """
    RFC 1071 16-bit one's complement checksum over bytes, bytearray, or memoryview.
    Returns a 16-bit unsigned integer in network byte order (big-endian).
    """
    mv = memoryview(data)
    length = len(mv)
    even_len = length & ~1

    total = 0
    if even_len > 0:
        # Sum the data as 16-bit words in network (big-endian) order. On a little-endian CPU the
        # array reads each word byte-swapped, so swap them back first.
        words = array.array("H")
        words.frombytes(mv[:even_len])
        if sys.byteorder == "little":
            words.byteswap()
        total += sum(words)

    # An odd last byte counts as a word padded with a zero byte on the right.
    if length & 1:
        total += mv[-1] << 8

    # Fold the carries back into the low 16 bits, then take the one's complement.
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)

    return ~total & 0xFFFF


def build_icmp_echo_packet(
    ident: int,
    seq: int,
    send_ns: Optional[int] = None,
    is_ipv6: bool = False,
    payload_len: int = 48,
) -> bytearray:
    """
    Packs an RFC 792 (ICMPv4) or RFC 4443 (ICMPv6) Echo Request packet into a bytearray
    using struct.pack_into("!BBHHH", ...) with an embedded 64-bit monotonic nanosecond timestamp.
    """
    if send_ns is None:
        send_ns = time.monotonic_ns()
    # The payload must at least hold the 8-byte timestamp.
    eff_payload_len = max(8, payload_len)
    buf = bytearray(8 + eff_payload_len)
    icmp_type = ICMPV6_ECHO_REQUEST if is_ipv6 else ICMP_ECHO_REQUEST

    # 8-byte header: type, code, checksum (0 while it is computed), identifier, sequence.
    struct.pack_into("!BBHHH", buf, 0, icmp_type, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    # The send time travels in the payload and comes back in the reply, so a reply can be matched
    # to the exact probe that caused it, not only to its (ident, seq).
    struct.pack_into("!Q", buf, 8, send_ns & 0xFFFFFFFFFFFFFFFF)
    # Fill remaining payload bytes with deterministic pattern
    for i in range(16, 8 + eff_payload_len):
        buf[i] = i & 0xFF

    csum = icmp_checksum(buf)
    struct.pack_into("!H", buf, 2, csum)
    return buf


# Older name, still imported by the tests.
_pack_icmp_echo = build_icmp_echo_packet


def parse_icmp_echo_reply(
    buf: bytes | bytearray | memoryview,
    nbytes: int,
    *,
    is_ipv6: bool = False,
    is_dgram: bool = False,
) -> Optional[Tuple[int, int, Optional[int]]]:
    """
    Parses an incoming packet buffer and extracts (ident, seq, embedded_send_ns)
    if the packet is a valid Echo Reply (Type 0 for IPv4, Type 129 for IPv6).
    Returns None if the packet is not an Echo Reply or is truncated.
    """
    mv = memoryview(buf)[:nbytes]
    offset = 0
    # A raw IPv4 socket hands over the IP header in front of the ICMP message. Its length is the
    # IHL field, in 32-bit words; 28 bytes is the smallest IP header (20) plus the ICMP header (8).
    # Raw IPv6 and datagram sockets give the ICMP message alone.
    if not is_ipv6 and not is_dgram:
        if nbytes >= 28 and (mv[0] >> 4) == 4:
            ihl = (mv[0] & 0x0F) * 4
            if ihl < 20 or nbytes < ihl + 8:
                return None
            offset = ihl
        elif nbytes >= 8 and mv[0] == 0:
            # No IP header: the buffer already starts with an echo reply (type 0).
            offset = 0
        else:
            return None
    else:
        if nbytes < 8:
            return None

    icmp_type, icmp_code, _csum, r_ident, r_seq = struct.unpack_from("!BBHHH", mv, offset)
    expected_type = ICMPV6_ECHO_REPLY if is_ipv6 else ICMP_ECHO_REPLY
    if icmp_type != expected_type or icmp_code != 0:
        return None

    embedded_send_ns: Optional[int] = None
    if nbytes >= offset + 16:
        (embedded_send_ns,) = struct.unpack_from("!Q", mv, offset + 8)

    return r_ident, r_seq, embedded_send_ns


class _RawICMPMultiplexer:
    """One raw ICMP socket per event loop and address family, shared by every endpoint's probes.

    A socket per probe would mean thousands of open sockets on a large fleet. Instead each probe
    registers a future under its (ident, seq), the event loop calls _on_readable when replies
    arrive, and each reply completes the matching future. The 1500-byte receive buffer (one
    Ethernet MTU) is allocated once and reused. The receive time is taken right after the read,
    so a busy event loop adds its own delay to the measured RTT.
    """

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None, is_ipv6: bool = False) -> None:
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
        self.loop = loop
        self.is_ipv6 = is_ipv6
        self._recv_buf = bytearray(1500)
        # (ident, seq) -> (future for the RTT, target IP, send time in ns)
        self._pending: Dict[Tuple[int, int], Tuple[asyncio.Future[float], str, int]] = {}
        # (ident, seq) -> TTL of the reply, for the probes still being collected.
        self._reply_ttls: Dict[Tuple[int, int], int] = {}
        # Raw ICMPv6 gets no IP header: the hop limit comes as ancillary data (enable_reply_ttl).
        self._ancillary_ttl = False
        self._closed = False
        self.sock: Optional[socket.socket] = None

        try:
            if is_ipv6:
                self.sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
            else:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
            set_receive_buffer(self.sock, "ICMPv6 socket" if is_ipv6 else "ICMP socket")
            if is_ipv6:
                self._ancillary_ttl = enable_reply_ttl(self.sock, is_ipv6=True)
            self.sock.setblocking(False)
            self.loop.add_reader(self.sock.fileno(), self._on_readable)
        except (PermissionError, OSError):
            # No CAP_NET_RAW. _run_raw_socket_ping_cycle sees sock is None and falls back.
            self.sock = None

    def _handle_incoming_packet(
        self,
        raw_bytes: bytes | bytearray | memoryview,
        responder_ip: str,
        recv_ns: Optional[int] = None,
        reply_ttl: Optional[int] = None,
    ) -> None:
        parsed = parse_icmp_echo_reply(
            raw_bytes,
            len(raw_bytes),
            is_ipv6=self.is_ipv6,
            is_dgram=False,
        )
        if parsed is None:
            return

        r_ident, r_seq, _embedded_ns = parsed
        key = (r_ident, r_seq)
        entry = self._pending.get(key)
        if entry is None:
            return

        fut, expected_ip, send_ns = entry

        # The raw socket sees replies from every host, and another host's reply can carry the
        # same (ident, seq): it must come from the address we pinged.
        if responder_ip:
            try:
                if ipaddress.ip_address(responder_ip) != ipaddress.ip_address(expected_ip):
                    return
            except ValueError:
                if responder_ip != expected_ip:
                    return

        # A different timestamp means a reply to some other probe that used the same (ident, seq).
        if _embedded_ns is not None and _embedded_ns != 0 and _embedded_ns != send_ns:
            return

        self._pending.pop(key, None)
        # A raw IPv4 read starts with the IP header: byte 8 is the TTL the reply arrived with.
        # Raw IPv6 has no header: the hop limit came as ancillary data (reply_ttl).
        if not self.is_ipv6 and len(raw_bytes) >= 20 and (raw_bytes[0] >> 4) == 4:
            self._reply_ttls[key] = int(raw_bytes[8])
        elif reply_ttl is not None:
            self._reply_ttls[key] = int(reply_ttl)
        if not fut.done():
            if recv_ns is None:
                recv_ns = time.monotonic_ns()
            rtt_ms = max(0.0, (recv_ns - send_ns) / 1_000_000.0)
            fut.set_result(rtt_ms)

    def _on_readable(self) -> None:
        if self.sock is None:
            return
        # Read until the socket is empty, so a burst of replies is handled in one callback.
        while not self._closed:
            reply_ttl = None
            try:
                if self._ancillary_ttl:
                    nbytes, ancdata, _flags, addr = self.sock.recvmsg_into([self._recv_buf], _TTL_CMSG_SPACE)
                    reply_ttl = ttl_from_ancdata(ancdata)
                else:
                    nbytes, addr = self.sock.recvfrom_into(self._recv_buf)
                recv_ns = time.monotonic_ns()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break

            responder_ip = addr[0] if (isinstance(addr, tuple) and addr) else ""
            self._handle_incoming_packet(
                self._recv_buf[:nbytes],
                responder_ip,
                recv_ns=recv_ns,
                reply_ttl=reply_ttl,
            )

    async def send_probe_with_ttl(
        self,
        target_ip: str,
        ident: int,
        seq: int,
        timeout: float,
    ) -> Tuple[Optional[float], Optional[int]]:
        """(RTT in ms, reply TTL) for one probe; (None, None) when no reply came in time."""
        try:
            rtt = await self._send_one(target_ip, ident, seq, timeout)
        finally:
            ttl = self._reply_ttls.pop((ident & 0xFFFF, seq & 0xFFFF), None)
        return rtt, (ttl if rtt is not None else None)

    async def send_probe(
        self,
        target_ip: str,
        ident: int,
        seq: int,
        timeout: float,
    ) -> Optional[float]:
        rtt, _ttl = await self.send_probe_with_ttl(target_ip, ident, seq, timeout)
        return rtt

    async def _send_one(
        self,
        target_ip: str,
        ident: int,
        seq: int,
        timeout: float,
    ) -> Optional[float]:
        key = (ident & 0xFFFF, seq & 0xFFFF)
        fut: asyncio.Future[float] = self.loop.create_future()
        send_ns = time.monotonic_ns()
        pkt = build_icmp_echo_packet(ident, seq, send_ns=send_ns, is_ipv6=self.is_ipv6)
        self._pending[key] = (fut, target_ip, send_ns)

        try:
            dest = (target_ip, 0, 0, 0) if self.is_ipv6 else (target_ip, 0)
            if self.sock is not None:
                self.sock.sendto(pkt, dest)
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, OSError):
            return None
        finally:
            self._pending.pop(key, None)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.sock is not None:
            try:
                self.loop.remove_reader(self.sock.fileno())
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# Weakly keyed by event loop: a socket is registered with one loop and cannot serve another, and
# when a loop is discarded (the tests create many) its entry goes with it.
_raw_multiplexers_v4: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _RawICMPMultiplexer] = (
    weakref.WeakKeyDictionary()
)
_raw_multiplexers_v6: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _RawICMPMultiplexer] = (
    weakref.WeakKeyDictionary()
)


def _get_raw_multiplexer(loop: asyncio.AbstractEventLoop, is_ipv6: bool) -> _RawICMPMultiplexer:
    store = _raw_multiplexers_v6 if is_ipv6 else _raw_multiplexers_v4
    mux = store.get(loop)
    if mux is None or mux._closed:
        mux = _RawICMPMultiplexer(loop, is_ipv6=is_ipv6)
        store[loop] = mux
    return mux


async def _run_raw_socket_ping_cycle(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float,
    is_ipv6: bool,
) -> PingResult:
    """Tier 1: Multiplexed singleton SOCK_RAW ICMP ping cycle."""
    loop = asyncio.get_running_loop()
    mux = _get_raw_multiplexer(loop, is_ipv6=is_ipv6)
    if mux.sock is None:
        # Without CAP_NET_RAW the socket could not be opened. Raise so run_ping_cycle falls back
        # to the unprivileged socket instead of reporting every probe as lost.
        raise PermissionError("raw ICMP socket unavailable (CAP_NET_RAW missing?)")
    ident = _next_ident()

    rtts: List[float] = []
    ttls: List[int] = []
    for seq in range(1, count + 1):
        rtt, ttl = await mux.send_probe_with_ttl(ip_address, ident, seq, timeout=timeout)
        if rtt is not None:
            rtts.append(rtt)
            if ttl is not None:
                ttls.append(ttl)
        if seq < count and interval > 0:
            await asyncio.sleep(interval)

    return result_from_rtts(rtts, count, ttls)


async def _run_dgram_socket_ping_cycle(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float,
    is_ipv6: bool,
) -> PingResult:
    """Fallback without CAP_NET_RAW: an unprivileged ICMP datagram socket (a Linux "ping socket").

    Linux allows these only for groups in net.ipv4.ping_group_range. The kernel replaces our
    identifier with its own (the socket's local "port", read with getsockname() after the first
    send) and delivers only the replies meant for this socket, without an IP header. One socket
    per cycle, closed at the end.
    """
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    proto = socket.IPPROTO_ICMPV6 if is_ipv6 else socket.IPPROTO_ICMP

    sock = socket.socket(family, socket.SOCK_DGRAM, proto)
    sock.setblocking(False)
    # The ping socket gets no IP header either: the TTL / hop limit comes as ancillary data.
    ancillary_ttl = enable_reply_ttl(sock, is_ipv6)

    recv_buf = bytearray(1500)
    pending_seqs: Dict[int, Tuple[int, asyncio.Future[float]]] = {}
    reply_ttls: Dict[int, int] = {}
    kernel_ident: Optional[int] = None

    def _on_dgram_readable() -> None:
        while True:
            reply_ttl = None
            try:
                if ancillary_ttl:
                    nbytes, ancdata, _flags, _addr = sock.recvmsg_into([recv_buf], _TTL_CMSG_SPACE)
                    reply_ttl = ttl_from_ancdata(ancdata)
                else:
                    nbytes, _addr = sock.recvfrom_into(recv_buf)
                recv_ns = time.monotonic_ns()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break

            parsed = parse_icmp_echo_reply(
                recv_buf,
                nbytes,
                is_ipv6=is_ipv6,
                is_dgram=True,
            )
            if parsed is None:
                continue

            r_ident, r_seq, _embedded_ns = parsed
            if kernel_ident is not None and kernel_ident != 0 and r_ident != kernel_ident:
                continue
            entry = pending_seqs.pop(r_seq, None)
            if entry is not None:
                send_ns, fut = entry
                if reply_ttl is not None:
                    reply_ttls[r_seq] = reply_ttl
                if not fut.done():
                    rtt_ms = max(0.0, (recv_ns - send_ns) / 1_000_000.0)
                    fut.set_result(rtt_ms)

    fd = sock.fileno()
    loop.add_reader(fd, _on_dgram_readable)
    rtts: List[float] = []
    ttls: List[int] = []
    try:
        for seq in range(1, count + 1):
            fut: asyncio.Future[float] = loop.create_future()
            send_ns = time.monotonic_ns()
            pkt = build_icmp_echo_packet(kernel_ident or 0, seq, send_ns=send_ns, is_ipv6=is_ipv6)
            pending_seqs[seq] = (send_ns, fut)
            try:
                dest = (ip_address, 0, 0, 0) if is_ipv6 else (ip_address, 0)
                sock.sendto(pkt, dest)
                if kernel_ident is None:
                    try:
                        sock_name = sock.getsockname()
                        if isinstance(sock_name, tuple) and len(sock_name) >= 2:
                            kernel_ident = int(sock_name[1]) & 0xFFFF
                    except OSError:
                        pass
                rtt = await asyncio.wait_for(fut, timeout=timeout)
                rtts.append(rtt)
                if seq in reply_ttls:
                    ttls.append(reply_ttls[seq])
            except (asyncio.TimeoutError, OSError):
                pass
            finally:
                pending_seqs.pop(seq, None)
                reply_ttls.pop(seq, None)

            if seq < count and interval > 0:
                await asyncio.sleep(interval)
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    return result_from_rtts(rtts, count, ttls)


async def run_system_ping_fallback(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float,
) -> PingResult:
    """Last resort when neither socket can be opened: run the system ping command and parse its
    output. The ping binary carries its own permission to send ICMP (setuid or a file capability)."""
    try:
        # Older iputils ping refuses intervals under 0.2 s unless run as root.
        safe_interval = max(0.2, interval)
        # Whole seconds, at least 1: BusyBox and older iputils accept only an integer -W
        # (current iputils also takes fractions).
        reply_wait = int(max(1, timeout))
        # The longest ping can run: every probe waiting out its timeout, plus the gaps between
        # probes, plus 5 s to start the process and print the summary. Leaving the gaps out
        # killed ping -c 5 -i 8 after 15 s, every cycle, and every endpoint read DOWN.
        deadline = count * reply_wait + max(count - 1, 0) * safe_interval + 5.0

        async with _subprocess_ping_semaphore:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c", str(count),
                "-i", str(safe_interval),
                "-W", str(reply_wait),
                "--",
                ip_address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=deadline,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                raise
        stdout_str = stdout.decode("utf-8", errors="ignore")

        # iputils prints "5 packets transmitted, 5 received", BusyBox "... 5 packets received".
        transmitted = 0
        received = 0
        match_stats = re.search(r"(\d+) packets transmitted,\s+(\d+)\s+(packets\s+)?received", stdout_str)
        if match_stats:
            transmitted = int(match_stats.group(1))
            received = int(match_stats.group(2))
        else:
            tx_match = re.search(r"(\d+)\s+packets transmitted", stdout_str)
            rx_match = re.search(r"(\d+)\s+(packets\s+)?received", stdout_str)
            if tx_match:
                transmitted = int(tx_match.group(1))
            if rx_match:
                received = int(rx_match.group(1))

        if transmitted == 0:
            transmitted = count

        failed_count = max(0, transmitted - received)

        # Individual replies ("... time=12.3 ms") give the same per-cycle stats as the socket tiers.
        reply_rtts = [float(m) for m in re.findall(r"time[=<]([\d.]+)\s*ms", stdout_str)]
        if reply_rtts and len(reply_rtts) == received:
            return result_from_rtts(reply_rtts, transmitted)

        # Otherwise only the average from the summary line: "rtt min/avg/max/mdev" on iputils,
        # "round-trip min/avg/max" on BusyBox.
        avg_rtt = None
        match_rtt = re.search(r"rtt\s+min/avg/max/mdev\s+=\s+[\d\.]+/(?P<avg>[\d\.]+)/", stdout_str)
        if match_rtt:
            avg_rtt = float(match_rtt.group("avg"))
        else:
            match_rt = re.search(r"round-trip\s+min/avg/max\s+=\s+[\d\.]+/(?P<avg>[\d\.]+)/", stdout_str)
            if match_rt:
                avg_rtt = float(match_rt.group("avg"))

        return PingResult(
            success_count=received,
            failed_count=failed_count,
            avg_rtt_ms=avg_rtt,
        )

    except Exception as e:
        logger.warning(
            f"System ping fallback failed for {ip_address}: {type(e).__name__}: {e}"
        )
        return PingResult(
            success_count=0,
            failed_count=count,
            avg_rtt_ms=None,
        )


def _resolve_target_ip(address: str) -> Tuple[str, bool]:
    """Resolves target IP or hostname and returns (canonical_ip_str, is_ipv6).

    A hostname takes the first address getaddrinfo returns, IPv4 or IPv6. That lookup blocks the
    event loop while it runs. The engine always passes IP addresses (the endpoints column is inet),
    so in practice it does not happen."""
    cleaned = address.strip()
    try:
        ip_obj = ipaddress.ip_address(cleaned)
        return str(ip_obj), (ip_obj.version == 6)
    except ValueError:
        infos = socket.getaddrinfo(cleaned, None, socket.AF_UNSPEC, socket.SOCK_RAW)
        if not infos:
            raise ValueError(f"Unable to resolve address: {address}")
        family = infos[0][0]
        resolved_ip = infos[0][4][0]
        return str(resolved_ip), (family == socket.AF_INET6)


async def run_ping_cycle(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float = 2.0,
    privileged: bool = True,
) -> PingResult:
    """Ping `ip_address` `count` times, `interval` seconds apart, each probe waiting up to `timeout`
    for its reply, using the first method that works:
    1. the shared raw socket (SOCK_RAW), when privileged=True;
    2. an unprivileged datagram ICMP socket (SOCK_DGRAM);
    3. the system ping command (at most 25 at once).
    """
    try:
        target_ip, is_ipv6 = _resolve_target_ip(ip_address)
    except Exception:
        return await run_system_ping_fallback(
            ip_address=ip_address,
            count=count,
            interval=interval,
            timeout=timeout,
        )

    if privileged:
        try:
            return await _run_raw_socket_ping_cycle(
                ip_address=target_ip,
                count=count,
                interval=interval,
                timeout=timeout,
                is_ipv6=is_ipv6,
            )
        except (PermissionError, OSError) as e:
            global _raw_fallback_logged
            if not _raw_fallback_logged:
                logger.warning(
                    f"Privileged raw socket ping failed for {ip_address} ({type(e).__name__}: {e}). "
                    "Using unprivileged SOCK_DGRAM sockets; this is logged once per process."
                )
                _raw_fallback_logged = True

    try:
        return await _run_dgram_socket_ping_cycle(
            ip_address=target_ip,
            count=count,
            interval=interval,
            timeout=timeout,
            is_ipv6=is_ipv6,
        )
    except (PermissionError, OSError, Exception) as fallback_err:
        logger.info(
            f"Unprivileged SOCK_DGRAM ping failed for {ip_address} ({type(fallback_err).__name__}). Falling back to system subprocess ping..."
        )
        return await run_system_ping_fallback(
            ip_address=ip_address,
            count=count,
            interval=interval,
            timeout=timeout,
        )


def classify_ping_result(
    result: PingResult,
    baseline_mean: Optional[float] = None,
    baseline_stddev: Optional[float] = None,
    k: float = 3.0,
) -> tuple[str, str]:
    """
    Classifies a ping result into operational state ('UP', 'DOWN') and
    detailed state ('UP', 'UP-UNSTABLE', 'DOWN-UNSTABLE', 'DOWN').

    Loss decides first, then RTT against the endpoint's own baseline (mean μ and spread σ of its
    usual RTT). With the default 5 pings:
    - 5/5 received, RTT <= μ + 3σ -> UP / UP
    - 5/5 received, RTT > μ + 3σ -> UP / UP-UNSTABLE
    - 3/5 or 4/5 received (20 to 40 % loss) -> UP / UP-UNSTABLE
    - 1/5 or 2/5 received (60 to 80 % loss) -> DOWN / DOWN-UNSTABLE
    - 0/5 received (100 % loss) -> DOWN / DOWN
    With no baseline (None), 5/5 is always UP. The engine passes None until the endpoint has
    real history (its first 7 days, or an empty hour of the week; services/baseline_service).
    """
    if result.total_count == 0 or result.success_count == 0:
        return "DOWN", "DOWN"

    ratio = result.success_count / result.total_count

    if ratio >= 1.0:
        if baseline_mean is not None and baseline_stddev is not None and result.avg_rtt_ms is not None:
            # The spread is floored at 2 ms: on a very steady link it is tiny, and ordinary
            # sub-millisecond wobble would otherwise read as degraded.
            safe_stddev = max(baseline_stddev, 2.0)
            threshold = baseline_mean + (k * safe_stddev)
            if result.avg_rtt_ms > threshold:
                return "UP", "UP-UNSTABLE"
        return "UP", "UP"
    elif ratio >= 0.6:  # 3/5 or 4/5 (20 to 40 % loss)
        return "UP", "UP-UNSTABLE"
    elif ratio > 0.0:   # 1/5 or 2/5 (60 to 80 % loss)
        return "DOWN", "DOWN-UNSTABLE"
    else:               # 0/5 (100% loss)
        return "DOWN", "DOWN"
