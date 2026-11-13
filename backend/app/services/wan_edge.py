"""WAN (network edge) interfaces and the traffic crossing them.

An interface is WAN when an admin sets `role: "wan"` on its `flow_interface_aliases` entry.
Totals summed over every exporter count traffic once for each router it crosses, so a branch to
HQ transfer seen by both routers counts twice. A packet crosses the edge once, so the sum over
WAN interfaces is the network's real in/out total. On a WAN interface, "in" is traffic arriving
from outside and "out" is traffic leaving.
"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import UUID

from app.services import snmp_inventory
from app.services import interface_traffic
from app.core.orm import AsyncSession, text
from app.services.flow_metrics import ROLE_WAN, configured_speed_mbps, interface_alias, interface_role


@dataclass(frozen=True)
class WanInterface:
    exporter_id: UUID
    exporter_hostname: str
    interface_idx: int
    name: str
    # None when no link speed is configured.
    speed_mbps: Optional[int]
    # For the Traffic page's WAN links (build plan 7.5 step 4): the router's site, where the
    # speed came from ("manual", "snmp" or "default", as the interface editors say) and what SNMP
    # itself reports, so a typed 20 Mb/s on a 1 Gb/s port can say so. SNMP's view of the port is
    # None for a router that isn't polled.
    site: Optional[str] = None
    speed_source: str = "default"
    snmp_speed_mbps: Optional[int] = None
    oper_status: Optional[str] = None
    admin_status: Optional[str] = None


def _decode_aliases(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def list_wan_interfaces(
    db: AsyncSession,
    exporter_id: Optional[UUID] = None,
    exporter_ids: Optional[Sequence[Any]] = None,
) -> List[WanInterface]:
    """WAN-role interfaces of live endpoints, ordered by host and index: optionally of one
    exporter, or of the routers in a Traffic page scope (`exporter_ids`, None for all of them;
    an SNMP-only router is included like any other, its traffic coming from its counters)."""
    exporter_filter = ""
    params: Dict[str, Any] = {}
    if exporter_id is not None:
        exporter_filter = "AND id = CAST(:exporter_id AS uuid)"
        params["exporter_id"] = str(exporter_id)
    if exporter_ids is not None:
        exporter_filter += " AND id = ANY(CAST(:exporter_ids AS uuid[]))"
        params["exporter_ids"] = tuple(str(e) for e in exporter_ids)
    res = await db.execute(
        text(f"""
            SELECT id, hostname, flow_interface_aliases, site
            FROM endpoints
            WHERE deleted_at IS NULL
              AND endpoint_status != 'DELETED'
              AND jsonb_path_exists(flow_interface_aliases, '$.* ? (@.role == "wan")')
              {exporter_filter}
            ORDER BY hostname
        """),
        params,
    )
    interfaces: List[WanInterface] = []
    found = res.all()
    # SNMP fills names and speeds that were not typed (v3.3.6); the WAN role stays manual. Loaded
    # here rather than through merged_for so each link can also say where its speed came from.
    snmp = await snmp_inventory.load(db, [row[0] for row in found])
    for row in found:
        manual = _decode_aliases(row[2])
        snmp_map = snmp.get(str(row[0]), {})
        aliases = snmp_inventory.merge(manual, snmp_map)
        site = str(row[3]).strip() if row[3] and str(row[3]).strip() else None
        for idx_str, alias_data in aliases.items():
            if interface_role(alias_data) != ROLE_WAN:
                continue
            try:
                if_idx = int(idx_str)
            except ValueError:
                continue
            name, _ = interface_alias(alias_data, f"Interface {if_idx}")
            snmp_row = snmp_map.get(if_idx)
            source = snmp_inventory.sources(manual, snmp_row, if_idx)
            interfaces.append(
                WanInterface(
                    exporter_id=UUID(str(row[0])),
                    exporter_hostname=str(row[1]),
                    interface_idx=if_idx,
                    name=name,
                    speed_mbps=configured_speed_mbps(alias_data),
                    site=site,
                    speed_source=source["speed_source"],
                    snmp_speed_mbps=source["snmp_speed_mbps"],
                    oper_status=(snmp_row or {}).get("oper_status"),
                    admin_status=(snmp_row or {}).get("admin_status"),
                )
            )
    interfaces.sort(key=lambda w: (w.exporter_hostname, w.interface_idx))
    return interfaces


def interface_filter_sql(interfaces: List[WanInterface]) -> Tuple[str, Dict[str, Any]]:
    """SQL predicate matching rows of the interface rollup tables for `interfaces`.

    Pairs are expanded into individual parameters: the ORM serialises list parameters as JSON.
    An empty list matches nothing.
    """
    if not interfaces:
        return "FALSE", {}
    clauses: List[str] = []
    params: Dict[str, Any] = {}
    for i, wan in enumerate(interfaces):
        clauses.append(f"(exporter_id = CAST(:wan_e{i} AS uuid) AND interface_idx = :wan_i{i})")
        params[f"wan_e{i}"] = str(wan.exporter_id)
        params[f"wan_i{i}"] = wan.interface_idx
    return "(" + " OR ".join(clauses) + ")", params


async def wan_link_totals(
    db: AsyncSession,
    interfaces: List[WanInterface],
    start: datetime.datetime,
    end: datetime.datetime,
) -> Dict[Tuple[UUID, int], Dict[str, int]]:
    """Bytes and packets per WAN interface in the minute buckets [start, end), and whether any of
    those minutes came from SNMP counters (`from_snmp`)."""
    if not interfaces:
        return {}
    predicate, params = interface_filter_sql(interfaces)
    source = interface_traffic.minute_source("{t}.bucket >= :start AND {t}.bucket < :end")
    res = await db.execute(
        text(f"""
            SELECT exporter_id, interface_idx,
                   COALESCE(SUM(in_bytes), 0) AS in_bytes, COALESCE(SUM(out_bytes), 0) AS out_bytes,
                   COALESCE(SUM(in_packets), 0) AS in_packets, COALESCE(SUM(out_packets), 0) AS out_packets,
                   bool_or(source = 'snmp') AS from_snmp
            -- SNMP counters where the link has them, flows otherwise (v3.3.6).
            FROM {source} t
            WHERE {predicate}
            GROUP BY exporter_id, interface_idx
        """),
        {**params, "start": start, "end": end},
    )
    totals: Dict[Tuple[UUID, int], Dict[str, int]] = {}
    for row in res.all():
        totals[(UUID(str(row[0])), int(row[1]))] = {
            "in_bytes": int(row[2] or 0),
            "out_bytes": int(row[3] or 0),
            "in_packets": int(row[4] or 0),
            "out_packets": int(row[5] or 0),
            "from_snmp": bool(row[6]),
        }
    return totals
