"""Forwarding paths seen in flows, read back for RCA and the topology (v3.4 Phase 4).

ephemres-flowd writes flow_path_observations (where each exporter forwarded traffic to or from
a monitored endpoint) and flow_route_changes. Traceroute stays the route of record; this module
only adds evidence next to it:

- flow_evidence(): what flows showed when an RCA runs (stored with the incident).
- classify_route_edges(): whether each traceroute hop-to-hop link is confirmed by traffic.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import UUID

from app.core.orm import AsyncSession, text
from app.services.flow_metrics import interface_alias

# Observations newer than this count as the path traffic takes now.
ACTIVE_PATH_MINUTES = 15
# Route changes this long before an RCA are reported with it.
RCA_ROUTE_CHANGE_MINUTES = 15

EDGE_CONFIRMED = "confirmed"
EDGE_DIFFERS = "differs"
EDGE_INFERRED = "inferred"


def _aliases(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _if_name(aliases: Dict[str, Any], if_idx: Optional[int]) -> Optional[str]:
    if not if_idx:
        return None
    name, _ = interface_alias(aliases.get(str(if_idx)), f"if{if_idx}")
    return name


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime.datetime) else None


async def flow_evidence(
    db: AsyncSession,
    endpoint_id: UUID,
    failure_started: datetime.datetime,
    now: Optional[datetime.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Flow paths and route changes for `endpoint_id`, or None when no exporter sees its traffic.

    `failure_started` is when probes began failing; traffic seen after it means the endpoint
    still talks to users while probes fail. Records can reach the collector up to the
    exporter's active timeout late, so no traffic is weaker evidence than traffic.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    res = await db.execute(
        text("""
            SELECT DISTINCT ON (o.exporter_id, o.direction)
                   o.exporter_id, e.hostname AS exporter_hostname, e.flow_interface_aliases AS aliases,
                   o.direction, o.in_if, o.out_if, host(o.next_hop) AS next_hop, o.last_seen
            FROM flow_path_observations o
            JOIN endpoints e ON e.id = o.exporter_id AND e.deleted_at IS NULL AND e.endpoint_status != 'DELETED'
            WHERE o.endpoint_id = CAST(:endpoint_id AS uuid)
              AND o.last_seen >= :since
            ORDER BY o.exporter_id, o.direction, o.last_seen DESC, o.bytes DESC
        """),
        {"endpoint_id": str(endpoint_id), "since": now - datetime.timedelta(days=1)},
    )
    rows = res.mappings().all()
    if not rows:
        return None

    paths: List[Dict[str, Any]] = []
    last_seen_any: Optional[datetime.datetime] = None
    for r in rows:
        aliases = _aliases(r["aliases"])
        last_seen = r["last_seen"]
        if last_seen_any is None or last_seen > last_seen_any:
            last_seen_any = last_seen
        paths.append({
            "exporter_id": str(r["exporter_id"]),
            "exporter_hostname": r["exporter_hostname"],
            "direction": r["direction"],
            "in_if": r["in_if"],
            "in_if_name": _if_name(aliases, r["in_if"]),
            "out_if": r["out_if"],
            "out_if_name": _if_name(aliases, r["out_if"]),
            "next_hop": r["next_hop"],
            "last_seen": _iso(last_seen),
            "after_failure": last_seen >= failure_started,
        })
    paths.sort(key=lambda p: (p["exporter_hostname"] or "", p["direction"]))

    ch_res = await db.execute(
        text("""
            SELECT c.detected_at, e.hostname AS exporter_hostname, c.direction,
                   c.old_out_if, host(c.old_next_hop) AS old_next_hop,
                   c.new_out_if, host(c.new_next_hop) AS new_next_hop
            FROM flow_route_changes c
            JOIN endpoints e ON e.id = c.exporter_id AND e.deleted_at IS NULL AND e.endpoint_status != 'DELETED'
            WHERE c.endpoint_id = CAST(:endpoint_id AS uuid)
              AND c.detected_at >= :since
            ORDER BY c.detected_at DESC
        """),
        {
            "endpoint_id": str(endpoint_id),
            "since": failure_started - datetime.timedelta(minutes=RCA_ROUTE_CHANGE_MINUTES),
        },
    )
    route_changes = [
        {
            "detected_at": _iso(c["detected_at"]),
            "exporter_hostname": c["exporter_hostname"],
            "direction": c["direction"],
            "old_out_if": c["old_out_if"],
            "old_next_hop": c["old_next_hop"],
            "new_out_if": c["new_out_if"],
            "new_next_hop": c["new_next_hop"],
        }
        for c in ch_res.mappings().all()
    ]

    traffic_after_failure = any(p["after_failure"] for p in paths)
    if traffic_after_failure:
        summary = (
            "Flows still show traffic to or from this endpoint after probes started failing: "
            "the fault is likely on the probe path, or ICMP is filtered."
        )
    else:
        summary = "No flow traffic seen since probes started failing."
    if route_changes:
        summary += f" {len(route_changes)} route change(s) seen in flows around the failure."

    return {
        "generated_at": now.isoformat(),
        "failure_started": failure_started.isoformat(),
        "last_traffic_seen": _iso(last_seen_any),
        "traffic_after_failure": traffic_after_failure,
        "paths": paths,
        "route_changes": route_changes,
        "summary": summary,
    }


async def exporter_ip_map(db: AsyncSession) -> Dict[str, str]:
    """Every address that identifies an exporter (its IP and flow_exporter_ips) -> exporter id."""
    res = await db.execute(
        text("""
            SELECT DISTINCT o.exporter_id, host(e.ip_address) AS ip, e.flow_exporter_ips
            FROM (SELECT DISTINCT exporter_id FROM flow_path_observations) o
            JOIN endpoints e ON e.id = o.exporter_id
            WHERE e.deleted_at IS NULL AND e.endpoint_status != 'DELETED'
        """)
    )
    mapping: Dict[str, str] = {}
    for row in res.all():
        exporter_id = str(row[0])
        if row[1]:
            mapping[str(row[1])] = exporter_id
        for extra in row[2] or []:
            mapping[str(extra).split("/")[0]] = exporter_id
    return mapping


async def active_next_hops(
    db: AsyncSession, now: Optional[datetime.datetime] = None
) -> Dict[Tuple[str, str], List[str]]:
    """(exporter_id, endpoint_id) -> next hops that carried traffic towards the endpoint recently,
    busiest first. Observations without a next hop are left out."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    res = await db.execute(
        text("""
            SELECT exporter_id, endpoint_id, host(next_hop) AS next_hop, SUM(bytes) AS bytes
            FROM flow_path_observations
            WHERE direction = 'to' AND next_hop IS NOT NULL AND last_seen >= :since
            GROUP BY exporter_id, endpoint_id, next_hop
            ORDER BY exporter_id, endpoint_id, SUM(bytes) DESC
        """),
        {"since": now - datetime.timedelta(minutes=ACTIVE_PATH_MINUTES)},
    )
    hops: Dict[Tuple[str, str], List[str]] = {}
    for row in res.all():
        hops.setdefault((str(row[0]), str(row[1])), []).append(str(row[2]))
    return hops


# Precedence when several routes share one link: traffic confirming it wins.
_EDGE_RANK = {EDGE_CONFIRMED: 3, EDGE_INFERRED: 2, EDGE_DIFFERS: 1}


def classify_route_edges(
    routes: Iterable[Tuple[str, List[Tuple[str, Optional[str]]]]],
    exporter_by_ip: Dict[str, str],
    next_hops: Dict[Tuple[str, str], List[str]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Flow status of traceroute links.

    `routes` holds, per endpoint, its path as (node_id, hop_ip) pairs from the first hop to the
    endpoint itself (hop_ip None for a `* * *` hop). The link from hop k to hop k+1 is checked
    against the flows of the exporter whose address hop k shows:

    - confirmed: the exporter's recent next hop for this endpoint is hop k+1's address;
    - inferred: hop k+1 did not answer traceroute and the exporter names its next hop;
    - differs: the exporter sends this endpoint's traffic to a different next hop.

    Links with no matching exporter or no recent next hop are left out (unknown). A traceroute
    shows a router's ingress address, so hop k only matches if that address is the exporter's
    IP or one of its flow_exporter_ips.
    """
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for endpoint_id, path in routes:
        for (node_id, hop_ip), (next_node_id, next_ip) in zip(path, path[1:]):
            if not hop_ip:
                continue
            exporter_id = exporter_by_ip.get(hop_ip)
            if exporter_id is None:
                continue
            seen = next_hops.get((exporter_id, endpoint_id))
            if not seen:
                continue
            if next_ip is None:
                status, detail = EDGE_INFERRED, f"Flows from {hop_ip} name next hop {seen[0]}"
            elif next_ip in seen:
                status, detail = EDGE_CONFIRMED, f"Traffic from {hop_ip} goes to {next_ip}"
            else:
                status, detail = EDGE_DIFFERS, f"Traceroute shows {next_ip}; traffic from {hop_ip} goes to {seen[0]}"
            key = (node_id, next_node_id)
            current = result.get(key)
            if current is None or _EDGE_RANK[status] > _EDGE_RANK[current["flow_status"]]:
                entry: Dict[str, Any] = {"flow_status": status, "flow_detail": detail}
                if status == EDGE_INFERRED:
                    entry["inferred_ip"] = seen[0]
                result[key] = entry
    return result


async def endpoint_flow_paths(
    db: AsyncSession, endpoint_id: UUID, now: Optional[datetime.datetime] = None
) -> Dict[str, Any]:
    """Paths exporters used for `endpoint_id` in the last 24 h and route changes in the last 7 days."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    res = await db.execute(
        text("""
            SELECT o.exporter_id, e.hostname AS exporter_hostname, e.flow_interface_aliases AS aliases,
                   o.direction, o.in_if, o.out_if, host(o.next_hop) AS next_hop,
                   o.first_seen, o.last_seen, o.bytes, o.flow_count
            FROM flow_path_observations o
            JOIN endpoints e ON e.id = o.exporter_id AND e.deleted_at IS NULL AND e.endpoint_status != 'DELETED'
            WHERE o.endpoint_id = CAST(:endpoint_id AS uuid) AND o.last_seen >= :since
            ORDER BY o.last_seen DESC, o.bytes DESC
            LIMIT 100
        """),
        {"endpoint_id": str(endpoint_id), "since": now - datetime.timedelta(days=1)},
    )
    active_since = now - datetime.timedelta(minutes=ACTIVE_PATH_MINUTES)
    paths = []
    for r in res.mappings().all():
        aliases = _aliases(r["aliases"])
        paths.append({
            "exporter_id": str(r["exporter_id"]),
            "exporter_hostname": r["exporter_hostname"],
            "direction": r["direction"],
            "in_if": r["in_if"],
            "in_if_name": _if_name(aliases, r["in_if"]),
            "out_if": r["out_if"],
            "out_if_name": _if_name(aliases, r["out_if"]),
            "next_hop": r["next_hop"],
            "first_seen": _iso(r["first_seen"]),
            "last_seen": _iso(r["last_seen"]),
            "bytes": int(r["bytes"] or 0),
            "flow_count": int(r["flow_count"] or 0),
            "active": r["last_seen"] >= active_since,
        })

    ch_res = await db.execute(
        text("""
            SELECT c.detected_at, c.exporter_id, e.hostname AS exporter_hostname, e.flow_interface_aliases AS aliases,
                   c.direction, c.old_out_if, host(c.old_next_hop) AS old_next_hop,
                   c.new_out_if, host(c.new_next_hop) AS new_next_hop
            FROM flow_route_changes c
            JOIN endpoints e ON e.id = c.exporter_id AND e.deleted_at IS NULL AND e.endpoint_status != 'DELETED'
            WHERE c.endpoint_id = CAST(:endpoint_id AS uuid) AND c.detected_at >= :since
            ORDER BY c.detected_at DESC
            LIMIT 50
        """),
        {"endpoint_id": str(endpoint_id), "since": now - datetime.timedelta(days=7)},
    )
    changes = []
    for c in ch_res.mappings().all():
        aliases = _aliases(c["aliases"])
        changes.append({
            "detected_at": _iso(c["detected_at"]),
            "exporter_id": str(c["exporter_id"]),
            "exporter_hostname": c["exporter_hostname"],
            "direction": c["direction"],
            "old_out_if": c["old_out_if"],
            "old_out_if_name": _if_name(aliases, c["old_out_if"]),
            "old_next_hop": c["old_next_hop"],
            "new_out_if": c["new_out_if"],
            "new_out_if_name": _if_name(aliases, c["new_out_if"]),
            "new_next_hop": c["new_next_hop"],
        })

    return {
        "paths": paths,
        "route_changes": changes,
        "active_minutes": ACTIVE_PATH_MINUTES,
        "next_hop_reported": any(p["next_hop"] for p in paths),
    }
