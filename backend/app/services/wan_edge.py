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
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

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


def _decode_aliases(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


async def list_wan_interfaces(db: AsyncSession, exporter_id: Optional[UUID] = None) -> List[WanInterface]:
    """WAN-role interfaces of live endpoints, optionally of one exporter, ordered by host and index."""
    exporter_filter = ""
    params: Dict[str, Any] = {}
    if exporter_id is not None:
        exporter_filter = "AND id = CAST(:exporter_id AS uuid)"
        params["exporter_id"] = str(exporter_id)
    res = await db.execute(
        text(f"""
            SELECT id, hostname, flow_interface_aliases
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
    for row in res.all():
        aliases = _decode_aliases(row[2])
        for idx_str, alias_data in aliases.items():
            if interface_role(alias_data) != ROLE_WAN:
                continue
            try:
                if_idx = int(idx_str)
            except ValueError:
                continue
            name, _ = interface_alias(alias_data, f"Interface {if_idx}")
            interfaces.append(
                WanInterface(
                    exporter_id=UUID(str(row[0])),
                    exporter_hostname=str(row[1]),
                    interface_idx=if_idx,
                    name=name,
                    speed_mbps=configured_speed_mbps(alias_data),
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
    """Bytes and packets per WAN interface in the minute buckets [start, end)."""
    if not interfaces:
        return {}
    predicate, params = interface_filter_sql(interfaces)
    res = await db.execute(
        text(f"""
            SELECT exporter_id, interface_idx,
                   COALESCE(SUM(in_bytes), 0) AS in_bytes, COALESCE(SUM(out_bytes), 0) AS out_bytes,
                   COALESCE(SUM(in_packets), 0) AS in_packets, COALESCE(SUM(out_packets), 0) AS out_packets
            FROM flow_interface_minute_rollups
            WHERE bucket >= :start AND bucket < :end AND {predicate}
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
        }
    return totals
