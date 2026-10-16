from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.diagnostic_trace import EndpointDiagnosticTrace
from app.models.rca_incident import EndpointRCAIncident

logger = logging.getLogger(__name__)

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


async def run_traceroute(target_ip: str) -> Dict[str, Any]:
    """
    Executes high-fidelity asynchronous route discovery:
    1. Layer-2 Subnet Auto-Bypass: if target is on local /24, immediately register 1-hop adjacency.
    2. Primary: ICMP Echo mode `traceroute -n -q 2 -w 3 -m 30 -I <target_ip>` (60s hard cap).
    3. Fallback 1: Fast UDP `traceroute -n -q 2 -w 3 -m 30 <target_ip>` (60s hard cap).
    4. Fallback 2: `tracepath -n -m 30 <target_ip>` (60s hard cap).
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

    stdout_str = ""

    # 2. Primary: ICMP traceroute (-I) with 2 probes, 3s timeout
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
