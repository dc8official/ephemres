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

    @property
    def health_score(self) -> float:
        total = self.success_count + self.failed_count
        if total == 0:
            return 0.0
        return round((self.success_count / total) * 100.0, 2)

    @property
    def total_count(self) -> int:
        return self.success_count + self.failed_count


_subprocess_ping_semaphore = asyncio.Semaphore(25)

# Monotonically incrementing identifier counter to prevent (ident, seq) collisions
_ident_counter = os.getpid() & 0xFFFF


def _next_ident() -> int:
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
        words = array.array("H")
        words.frombytes(mv[:even_len])
        if sys.byteorder == "little":
            words.byteswap()
        total += sum(words)

    if length & 1:
        total += mv[-1] << 8

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
    eff_payload_len = max(8, payload_len)
    buf = bytearray(8 + eff_payload_len)
    icmp_type = ICMPV6_ECHO_REQUEST if is_ipv6 else ICMP_ECHO_REQUEST

    # Pack header with zero checksum first
    struct.pack_into("!BBHHH", buf, 0, icmp_type, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    # Pack monotonic nanosecond timestamp into first 8 bytes of payload
    struct.pack_into("!Q", buf, 8, send_ns & 0xFFFFFFFFFFFFFFFF)
    # Fill remaining payload bytes with deterministic pattern
    for i in range(16, 8 + eff_payload_len):
        buf[i] = i & 0xFF

    csum = icmp_checksum(buf)
    struct.pack_into("!H", buf, 2, csum)
    return buf


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
    if not is_ipv6 and not is_dgram:
        if nbytes < 28:
            return None
        ihl = (mv[0] & 0x0F) * 4
        if ihl < 20 or nbytes < ihl + 8:
            return None
        offset = ihl
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
    """
    Tier 1 Multiplexed Singleton Raw Socket engine per event loop and address family.
    Uses a single non-blocking SOCK_RAW socket registered with loop.add_reader()
    and a pre-allocated 1500-byte receive buffer for zero-jitter RTT measurement.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, is_ipv6: bool = False) -> None:
        self.loop = loop
        self.is_ipv6 = is_ipv6
        self._recv_buf = bytearray(1500)
        self._pending: Dict[Tuple[int, int], Tuple[int, asyncio.Future[float]]] = {}
        self._closed = False

        if is_ipv6:
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)

        self.sock.setblocking(False)
        self.loop.add_reader(self.sock.fileno(), self._on_readable)

    def _on_readable(self) -> None:
        while not self._closed:
            try:
                nbytes, _addr = self.sock.recvfrom_into(self._recv_buf)
                recv_ns = time.monotonic_ns()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break

            parsed = parse_icmp_echo_reply(
                self._recv_buf,
                nbytes,
                is_ipv6=self.is_ipv6,
                is_dgram=False,
            )
            if parsed is None:
                continue

            r_ident, r_seq, _embedded_ns = parsed
            key = (r_ident, r_seq)
            entry = self._pending.pop(key, None)
            if entry is not None:
                send_ns, fut = entry
                if not fut.done():
                    rtt_ms = max(0.0, (recv_ns - send_ns) / 1_000_000.0)
                    fut.set_result(rtt_ms)

    async def send_probe(
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
        self._pending[key] = (send_ns, fut)

        try:
            dest = (target_ip, 0, 0, 0) if self.is_ipv6 else (target_ip, 0)
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
        try:
            self.loop.remove_reader(self.sock.fileno())
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


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
    ident = _next_ident()

    rtts: List[float] = []
    for seq in range(1, count + 1):
        rtt = await mux.send_probe(ip_address, ident, seq, timeout=timeout)
        if rtt is not None:
            rtts.append(rtt)
        if seq < count and interval > 0:
            await asyncio.sleep(interval)

    success_count = len(rtts)
    failed_count = max(0, count - success_count)
    avg_rtt_ms = round(sum(rtts) / success_count, 3) if success_count > 0 else None
    return PingResult(
        success_count=success_count,
        failed_count=failed_count,
        avg_rtt_ms=avg_rtt_ms,
    )


async def _run_dgram_socket_ping_cycle(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float,
    is_ipv6: bool,
) -> PingResult:
    """
    Tier 2: Unprivileged datagram ICMP socket fallback (SOCK_DGRAM, IPPROTO_ICMP / IPPROTO_ICMPV6).
    Registers the non-blocking socket with the event loop and reads kernel-assigned ICMP IDs after sendto().
    """
    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if is_ipv6 else socket.AF_INET
    proto = socket.IPPROTO_ICMPV6 if is_ipv6 else socket.IPPROTO_ICMP

    sock = socket.socket(family, socket.SOCK_DGRAM, proto)
    sock.setblocking(False)

    recv_buf = bytearray(1500)
    pending_seqs: Dict[int, Tuple[int, asyncio.Future[float]]] = {}
    kernel_ident: Optional[int] = None

    def _on_dgram_readable() -> None:
        while True:
            try:
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
                if not fut.done():
                    rtt_ms = max(0.0, (recv_ns - send_ns) / 1_000_000.0)
                    fut.set_result(rtt_ms)

    fd = sock.fileno()
    loop.add_reader(fd, _on_dgram_readable)
    rtts: List[float] = []
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
            except asyncio.TimeoutError:
                pass
            finally:
                pending_seqs.pop(seq, None)

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

    success_count = len(rtts)
    failed_count = max(0, count - success_count)
    avg_rtt_ms = round(sum(rtts) / success_count, 3) if success_count > 0 else None
    return PingResult(
        success_count=success_count,
        failed_count=failed_count,
        avg_rtt_ms=avg_rtt_ms,
    )


async def run_system_ping_fallback(
    ip_address: str,
    count: int,
    interval: float,
    timeout: float,
) -> PingResult:
    """
    Tier 3 Fallback method that spawns the native system 'ping' command as a subprocess.
    This works reliably in standard unprivileged user spaces because the system
    ping binary usually has the SUID capability or raw socket permissions.
    """
    try:
        # non-root users cannot set ping interval < 0.2
        safe_interval = max(0.2, interval)

        async with _subprocess_ping_semaphore:
            proc = await asyncio.create_subprocess_exec(
                "ping",
                "-c", str(count),
                "-i", str(safe_interval),
                "-W", str(int(max(1, timeout))),
                "--",
                ip_address,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
        stdout_str = stdout.decode("utf-8", errors="ignore")

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
    """Resolves target IP or hostname and returns (canonical_ip_str, is_ipv6)."""
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
    """
    Executes an ICMP Echo probe cycle across the 3-tier engine cascade:
    - Tier 1: Multiplexed Singleton Raw Socket (SOCK_RAW) when privileged=True
    - Tier 2: Unprivileged Datagram ICMP Socket (SOCK_DGRAM) fallback
    - Tier 3: Subprocess 'ping' CLI fallback (bounded by 25-process semaphore)
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
            logger.info(
                f"Privileged raw socket ping failed for {ip_address} ({type(e).__name__}). Retrying with unprivileged SOCK_DGRAM socket..."
            )

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

    Uses hybrid adaptive baseline evaluation (Z-score logic) for 5-ping ratio thresholds:
    - 5/5 received + healthy RTT (<= μ + 3σ) ➔ UP / UP
    - 5/5 received + degraded RTT (> μ + 3σ) ➔ UP / UP-UNSTABLE
    - 3/5 or 4/5 received (20–40% loss) ➔ UP / UP-UNSTABLE
    - 1/5 or 2/5 received (60–80% loss) ➔ DOWN / DOWN-UNSTABLE
    - 0/5 received (100% loss) ➔ DOWN / DOWN
    """
    if result.total_count == 0 or result.success_count == 0:
        return "DOWN", "DOWN"

    ratio = result.success_count / result.total_count

    if ratio >= 1.0:
        # Dynamic Z-score baseline evaluation
        if baseline_mean is not None and baseline_stddev is not None and result.avg_rtt_ms is not None:
            safe_stddev = max(baseline_stddev, 2.0)
            threshold = baseline_mean + (k * safe_stddev)
            if result.avg_rtt_ms > threshold:
                return "UP", "UP-UNSTABLE"
        return "UP", "UP"
    elif ratio >= 0.6:  # 3/5 or 4/5 (20–40% loss)
        return "UP", "UP-UNSTABLE"
    elif ratio > 0.0:   # 1/5 or 2/5 (60–80% loss)
        return "DOWN", "DOWN-UNSTABLE"
    else:               # 0/5 (100% loss)
        return "DOWN", "DOWN"
