"""Root-cause analysis when an endpoint goes DOWN, and clean-up when it comes back.

Differential RCA traceroutes to the endpoint now and compares the hops with its baseline route:
since v3.3.6 the CURRENT path, the one traffic was using just before the failure
(services/route_paths). When that was not the endpoint's normal path, the summary says so
("On an alternate path since 14:02 UTC (normal path via 172.16.1.1)."). The first hop that no longer answers, or answers from a different
address, is reported as the likely failure point, with the hop before it as the last known good
one. An endpoint on the local segment (no routers in between) is reported as a Layer 2 problem.
The result is stored in endpoint_rca_incidents, with flow evidence when there is some, and
published as RCA_INCIDENT. The traceroute is the verdict; flow data only adds context.
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from app.core.orm import text
from app.core.orm import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.services.baseline_route import refresh_baseline_route
from app.services.flow_paths import flow_evidence
from app.services.diagnostics import run_throttled_traceroute
from app.services.driver_manager import driver_manager

logger = logging.getLogger(__name__)


async def run_differential_rca(
    endpoint_id: UUID,
    db: Optional[AsyncSession] = None,
) -> Optional[Dict[str, Any]]:
    """
    Executes comparative differential Root Cause Analysis (RCA) for a DOWN endpoint.
    Compares live failure traceroute against the last-known-online baseline route.
    """

    async def _execute_rca(session: AsyncSession) -> Optional[Dict[str, Any]]:
        # 1. Fetch endpoint info
        ep_query = text("""
            SELECT id, host(ip_address) AS ip_address, enable_rca, is_l2_segment
            FROM endpoints
            WHERE id = CAST(:endpoint_id AS uuid) AND endpoint_status != 'DELETED'
        """)
        ep_res = await session.execute(ep_query, {"endpoint_id": str(endpoint_id)})
        ep_row = ep_res.fetchone()

        if not ep_row:
            logger.warning("Endpoint %s not found for RCA.", endpoint_id)
            return None

        target_ip = str(ep_row.ip_address)
        enable_rca = bool(ep_row.enable_rca)
        is_l2_segment = bool(ep_row.is_l2_segment)

        if not enable_rca:
            logger.info("RCA disabled for endpoint %s. Skipping.", endpoint_id)
            return None

        is_global = False
        try:
            is_global = ipaddress.ip_address(target_ip).is_global
        except Exception:
            pass

        # 2. Fetch baseline route snapshot
        bl_query = text("""
            SELECT total_hops, hops, normal_hops, current_since
            FROM endpoint_baseline_routes
            WHERE endpoint_id = CAST(:endpoint_id AS uuid)
        """)
        bl_res = await session.execute(bl_query, {"endpoint_id": str(endpoint_id)})
        bl_row = bl_res.fetchone()

        baseline_hops: List[Dict[str, Any]] = []
        if bl_row and bl_row.hops:
            b_hops = bl_row.hops
            if isinstance(b_hops, str):
                try:
                    b_hops = json.loads(b_hops)
                except Exception:
                    b_hops = []
            if isinstance(b_hops, list):
                baseline_hops = b_hops

        # 3. Execute live failure traceroute
        failure_trace = await run_throttled_traceroute(target_ip)
        live_hops: List[Dict[str, Any]] = failure_trace.get("hops", [])

        failed_hop_number: Optional[int] = None
        failed_hop_ip: Optional[str] = None
        last_known_good_hop_ip: Optional[str] = None
        rca_summary: str = ""

        # 4. Evaluation Logic (Layer 2 vs Layer 3)
        if is_l2_segment or (not is_global and len(live_hops) <= 1 and len(baseline_hops) <= 1):
            failed_hop_number = 1
            failed_hop_ip = target_ip
            last_known_good_hop_ip = None
            rca_summary = (
                "Direct Layer 2 Attachment (Same Subnet). "
                "No intermediate L3 routers detected. "
                "Failure isolated to local host, NIC, or local L2 switch port."
            )
            # Traceroute sees nothing on the same subnet, but LLDP/CDP may know the switches in
            # between (v3.3.6 build plan 8.3): name the nearest one that is down too.
            upstream = _l2_upstream_verdict(str(endpoint_id))
            if upstream:
                failed_ip, last_known_good_hop_ip, rca_summary = upstream
                failed_hop_ip = failed_ip or target_ip
        else:
            # Layer 3 Multi-Hop: Compare live failure hops against baseline
            live_hops_by_num = {
                h["hop"]: h for h in live_hops if isinstance(h, dict) and h.get("hop") is not None
            }
            diverged = False

            for idx, b_hop in enumerate(baseline_hops):
                hop_num = b_hop.get("hop", idx + 1)
                b_ip = b_hop.get("ip")

                # Corresponding live hop by physical hop number (fallback to idx)
                l_hop = live_hops_by_num.get(hop_num, live_hops[idx] if idx < len(live_hops) else None)
                l_ip = l_hop.get("ip") if l_hop else None

                # Divergence / Timeout check
                if l_hop is None or l_ip is None or l_ip != b_ip:
                    failed_hop_number = hop_num
                    failed_hop_ip = b_ip or target_ip
                    if idx > 0 and baseline_hops[idx - 1].get("ip"):
                        last_known_good_hop_ip = baseline_hops[idx - 1].get("ip")
                        rca_summary = (
                            f"Transit failure at Hop {hop_num} ({failed_hop_ip}). "
                            f"Path confirmed operational through Hop {hop_num - 1} ({last_known_good_hop_ip})."
                        )
                    else:
                        last_known_good_hop_ip = None
                        if is_global:
                            rca_summary = (
                                f"Remote L3 Routed Destination ({target_ip}). "
                                f"Failure at Hop {hop_num} ({failed_hop_ip}). Path blocked across WAN/carrier network."
                            )
                        else:
                            rca_summary = (
                                f"Transit failure at Hop {hop_num} ({failed_hop_ip}). "
                                f"No prior operational hop."
                            )
                    diverged = True
                    break

            if not diverged:
                last_b_hop = baseline_hops[-1] if baseline_hops else {"hop": 1, "ip": target_ip}
                failed_hop_number = last_b_hop.get("hop", 1)
                failed_hop_ip = last_b_hop.get("ip", target_ip) or target_ip
                if len(baseline_hops) > 1 and baseline_hops[-2].get("ip"):
                    last_known_good_hop_ip = baseline_hops[-2].get("ip")
                    rca_summary = (
                        f"Failure at destination endpoint {failed_hop_ip} (Hop {failed_hop_number}). "
                        f"Path confirmed operational through Hop {failed_hop_number - 1} ({last_known_good_hop_ip})."
                    )
                else:
                    last_known_good_hop_ip = None
                    if is_global:
                        rca_summary = (
                            f"Remote L3 Routed Destination ({target_ip}). "
                            "Failure isolated to destination host or intermediate WAN carrier path."
                        )
                    else:
                        rca_summary = f"Failure at Hop {failed_hop_number} ({failed_hop_ip})."

        # The comparison above was against the current path. If that was a failover path, say
        # so: the operator should know the endpoint was already off its normal route.
        try:
            from app.services.route_paths import alternate_path_note, as_hops

            note = alternate_path_note(
                baseline_hops,
                as_hops(getattr(bl_row, "normal_hops", None)) if bl_row else [],
                getattr(bl_row, "current_since", None) if bl_row else None,
            )
        except Exception:
            note = None
        if note:
            rca_summary = f"{rca_summary} {note}".strip()

        # 5. Persist RCA Incident Record
        insert_incident_sql = text("""
            INSERT INTO endpoint_rca_incidents (
                endpoint_id,
                incident_timestamp,
                status_at_execution,
                failed_hop_number,
                failed_hop_ip,
                last_known_good_hop_ip,
                rca_summary,
                baseline_snapshot,
                failure_trace_snapshot,
                is_resolved
            ) VALUES (
                CAST(:endpoint_id AS uuid),
                NOW(),
                'DOWN',
                :failed_hop_number,
                :failed_hop_ip,
                :last_known_good_hop_ip,
                :rca_summary,
                :baseline_snapshot,
                :failure_trace_snapshot,
                FALSE
            ) RETURNING id
        """)

        res = await session.execute(
            insert_incident_sql,
            {
                "endpoint_id": str(endpoint_id),
                "failed_hop_number": failed_hop_number,
                "failed_hop_ip": failed_hop_ip,
                "last_known_good_hop_ip": last_known_good_hop_ip,
                "rca_summary": rca_summary,
                "baseline_snapshot": json.dumps(baseline_hops),
                "failure_trace_snapshot": json.dumps(live_hops),
            },
        )
        row = res.fetchone() if hasattr(res, "fetchone") else None
        incident_id = None
        if row:
            val = getattr(row, "id", None)
            if val is None and len(row) > 0:
                val = row[0]
            incident_id = str(val) if val else None

        logger.info(
            "Recorded RCA incident %s for endpoint %s: %s",
            incident_id,
            endpoint_id,
            rca_summary,
        )

        # Query downstream symptom endpoints
        symptom_endpoint_ids = []
        try:
            sym_query = text("""
                SELECT id FROM endpoints
                WHERE manual_parent_id = CAST(:endpoint_id AS uuid)
                  AND endpoint_status != 'DELETED'
            """)
            sym_res = await session.execute(sym_query, {"endpoint_id": str(endpoint_id)})
            if sym_res:
                rows = sym_res.fetchall() if hasattr(sym_res, "fetchall") else []
                for r in rows:
                    val = getattr(r, "id", None)
                    if val is None and len(r) > 0:
                        val = r[0]
                    if val:
                        symptom_endpoint_ids.append(str(val))
        except (Exception, StopIteration, StopAsyncIteration) as exc:
            logger.debug("Could not query symptom endpoints: %s", exc)
        # Endpoints placed under this one by LLDP/CDP are its symptoms too (v3.3.6).
        try:
            from app.services.topology import topology_manager

            for child in topology_manager.l2_children(str(endpoint_id)):
                if child not in symptom_endpoint_ids:
                    symptom_endpoint_ids.append(child)
        except Exception as exc:
            logger.debug("Could not read LLDP/CDP children: %s", exc)

        # Flow evidence, added after the incident is recorded (traceroute stays the verdict):
        # which path real traffic last took and whether it still flowed after probes started
        # failing (probes fail for the confirmation cycles before the RCA runs). A savepoint keeps
        # a failed evidence query from aborting the incident.
        evidence: Optional[Dict[str, Any]] = None
        if incident_id:
            try:
                await session.execute(text("SAVEPOINT rca_flow_evidence"))
                try:
                    monitoring_cfg = getattr(settings, "monitoring", None)
                    failing_seconds = int(getattr(monitoring_cfg, "interval_seconds", 60)) * int(
                        getattr(monitoring_cfg, "state_confirmation_cycles", 3)
                    )
                    now = datetime.datetime.now(datetime.timezone.utc)
                    evidence = await flow_evidence(
                        session, endpoint_id, now - datetime.timedelta(seconds=failing_seconds), now=now
                    )
                    if evidence is not None:
                        await session.execute(
                            text("""
                                UPDATE endpoint_rca_incidents SET flow_evidence = CAST(:evidence AS jsonb)
                                WHERE id = CAST(:incident_id AS uuid)
                            """),
                            {"evidence": json.dumps(evidence), "incident_id": incident_id},
                        )
                    await session.execute(text("RELEASE SAVEPOINT rca_flow_evidence"))
                except Exception as exc:
                    evidence = None
                    await session.execute(text("ROLLBACK TO SAVEPOINT rca_flow_evidence"))
                    logger.debug("Flow evidence unavailable for RCA of %s: %s", endpoint_id, exc)
            except Exception as exc:
                evidence = None
                logger.debug("Could not record RCA flow evidence for %s: %s", endpoint_id, exc)

        # Publish RCA_INCIDENT event to event broker
        try:
            broker = driver_manager.get_event_broker()
            if broker:
                rca_payload = {
                    "event_type": "RCA_INCIDENT",
                    "incident_id": incident_id,
                    "root_cause_endpoint_id": str(endpoint_id),
                    "symptom_endpoint_ids": symptom_endpoint_ids,
                    "failed_hop_number": failed_hop_number,
                    "failed_hop_ip": failed_hop_ip,
                    "last_known_good_hop_ip": last_known_good_hop_ip,
                    "rca_summary": rca_summary,
                }
                await broker.publish("RCA_INCIDENT", rca_payload)
        except Exception as exc:
            logger.warning("Failed to publish RCA_INCIDENT event: %s", exc)

        return {
            "incident_id": incident_id,
            "endpoint_id": str(endpoint_id),
            "root_cause_endpoint_id": str(endpoint_id),
            "symptom_endpoint_ids": symptom_endpoint_ids,
            "failed_hop_number": failed_hop_number,
            "failed_hop_ip": failed_hop_ip,
            "last_known_good_hop_ip": last_known_good_hop_ip,
            "rca_summary": rca_summary,
            "baseline_snapshot": baseline_hops,
            "failure_trace_snapshot": live_hops,
            "flow_evidence": evidence,
        }

    if db is not None:
        return await _execute_rca(db)
    else:
        async with AsyncSessionLocal() as session:
            result = await _execute_rca(session)
            await session.commit()
            return result


_confirm_tasks: set = set()


def _l2_upstream_verdict(endpoint_id: str) -> Optional[tuple]:
    """(failed IP, last good IP, summary) from the endpoint's LLDP/CDP parents on the map, or
    None when it has none. The nearest parent that is down is the likely cause; a parent that
    answers puts the fault at this host, its NIC or its switch port."""
    try:
        from app.services.topology import topology_manager

        chain = topology_manager.l2_parent_chain(endpoint_id)
        nodes = topology_manager._nodes
    except Exception:
        return None
    if not chain:
        return None

    def label(node_id: str) -> str:
        node = nodes.get(node_id) or {}
        return f"{node.get('label') or node_id} ({node.get('ip_address') or '?'})"

    for parent in chain:
        state = str((nodes.get(parent) or {}).get("state") or "").upper()
        if state in ("DOWN", "FAILURE_POINT"):
            return (
                (nodes.get(parent) or {}).get("ip_address"),
                None,
                f"Layer 2 attachment through {label(parent)}, found by LLDP/CDP, which is down too: "
                f"the failure is most likely at or above {label(parent)}.",
            )
    first = chain[0]
    return (
        None,
        (nodes.get(first) or {}).get("ip_address"),
        f"Layer 2 attachment through {label(first)}, found by LLDP/CDP, which still answers: "
        "the failure is at this host, its NIC or its switch port.",
    )


def _confirm_later(endpoint_id: UUID, target_ip: str, delay: Optional[float]) -> None:
    """Second trace after a recovery showed a new path, once the confirmation delay is over."""
    import asyncio

    async def _run() -> None:
        await asyncio.sleep(float(delay or 120.0) + 5.0)
        try:
            await refresh_baseline_route(endpoint_id, target_ip, trigger="confirm")
        except Exception as exc:
            logger.warning("Confirming trace for %s failed: %s", endpoint_id, exc)

    task = asyncio.create_task(_run())
    _confirm_tasks.add(task)
    task.add_done_callback(_confirm_tasks.discard)


async def handle_endpoint_recovery(
    endpoint_id: UUID,
    target_ip: str,
    db: Optional[AsyncSession] = None,
) -> None:
    """
    Recovery Hook: When an endpoint transitions back from 'DOWN' to 'UP',
    automatically sets active incidents to `is_resolved = True` and refreshes baseline route.
    """

    async def _execute_recovery(session: AsyncSession):
        resolve_sql = text("""
            UPDATE endpoint_rca_incidents
            SET is_resolved = TRUE
            WHERE endpoint_id = CAST(:endpoint_id AS uuid)
              AND is_resolved = FALSE
        """)
        res = await session.execute(resolve_sql, {"endpoint_id": str(endpoint_id)})
        resolved_count = res.rowcount
        logger.info(
            "Endpoint %s recovered to UP. Resolved %d active RCA incidents.",
            endpoint_id,
            resolved_count,
        )
        # Trace the route it came back on. A path different from before is only pending until a
        # second trace confirms it (route_paths); that trace runs here a little later, since the
        # engine has no discovery queue of its own.
        traced = await refresh_baseline_route(endpoint_id, target_ip, db=session, trigger="recovery")
        if traced.get("path_outcome") == "pending":
            _confirm_later(endpoint_id, target_ip, traced.get("confirm_after"))

        # Publish resolution RCA_INCIDENT event
        try:
            broker = driver_manager.get_event_broker()
            if broker:
                rca_payload = {
                    "event_type": "RCA_INCIDENT",
                    "root_cause_endpoint_id": str(endpoint_id),
                    "is_resolved": True,
                }
                await broker.publish("RCA_INCIDENT", rca_payload)
        except Exception as exc:
            logger.warning("Failed to publish recovery RCA_INCIDENT event: %s", exc)

    if db is not None:
        await _execute_recovery(db)
    else:
        async with AsyncSessionLocal() as session:
            await _execute_recovery(session)
            await session.commit()
