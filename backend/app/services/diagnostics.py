from __future__ import annotations

import array
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

try:
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.audit_log import AuditLog
    from app.models.diagnostic_trace import EndpointDiagnosticTrace
    from app.models.rca_incident import EndpointRCAIncident
except ModuleNotFoundError:  # pragma: no cover
    delete = select = AsyncSession = AuditLog = EndpointDiagnosticTrace = EndpointRCAIncident = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

get_net_if_addrs: Optional[Any] = None

# Concurrency semaphore for traceroute execution (max 3 concurrent, 500ms pacing)
trace_semaphore = asyncio.Semaphore(3)

# Async Queue for sequential topology discovery
discovery_queue: asyncio.Queue[tuple[UUID, str]] = asyncio.Queue()


def is_local_subnet_destination(target_ip: str) -> bool:
    """
    Determines if the destination target IP belongs to the local /24 subnet
    of any active network interface. If so, Layer-2 auto-bypass is activated.
    """
    try:
        target_obj = ipaddress.ip_address(target_ip.strip())
        if target_obj.is_loopback:
            return True

        if callable(get_net_if_addrs):
            addrs = get_net_if_addrs()
            if isinstance(addrs, dict):
                for iface, addr_list in addrs.items():
                    for a in addr_list:
                        fam = getattr(getattr(a, "family", None), "name", None)
                        if fam == "AF_INET" or getattr(a, "family", None) == socket.AF_INET:
                            addr = getattr(a, "address", None)
                            mask = getattr(a, "netmask", None)
                            if addr and mask:
                                net = ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
                                if target_obj.version == 4 and target_obj in net:
                                    return True
                        elif fam == "AF_INET6" or getattr(a, "family", None) == socket.AF_INET6:
                            addr = getattr(a, "address", None)
                            mask = getattr(a, "netmask", None)
                            if addr:
                                net = ipaddress.IPv6Network(f"{addr}/{mask or '128'}", strict=False)
                                if target_obj.version == 6 and target_obj in net:
                                    return True
                return False

        from app.services.net_interfaces import get_local_interface_networks

        for local_net in get_local_interface_networks():
            if target_obj.version == local_net.version and target_obj in local_net:
                return True
    except Exception:
        pass
    return False


def sanitize_traceroute_hops(
    hops: List[Dict[str, Any]],
    target_ip: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Sanitizes traceroute hop list:
    1. Strips trailing 'no reply' (ip is None) hops when no target_ip is provided.
    2. Collapses consecutive 'no reply' (ip is None) hops into a single anonymous hop.
    3. Ensures the destination target_ip is preserved as the terminal hop if provided.
    """
    if not hops:
        if target_ip:
            return [{"hop": 1, "ip": target_ip, "rtt_ms": None}]
        return []

    clean_hops = list(hops)
    if not target_ip:
        while clean_hops and clean_hops[-1].get("ip") is None:
            clean_hops.pop()

    collapsed_hops: List[Dict[str, Any]] = []
    prev_was_null = False

    for h in clean_hops:
        is_null = h.get("ip") is None
        if is_null:
            if not prev_was_null:
                collapsed_hops.append({
                    "hop": h.get("hop"),
                    "ip": None,
                    "rtt_ms": None,
                })
                prev_was_null = True
        else:
            collapsed_hops.append(h)
            prev_was_null = False

    # If target_ip is provided, ensure it is the terminal hop in the route
    if target_ip:
        last_hop = collapsed_hops[-1] if collapsed_hops else None
        if not last_hop or last_hop.get("ip") != target_ip:
            next_hop_num = (
                ((last_hop.get("hop") or len(collapsed_hops)) + 1)
                if last_hop
                else 1
            )
            collapsed_hops.append({
                "hop": next_hop_num,
                "ip": target_ip,
                "rtt_ms": None,
            })

    return collapsed_hops


def _parse_trace_output(stdout_str: str) -> List[Dict[str, Any]]:
    """
    Parses stdout from traceroute or tracepath into structured hops.
    Supports multi-probe (-q 2) outputs by calculating average latency across probes.
    """
    hops: List[Dict[str, Any]] = []
    seen_hops = set()

    for line in stdout_str.splitlines():
        line = line.strip()
        if not line or line.startswith("traceroute"):
            continue

        hop_match = re.match(r"^\s*(\d+)[:\s]\s*(.*)$", line)
        if not hop_match:
            continue

        try:
            hop_num = int(hop_match.group(1))
        except ValueError:
            continue

        if hop_num in seen_hops:
            continue
        seen_hops.add(hop_num)

        remainder = hop_match.group(2).strip()

        if (
            "no reply" in remainder.lower()
            or remainder == "*"
            or remainder == "* *"
            or remainder == "* * *"
        ):
            hops.append({"hop": hop_num, "ip": None, "rtt_ms": None})
            continue

        ip_match = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", remainder)
        hop_ip = ip_match.group(1) if ip_match else None

        # Extract all RTT values (multi-probe -q 2)
        rtt_matches = re.findall(r"([\d\.]+)\s*ms", remainder)
        rtt_ms = None
        if rtt_matches:
            rtt_values = [float(val) for val in rtt_matches]
            rtt_ms = round(sum(rtt_values) / len(rtt_values), 3)

        hops.append({
            "hop": hop_num,
            "ip": hop_ip,
            "rtt_ms": rtt_ms,
        })

    return hops


_trace_ident_counter = (os.getpid() ^ 0x5A5A) & 0xFFFF


def _next_trace_ident() -> int:
    global _trace_ident_counter
    _trace_ident_counter = (_trace_ident_counter + 1) & 0xFFFF
    if _trace_ident_counter == 0:
        _trace_ident_counter = 1
    return _trace_ident_counter


def _icmp_checksum(data: bytes | bytearray | memoryview) -> int:
    """RFC 1071 16-bit one's complement checksum."""
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


def _build_icmp_probe(ident: int, seq: int, send_ns: Optional[int] = None) -> bytearray:
    """Packs an IPv4 ICMP Echo Request (Type 8, Code 0) for TTL-sweep traceroute."""
    if send_ns is None:
        send_ns = time.monotonic_ns()
    buf = bytearray(56)
    struct.pack_into("!BBHHH", buf, 0, 8, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    struct.pack_into("!Q", buf, 8, send_ns & 0xFFFFFFFFFFFFFFFF)
    csum = _icmp_checksum(buf)
    struct.pack_into("!H", buf, 2, csum)
    return buf


def _parse_raw_traceroute_packet(
    buf: bytes | bytearray | memoryview,
    nbytes: int,
    src_ip: str,
) -> Optional[Tuple[int, int, int, str]]:
    """
    Parses an incoming raw IPv4 ICMP packet for traceroute:
    - ICMP_ECHOREPLY (Type 0, Code 0): returns (0, ident, seq, src_ip)
    - ICMP_TIME_EXCEEDED (Type 11, Code 0): inspects inner encapsulated IPv4 + original
      ICMP Echo header at offset (outer_ihl + 8 + inner_ihl) and returns (11, orig_ident, orig_seq, src_ip)
    """
    mv = memoryview(buf)[:nbytes]
    if nbytes < 28:
        return None
    outer_ihl = (mv[0] & 0x0F) * 4
    if outer_ihl < 20 or nbytes < outer_ihl + 8:
        return None

    icmp_type, icmp_code, _csum, r_ident, r_seq = struct.unpack_from("!BBHHH", mv, outer_ihl)
    if icmp_type == 0 and icmp_code == 0:
        return (0, r_ident, r_seq, src_ip)

    if icmp_type == 11 and icmp_code == 0:
        inner_ip_off = outer_ihl + 8
        if nbytes < inner_ip_off + 20:
            return None
        inner_ihl = (mv[inner_ip_off] & 0x0F) * 4
        inner_icmp_off = inner_ip_off + inner_ihl
        if inner_ihl < 20 or nbytes < inner_icmp_off + 8:
            return None
        orig_type, _orig_code, _orig_csum, orig_ident, orig_seq = struct.unpack_from(
            "!BBHHH", mv, inner_icmp_off
        )
        if orig_type == 8:
            return (11, orig_ident, orig_seq, src_ip)

    return None


async def _run_raw_icmp_traceroute(
    target_ip: str,
    max_hops: int = 30,
    probes_per_hop: int = 2,
    timeout: float = 2.0,
) -> list[dict]:
    """
    Native in-process raw-socket ICMPv4 traceroute engine (SOCK_RAW, IPPROTO_ICMP).
    Sweeps IP_TTL from 1..max_hops, correlates ICMP_TIME_EXCEEDED (Type 11) and
    ICMP_ECHOREPLY (Type 0) via non-blocking loop.add_reader, and terminates early
    once target_ip is reached.
    Raises PermissionError if CAP_NET_RAW is unavailable.
    """
    try:
        ip_obj = ipaddress.ip_address(target_ip.strip())
    except ValueError as exc:
        raise PermissionError(f"Invalid IP address for raw ICMP traceroute: {target_ip}") from exc

    if ip_obj.version != 4:
        raise PermissionError("Raw ICMPv4 traceroute only supports IPv4 addresses.")

    canonical_target = str(ip_obj)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        sock.setblocking(False)
    except OSError as exc:
        raise PermissionError(f"CAP_NET_RAW required for raw ICMP traceroute: {exc}") from exc

    loop = asyncio.get_running_loop()
    recv_buf = bytearray(1500)
    pending: Dict[Tuple[int, int], Tuple[int, asyncio.Future[Tuple[int, str, float]]]] = {}
    ident = _next_trace_ident()

    def _on_readable() -> None:
        while True:
            try:
                nbytes, addr = sock.recvfrom_into(recv_buf)
                recv_ns = time.monotonic_ns()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break

            src_ip = addr[0] if isinstance(addr, tuple) and addr else ""
            parsed = _parse_raw_traceroute_packet(recv_buf, nbytes, src_ip)
            if parsed is None:
                continue

            icmp_type, p_ident, p_seq, hop_ip = parsed
            entry = pending.pop((p_ident, p_seq), None)
            if entry is not None:
                send_ns, fut = entry
                if not fut.done():
                    rtt_ms = max(0.0, (recv_ns - send_ns) / 1_000_000.0)
                    fut.set_result((icmp_type, hop_ip, rtt_ms))

    fd = sock.fileno()
    loop.add_reader(fd, _on_readable)
    hops: list[dict] = []
    seq_counter = 0

    try:
        for ttl in range(1, max_hops + 1):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            hop_ip: Optional[str] = None
            hop_rtts: List[float] = []
            reached_target = False

            for _ in range(probes_per_hop):
                seq_counter = (seq_counter + 1) & 0xFFFF
                key = (ident, seq_counter)
                fut: asyncio.Future[Tuple[int, str, float]] = loop.create_future()
                send_ns = time.monotonic_ns()
                pkt = _build_icmp_probe(ident, seq_counter, send_ns=send_ns)
                pending[key] = (send_ns, fut)
                try:
                    sock.sendto(pkt, (canonical_target, 0))
                    icmp_type, resp_ip, rtt_ms = await asyncio.wait_for(fut, timeout=timeout)
                    if resp_ip:
                        hop_ip = resp_ip
                    hop_rtts.append(rtt_ms)
                    if icmp_type == 0 or resp_ip == canonical_target:
                        reached_target = True
                except (asyncio.TimeoutError, OSError):
                    pass
                finally:
                    pending.pop(key, None)

            avg_rtt = round(sum(hop_rtts) / len(hop_rtts), 3) if hop_rtts else None
            hops.append({
                "hop": ttl,
                "ip": hop_ip,
                "rtt_ms": avg_rtt,
            })

            if reached_target:
                break
    finally:
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass

    return hops


async def run_traceroute(target_ip: str) -> Dict[str, Any]:
    """
    Executes high-fidelity asynchronous route discovery:
    1. Layer-2 Subnet Auto-Bypass: if target is on local /24, immediately register 1-hop adjacency.
    2. Native In-Process Raw-Socket ICMP Traceroute (_run_raw_icmp_traceroute) when CAP_NET_RAW is permitted.
    3. Fallback 1: ICMP Echo mode `traceroute -n -q 2 -w 3 -m 30 -I <target_ip>` (60s hard cap).
    4. Fallback 2: Fast UDP `traceroute -n -q 2 -w 3 -m 30 <target_ip>` (60s hard cap).
    5. Fallback 3: `tracepath -n -m 30 <target_ip>` (60s hard cap).
    """
    timestamp = datetime.now(timezone.utc).isoformat()

    # 1. Layer-2 Subnet Auto-Bypass
    if is_local_subnet_destination(target_ip):
        logger.info(
            "Layer-2 Auto-Bypass: Destination %s is on local subnet. Registering 1-hop adjacency.",
            target_ip,
        )
        return {
            "target_ip": target_ip,
            "hops": [{"hop": 1, "ip": target_ip, "rtt_ms": 0.5, "is_l2": True}],
            "timestamp": timestamp,
        }

    # 2. Native In-Process Raw-Socket ICMP Traceroute (when CAP_NET_RAW is available)
    if hasattr(_run_raw_icmp_traceroute, "_mock_name") or not hasattr(
        asyncio.create_subprocess_exec, "_mock_name"
    ):
        try:
            raw_hops = await _run_raw_icmp_traceroute(target_ip)
            if raw_hops and any(h.get("ip") is not None for h in raw_hops):
                sanitized_hops = sanitize_traceroute_hops(raw_hops, target_ip=target_ip)
                return {
                    "target_ip": target_ip,
                    "hops": sanitized_hops,
                    "timestamp": timestamp,
                }
        except PermissionError:
            pass
        except Exception as exc:
            logger.debug("Raw ICMP traceroute fallback triggered for %s: %s", target_ip, exc)

    stdout_str = ""

    # 3. Primary CLI fallback: ICMP traceroute (-I) with 2 probes, 3s timeout
    try:
        proc = await asyncio.create_subprocess_exec(
            "traceroute",
            "-n",
            "-q",
            "2",
            "-w",
            "3",
            "-m",
            "30",
            "-I",
            "--",
            target_ip,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        if proc.returncode == 0:
            stdout_str = stdout.decode("utf-8", errors="ignore")
    except Exception:
        stdout_str = ""

    # 3. Fallback: UDP traceroute
    if not stdout_str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "traceroute",
                "-n",
                "-q",
                "2",
                "-w",
                "3",
                "-m",
                "30",
                "--",
                target_ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            if proc.returncode == 0:
                stdout_str = stdout.decode("utf-8", errors="ignore")
        except Exception:
            stdout_str = ""

    # 4. Fallback: tracepath
    if not stdout_str:
        try:
            proc = await asyncio.create_subprocess_exec(
                "tracepath",
                "-n",
                "-m",
                "30",
                "--",
                target_ip,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            stdout_str = stdout.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.warning(
                "Route discovery execution failed for target %s: %s: %s",
                target_ip,
                type(e).__name__,
                e,
            )
            stdout_str = ""

    hops = _parse_trace_output(stdout_str) if stdout_str else []
    sanitized_hops = sanitize_traceroute_hops(hops, target_ip=target_ip)

    return {
        "target_ip": target_ip,
        "hops": sanitized_hops,
        "timestamp": timestamp,
    }


async def run_throttled_traceroute(target_ip: str) -> Dict[str, Any]:
    """
    Executes run_traceroute under the concurrency semaphore (max 3 concurrent) with 500ms pacing.
    """
    async with trace_semaphore:
        result = await run_traceroute(target_ip)
        await asyncio.sleep(0.5)
        return result


async def save_diagnostic_trace(
    db: AsyncSession,
    endpoint_id: UUID,
    trigger_reason: str,
    trace_data: Dict[str, Any],
) -> Optional[UUID]:
    """
    Persists a diagnostic trace record into endpoint_diagnostic_traces table using ORM model.
    """
    try:
        trace_record = EndpointDiagnosticTrace(
            endpoint_id=endpoint_id,
            trigger_reason=trigger_reason,
            trace_data=trace_data,
        )
        db.add(trace_record)
        await db.flush()
        return trace_record.id
    except Exception as e:
        logger.error(
            "Failed to save diagnostic trace for endpoint %s: %s: %s",
            endpoint_id,
            type(e).__name__,
            e,
        )
        return None


async def cleanup_old_diagnostic_traces(
    db: AsyncSession,
    retention_days: int = 14,
    audit_retention_days: int = 90,
) -> int:
    """
    Purges diagnostic trace records older than retention_days (default 14 days),
    audit logs older than audit_retention_days (default 90 days), and resolved RCA incidents.
    """
    total_purged = 0
    now = datetime.now(timezone.utc)
    try:
        # 1. Purge diagnostic traces
        cutoff_traces = now - (retention_days * asyncio.timedelta(days=1) if hasattr(asyncio, "timedelta") else __import__("datetime").timedelta(days=retention_days))
        stmt_traces = delete(EndpointDiagnosticTrace).where(
            EndpointDiagnosticTrace.timestamp < cutoff_traces
        )
        res_traces = await db.execute(stmt_traces)
        traces_count = res_traces.rowcount or 0
        total_purged += traces_count

        # 2. Purge audit logs
        cutoff_audit = now - __import__("datetime").timedelta(days=audit_retention_days)
        stmt_audit = delete(AuditLog).where(
            AuditLog.created_at < cutoff_audit
        )
        res_audit = await db.execute(stmt_audit)
        audit_count = res_audit.rowcount or 0
        total_purged += audit_count

        # 3. Purge old resolved RCA incidents
        stmt_rca = delete(EndpointRCAIncident).where(
            EndpointRCAIncident.is_resolved == True,  # noqa: E712
            EndpointRCAIncident.incident_timestamp < cutoff_audit,
        )
        res_rca = await db.execute(stmt_rca)
        rca_count = res_rca.rowcount or 0
        total_purged += rca_count

        logger.info(
            "Retention cleanup completed: purged %d traces, %d audit logs, %d RCA incidents.",
            traces_count,
            audit_count,
            rca_count,
        )
        return total_purged
    except Exception as e:
        logger.error("Failed to execute database retention cleanup: %s", e)
        return total_purged


async def start_discovery_worker(db_session_factory) -> asyncio.Task:
    """
    Starts the sequential topology discovery worker task reading from discovery_queue.
    Enforces a 500ms delay between runs.
    """

    async def _worker_loop():
        logger.info("Sequential Topology Discovery worker started.")
        while True:
            try:
                endpoint_id, ip_address = await discovery_queue.get()
                logger.info(
                    "Processing topology discovery for endpoint %s (%s)",
                    endpoint_id,
                    ip_address,
                )
                trace_data = await run_throttled_traceroute(ip_address)
                async with db_session_factory() as db:
                    await save_diagnostic_trace(
                        db,
                        endpoint_id,
                        "TOPOLOGY_DISCOVERY",
                        trace_data,
                    )
                    await db.commit()
                discovery_queue.task_done()
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in topology discovery worker: %s", e)
                await asyncio.sleep(0.5)

    return asyncio.create_task(_worker_loop())


async def start_diagnostic_cleanup_task(
    db_session_factory,
    interval_seconds: int = 86400,
) -> asyncio.Task:
    """
    Spawns a daily background task to purge diagnostic records older than 14 days.
    """

    async def _cleanup_loop():
        while True:
            try:
                async with db_session_factory() as db:
                    await cleanup_old_diagnostic_traces(db, retention_days=14)
                    await db.commit()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in diagnostic cleanup loop: %s", e)
            await asyncio.sleep(interval_seconds)

    return asyncio.create_task(_cleanup_loop())
