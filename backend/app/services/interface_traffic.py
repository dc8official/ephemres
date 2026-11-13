"""Per-interface traffic from SNMP counters where a device has them, from flows otherwise.

v3.3.6 build plan 8.3, decided with Kenneth: where a device answers SNMP, its interface figures
(Traffic page charts and table, WAN totals, traffic alerts, PDF interface figures) use its
counters: they are exact, where flow figures are sampled and miss whatever the router does not
export. Flows stay the source for devices without SNMP and for everything about conversations.

The choice is made per interface and per minute (or hour): a minute that has an SNMP row for
an interface uses it; one that has none (no SNMP, the device not answering, or a minute before
SNMP was switched on) uses the flow row. So switching SNMP on hands over cleanly with no gap
and no double count. flow_count always comes from flows (SNMP knows nothing of flows).

The functions return SQL to put after FROM; the result has the flow tables' column names
(bucket, exporter_id, interface_idx, in_bytes, out_bytes, in_packets, out_packets, flow_count,
and for the hourly tier peak_in_minute_bytes, peak_out_minute_bytes) plus `source` ('snmp' or
'flow'). `time_clause` is a condition on a table alias written as {t}, e.g.
"{t}.bucket >= :start", applied inside both halves so each can use its own index.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple
from uuid import UUID

from app.core.orm import AsyncSession, text


# Both sources are written as one GROUP BY over the two tables, not as "flow rows with NOT EXISTS
# an SNMP row": PostgreSQL planned that anti-join as a merge on (exporter, interface) with the
# minute as a join filter, comparing every flow minute with every SNMP minute of the interface.
# Which plan it picked depended on the router list (Traffic page Interfaces table, build plan 7.5
# step 6): one router over 7 days took 17-24 s on the scratch week, three took 60 ms. The grouped
# form is a hash aggregate over the rows in the window, 65-130 ms in every case tried. Each table
# has one row per (bucket, exporter, interface), so MAX picks that row's value.

def _pick(column: str) -> str:
    """SNMP's value for a bucket that has an SNMP row, else the flow row's."""
    return (f"CASE WHEN bool_or(src = 'snmp') THEN MAX({column}) FILTER (WHERE src = 'snmp') "
            f"ELSE MAX({column}) END AS {column}")


def _grouped(snmp_table: str, flow_table: str, time_clause: str, extra: tuple = ()) -> str:
    s, f = (time_clause.format(t=a) for a in ("s", "f"))
    cols = ("in_bytes", "out_bytes", "in_packets", "out_packets") + extra
    s_cols = ", ".join(f"s.{c}" for c in cols)
    f_cols = ", ".join(f"f.{c}" for c in cols)
    picks = ",\n               ".join(_pick(c) for c in cols)
    return f"""(
        SELECT bucket, exporter_id, interface_idx,
               {picks},
               COALESCE(MAX(flow_count) FILTER (WHERE src = 'flow'), 0) AS flow_count,
               CASE WHEN bool_or(src = 'snmp') THEN 'snmp' ELSE 'flow' END AS source
        FROM (
            SELECT s.bucket, s.exporter_id, s.interface_idx, {s_cols}, 0::bigint AS flow_count, 'snmp'::text AS src
            FROM {snmp_table} s
            WHERE {s}
            UNION ALL
            SELECT f.bucket, f.exporter_id, f.interface_idx, {f_cols}, f.flow_count, 'flow'::text AS src
            FROM {flow_table} f
            WHERE {f}
        ) u
        GROUP BY bucket, exporter_id, interface_idx
    )"""


def minute_source(time_clause: str) -> str:
    return _grouped("snmp_interface_minute_rollups", "flow_interface_minute_rollups", time_clause)


def hourly_source(time_clause: str) -> str:
    """The hourly tier. An hour with any SNMP minute for an interface counts as SNMP's: the hour
    SNMP was switched on shows only its SNMP minutes (at most one hour, once)."""
    return _grouped(
        "snmp_interface_hourly_rollups", "flow_interface_hourly_rollups", time_clause,
        extra=("peak_in_minute_bytes", "peak_out_minute_bytes"),
    )


def source_for(hourly: bool, time_clause: str) -> str:
    return hourly_source(time_clause) if hourly else minute_source(time_clause)


# --- Per-link hours (v3.3.6 build plan 7.5 step 3) ---------------------------------------------
# The usual band (services/traffic_usual) and, from step 4, today's peak and the per-link charts
# read hours of WAN links through here, so they all apply the SNMP-else-flow rule above. Each hour
# carries its busiest minute (the hourly aggregates keep it, migrations 0016 and 0027): an hour's
# average hides a ten-minute burst, its peak minute does not.

LinkKey = Tuple[UUID, int]


@dataclass(frozen=True)
class LinkHour:
    bucket: datetime.datetime
    in_bytes: int
    out_bytes: int
    # The hour's busiest minute, in bytes (that minute's rate is bytes * 8 / 60).
    peak_in_minute_bytes: int
    peak_out_minute_bytes: int
    source: str  # 'snmp' or 'flow'


def _links_predicate(links: Sequence[LinkKey]) -> Tuple[str, Dict[str, Any]]:
    """Rows of `links`, one parameter per value (list parameters don't reach text() as arrays)."""
    if not links:
        return "FALSE", {}
    params: Dict[str, Any] = {}
    clauses = []
    for i, (exporter_id, idx) in enumerate(links):
        clauses.append(f"(exporter_id = CAST(:lk_e{i} AS uuid) AND interface_idx = :lk_i{i})")
        params[f"lk_e{i}"], params[f"lk_i{i}"] = str(exporter_id), int(idx)
    return "(" + " OR ".join(clauses) + ")", params


async def link_hours(
    db: AsyncSession,
    links: Sequence[LinkKey],
    start: datetime.datetime,
    end: datetime.datetime,
) -> Dict[LinkKey, List[LinkHour]]:
    """Hours [start, end) of each link, oldest first; hours with no row are left out (no
    traffic, or nothing reported). Hours still filling come from the aggregates' real-time part."""
    out: Dict[LinkKey, List[LinkHour]] = {(UUID(str(e)), int(i)): [] for e, i in links}
    if not links:
        return out
    predicate, params = _links_predicate(links)
    source = hourly_source("{t}.bucket >= :start AND {t}.bucket < :end")
    res = await db.execute(
        text(f"""
            SELECT exporter_id, interface_idx, bucket, in_bytes, out_bytes,
                   peak_in_minute_bytes, peak_out_minute_bytes, source
            FROM {source} t
            WHERE {predicate}
            ORDER BY bucket
        """),
        {**params, "start": start, "end": end},
    )
    for row in res.all():
        key = (UUID(str(row[0])), int(row[1]))
        out.setdefault(key, []).append(LinkHour(
            bucket=row[2],
            in_bytes=int(row[3] or 0),
            out_bytes=int(row[4] or 0),
            peak_in_minute_bytes=int(row[5] or 0),
            peak_out_minute_bytes=int(row[6] or 0),
            source=str(row[7]),
        ))
    return out


async def link_minutes(
    db: AsyncSession,
    links: Sequence[LinkKey],
    start: datetime.datetime,
    end: datetime.datetime,
) -> Dict[LinkKey, Dict[datetime.datetime, Tuple[int, int]]]:
    """Minutes [start, end) of each link as {bucket: (in bytes, out bytes)}, SNMP else flows: the
    per-link charts at 1h and 6h (build plan 7.5 step 5). Minutes with no row are left out."""
    out: Dict[LinkKey, Dict[datetime.datetime, Tuple[int, int]]] = {(UUID(str(e)), int(i)): {} for e, i in links}
    if not links:
        return out
    predicate, params = _links_predicate(links)
    source = minute_source("{t}.bucket >= :start AND {t}.bucket < :end")
    res = await db.execute(
        text(f"""
            SELECT exporter_id, interface_idx, bucket, in_bytes, out_bytes
            FROM {source} t
            WHERE {predicate}
        """),
        {**params, "start": start, "end": end},
    )
    for row in res.all():
        key = (UUID(str(row[0])), int(row[1]))
        out.setdefault(key, {})[row[2]] = (int(row[3] or 0), int(row[4] or 0))
    return out
