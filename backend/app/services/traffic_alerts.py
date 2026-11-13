"""Traffic alerts on WAN links (v3.3.6 build plan 7.4, designed with Kenneth on 2026-11-12).

Watched links: interfaces marked WAN that have a speed set ("set a speed" otherwise; LAN links
are not watched). Each link follows the default rule in Settings → Alerts, or its own
threshold and minutes, or is opted out, through its flow_interface_aliases entry ("alert").

Once a minute (the alert dispatcher's leader calls evaluate()):

- HIGH: in or out utilisation above the threshold for every one of the last N minutes →
  TRAFFIC_HIGH, once per busy spell (no reminders).
- NORMAL: while high, utilisation under (threshold - 10) for the last 5 minutes → TRAFFIC_NORMAL,
  with how long it lasted and the peak.
- SILENT: no flow data at all for the link in the last 15 minutes, though it had some in the
  last 24 hours → TRAFFIC_SILENT ("link down, or the router stopped exporting"); the first
  minute with data again → TRAFFIC_RESUMED. Only while flow collection is on.

Minutes are taken only once complete: routers export long-running flows about every 60 s, so a
minute's total settles a minute or two later (COMPLETE_AFTER). A minute without a row is a
minute without traffic (0 %). Utilisation is the minute's bits over the link speed, the same
arithmetic as the Traffic page (flow_metrics.utilization_pct). When phase 6 gives a link SNMP
counters, link_utilisation() is the place to prefer them.

State (open alert, since, peak) is in traffic_alert_state, so a new dispatcher leader neither
repeats nor loses an alert in progress.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from app.services import snmp_inventory
from app.services import interface_traffic
from app.core.orm import AsyncSession, text
from app.services.flow_metrics import ROLE_WAN, configured_speed_mbps, interface_alias, interface_role, utilization_pct

logger = logging.getLogger(__name__)

COMPLETE_AFTER = timedelta(minutes=2)
CLEAR_MARGIN_PCT = 10
CLEAR_MINUTES = 5
SILENT_MINUTES = 15
SILENT_ARMED_WITHIN = timedelta(hours=24)

HIGH, NORMAL, SILENT, RESUMED = "TRAFFIC_HIGH", "TRAFFIC_NORMAL", "TRAFFIC_SILENT", "TRAFFIC_RESUMED"
SEVERITIES = (HIGH, NORMAL, SILENT, RESUMED)


@dataclass(frozen=True)
class WatchedLink:
    exporter_id: UUID
    exporter_name: str
    exporter_ip: str
    interface_idx: int
    name: str
    speed_mbps: int
    threshold_pct: int
    minutes: int


@dataclass
class TrafficEvent:
    severity: str
    link: WatchedLink
    summary: str
    facts: Dict[str, str] = field(default_factory=dict)
    direction: Optional[str] = None

    @property
    def rate_key(self) -> str:
        """Rate-limit key: one link and direction, not the whole exporter."""
        return f"traffic:{self.link.exporter_id}:{self.link.interface_idx}:{self.direction or 'link'}"


def link_rule(alias_data: Any, default_pct: int, default_minutes: int) -> Optional[Tuple[int, int]]:
    """(threshold %, minutes) for a link, or None when it opted out."""
    alert = alias_data.get("alert") if isinstance(alias_data, dict) else None
    if not isinstance(alert, dict) or alert.get("mode") in (None, "default"):
        return default_pct, default_minutes
    if alert.get("mode") == "off":
        return None
    pct = alert.get("threshold_pct") or default_pct
    mins = alert.get("minutes") or default_minutes
    return int(pct), int(mins)


async def watched_links(db: AsyncSession, default_pct: int, default_minutes: int) -> List[WatchedLink]:
    """WAN links with a speed on live endpoints, with the rule each one follows."""
    res = await db.execute(
        text("""
            SELECT id, hostname, host(ip_address), flow_interface_aliases
            FROM endpoints
            WHERE deleted_at IS NULL AND endpoint_status != 'DELETED'
              AND jsonb_path_exists(flow_interface_aliases, '$.* ? (@.role == "wan")')
        """)
    )
    links: List[WatchedLink] = []
    found = res.fetchall()
    # A link's speed may come from SNMP when none was typed (v3.3.6 SNMP poller); the WAN role
    # and the alert rule are manual only, so the merge cannot add or remove a watched link.
    merged = await snmp_inventory.merged_for(db, {ep_id: raw for ep_id, _, _, raw in found})
    for ep_id, hostname, ip, raw in found:
        aliases = merged.get(str(ep_id), {})
        for idx_str, alias_data in (aliases or {}).items():
            if interface_role(alias_data) != ROLE_WAN:
                continue
            speed = configured_speed_mbps(alias_data)
            rule = link_rule(alias_data, default_pct, default_minutes)
            if not speed or rule is None:
                continue
            try:
                idx = int(idx_str)
            except ValueError:
                continue
            name, _ = interface_alias(alias_data, f"Interface {idx}")
            links.append(WatchedLink(UUID(str(ep_id)), str(hostname), str(ip), idx, name, speed, rule[0], rule[1]))
    return links


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def link_utilisation(
    rows: Dict[datetime, Tuple[int, int]], minutes: List[datetime], speed_mbps: int
) -> Tuple[List[float], List[float]]:
    """Per minute (in the order of `minutes`), in and out utilisation in %; no row is 0 %."""
    ins, outs = [], []
    for m in minutes:
        in_b, out_b = rows.get(m, (0, 0))
        ins.append(utilization_pct(in_b * 8 / 60.0, speed_mbps))
        outs.append(utilization_pct(out_b * 8 / 60.0, speed_mbps))
    return ins, outs


def _duration(td: timedelta) -> str:
    mins = max(1, int(round(td.total_seconds() / 60)))
    h, m = divmod(mins, 60)
    return f"{h} h {m} min" if h and m else (f"{h} h" if h else f"{m} min")


async def _load_state(db: AsyncSession, links: List[WatchedLink]) -> Dict[Tuple[str, int, str], Dict[str, Any]]:
    if not links:
        return {}
    res = await db.execute(
        text("""
            SELECT exporter_id, interface_idx, kind, active, since, peak_pct
            FROM traffic_alert_state WHERE exporter_id = ANY(CAST(:ids AS uuid[]))
        """),
        {"ids": tuple({str(l.exporter_id) for l in links})},
    )
    return {
        (str(r[0]), int(r[1]), str(r[2])): {"active": bool(r[3]), "since": r[4], "peak": r[5]}
        for r in res.fetchall()
    }


async def _save_state(db: AsyncSession, link: WatchedLink, kind: str, active: bool,
                      since: Optional[datetime], peak: Optional[float], now: datetime) -> None:
    await db.execute(
        text("""
            INSERT INTO traffic_alert_state (exporter_id, interface_idx, kind, active, since, peak_pct, updated_at)
            VALUES (CAST(:eid AS uuid), :idx, :kind, :active, CAST(:since AS timestamptz),
                    CAST(:peak AS real), CAST(:now AS timestamptz))
            ON CONFLICT (exporter_id, interface_idx, kind) DO UPDATE SET
                active = EXCLUDED.active, since = EXCLUDED.since,
                peak_pct = EXCLUDED.peak_pct, updated_at = EXCLUDED.updated_at
        """),
        {"eid": str(link.exporter_id), "idx": link.interface_idx, "kind": kind, "active": active,
         "since": since, "peak": peak, "now": now},
    )


async def evaluate(
    db: AsyncSession,
    *,
    default_pct: int,
    default_minutes: int,
    flow_collection_on: bool,
    now: Optional[datetime] = None,
) -> List[TrafficEvent]:
    """Checks every watched link once and returns the alerts to send. The caller commits."""
    now = now or datetime.now(timezone.utc)
    links = await watched_links(db, default_pct, default_minutes)
    if not links:
        return []

    # Complete minutes only: [end - lookback, end).
    end = _minute(now - COMPLETE_AFTER)
    lookback = max([l.minutes for l in links] + [CLEAR_MINUTES, SILENT_MINUTES])
    start = end - timedelta(minutes=lookback)

    res = await db.execute(
        text("""
            -- Named: the ORM keys row values by column name, and two bare SUM()s would collide.
            -- SNMP counters where the link has them, flows otherwise (v3.3.6). "Silent" below stays
            -- on flow rows: it means the router stopped exporting flows.
            SELECT exporter_id, interface_idx, bucket, SUM(in_bytes) AS in_b, SUM(out_bytes) AS out_b
            FROM """ + interface_traffic.minute_source(
                "{t}.exporter_id = ANY(CAST(:ids AS uuid[])) AND {t}.bucket >= CAST(:start AS timestamptz)"
                " AND {t}.bucket < CAST(:end AS timestamptz)") + """ t
            GROUP BY exporter_id, interface_idx, bucket
        """),
        {"ids": tuple({str(l.exporter_id) for l in links}), "start": start, "end": end},
    )
    rows: Dict[Tuple[str, int], Dict[datetime, Tuple[int, int]]] = {}
    for eid, idx, bucket, in_b, out_b in res.fetchall():
        rows.setdefault((str(eid), int(idx)), {})[_minute(bucket)] = (int(in_b or 0), int(out_b or 0))

    # Links with any data in the last 24 h: only those can "go silent".
    armed: set = set()
    if flow_collection_on:
        res = await db.execute(
            text("""
                SELECT DISTINCT exporter_id, interface_idx FROM flow_interface_minute_rollups
                WHERE exporter_id = ANY(CAST(:ids AS uuid[]))
                  AND bucket >= CAST(:since AS timestamptz) AND bucket < CAST(:end AS timestamptz)
            """),
            {"ids": tuple({str(l.exporter_id) for l in links}), "since": end - SILENT_ARMED_WITHIN, "end": end},
        )
        armed = {(str(r[0]), int(r[1])) for r in res.fetchall()}

    state = await _load_state(db, links)
    events: List[TrafficEvent] = []
    for link in links:
        key = (str(link.exporter_id), link.interface_idx)
        link_rows = rows.get(key, {})
        last_n = [end - timedelta(minutes=i) for i in range(link.minutes, 0, -1)]
        last_clear = [end - timedelta(minutes=i) for i in range(CLEAR_MINUTES, 0, -1)]
        last_silent = [end - timedelta(minutes=i) for i in range(SILENT_MINUTES, 0, -1)]
        label = f"{link.exporter_name} {link.name}"

        for direction, pos in (("in", 0), ("out", 1)):
            kind = f"high_{direction}"
            st = state.get((key[0], key[1], kind), {"active": False, "since": None, "peak": None})
            series_n = link_utilisation(link_rows, last_n, link.speed_mbps)[pos]
            series_c = link_utilisation(link_rows, last_clear, link.speed_mbps)[pos]
            word = "inbound" if direction == "in" else "outbound"

            if not st["active"]:
                if series_n and all(u > link.threshold_pct for u in series_n):
                    peak = max(series_n)
                    since = last_n[0]
                    await _save_state(db, link, kind, True, since, peak, now)
                    events.append(TrafficEvent(
                        HIGH, link,
                        f"{label}: {word} traffic above {link.threshold_pct}% for {link.minutes} min",
                        {"Link": f"{link.name} ({link.speed_mbps} Mbps)", "Direction": word,
                         "Average": f"{sum(series_n) / len(series_n):.0f}%", "Peak": f"{peak:.0f}%",
                         "Threshold": f"{link.threshold_pct}% for {link.minutes} min"},
                        direction,
                    ))
            else:
                peak = max([st["peak"] or 0.0] + series_c)
                if all(u < link.threshold_pct - CLEAR_MARGIN_PCT for u in series_c):
                    since = st["since"] or last_clear[0]
                    lasted = _duration(last_clear[0] - since)
                    await _save_state(db, link, kind, False, None, None, now)
                    events.append(TrafficEvent(
                        NORMAL, link,
                        f"{label}: {word} traffic back to normal (busy {lasted}, peak {peak:.0f}%)",
                        {"Link": f"{link.name} ({link.speed_mbps} Mbps)", "Direction": word,
                         "Busy for": lasted, "Peak": f"{peak:.0f}%"},
                        direction,
                    ))
                elif peak != (st["peak"] or 0.0):
                    await _save_state(db, link, kind, True, st["since"], peak, now)

        st = state.get((key[0], key[1], "silent"), {"active": False, "since": None, "peak": None})
        has_recent = any(m in link_rows for m in last_silent)
        if not st["active"]:
            if flow_collection_on and key in armed and not has_recent:
                await _save_state(db, link, "silent", True, last_silent[0], None, now)
                events.append(TrafficEvent(
                    SILENT, link,
                    f"{label}: no flow data for {SILENT_MINUTES} min (link down, or the router stopped exporting)",
                    {"Link": f"{link.name} ({link.speed_mbps} Mbps)", "Last seen": "over 15 min ago"},
                ))
        elif has_recent and (end - timedelta(minutes=1)) in link_rows:
            since = st["since"] or last_silent[0]
            await _save_state(db, link, "silent", False, None, None, now)
            events.append(TrafficEvent(
                RESUMED, link,
                f"{label}: traffic seen again (silent for {_duration(end - since)})",
                {"Link": f"{link.name} ({link.speed_mbps} Mbps)", "Silent for": _duration(end - since)},
            ))
    return events
