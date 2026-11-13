"""The Traffic page's "Now" area: each WAN link against its capacity (v3.3.6 build plan 7.5 step 4).

Review findings P1, O1 and O4, decided with Kenneth on 2026-11-13: the top of the page answers
"is any link in trouble right now" for the routers in scope, one row per WAN link, instead of
tiles that mixed five-minute figures with the picked window. For each link:

- now: in and out over the last five complete minutes (the overview's window), SNMP counters
  where the link has them, flows otherwise (wan_edge.wan_link_totals);
- the usual range for this hour of the week (services/traffic_usual) and how far above it now is;
- today's busiest minute since the server's local midnight;
- what SNMP says of the port (up, down since, shut down) and whether the router answers;
- an open traffic alert (busy since, silent since) from traffic_alert_state.

The 28 days the usual range needs and today's hours come from one read of the hourly
aggregates (interface_traffic.link_hours), so the cost does not grow with the number of links.

With a window (step 5, review O2) each link also carries its chart: in and out per bucket, each
hour's busiest minute, the usual range for each bucket's hour of the week, and the spans SNMP
saw the port down. One request for every link, not one per chart.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID

from app.core.orm import AsyncSession, text
from app.services import interface_traffic, traffic_usual, wan_edge
from app.services.flow_metrics import bits_per_second, utilization_pct
from app.services.interface_traffic import LinkHour, LinkKey
from app.services.timezone_utils import server_zone
from app.services.traffic_usual import Band

# The "now" window, the same five complete minutes as the overview.
NOW_MINUTES = 5

# SNMP device states in which its view of a port can't be trusted (snmp_device_state.status).
_SNMP_NOT_ANSWERING = ("no_reply", "auth_failed", "error")


@dataclass
class LinkAlert:
    kind: str  # "high" or "silent"
    direction: Optional[str]  # "in" / "out" for high, None for silent
    since: Optional[datetime.datetime]
    peak_pct: Optional[float]


# Per-link charts: minutes up to 6 hours (as the combined chart), hours beyond.
MINUTE_SERIES_UP_TO = datetime.timedelta(hours=6)


@dataclass
class LinkSeries:
    """One link's chart over the window, column by column (one entry per bucket). A bucket with no
    row is None: no traffic reported, which the chart draws as a gap, like the combined chart."""
    bucket_seconds: int
    times: List[datetime.datetime]
    in_bps: List[Optional[float]]
    out_bps: List[Optional[float]]
    # Each hour's busiest minute; hourly buckets only (a minute bucket is its own peak).
    peak_in_bps: List[Optional[float]]
    peak_out_bps: List[Optional[float]]
    # The usual range for each bucket's hour of the week, as worked out now.
    usual_in_low: List[Optional[float]]
    usual_in_high: List[Optional[float]]
    usual_out_low: List[Optional[float]]
    usual_out_high: List[Optional[float]]
    # (start, end) while SNMP saw the port down; end None while it still is.
    down_spans: List[tuple]
    # The busiest bucket (the busier direction; its peak minute where there is one), in % of the
    # speed when set: which charts the page opens first.
    window_peak_bps: float = 0.0
    window_peak_pct: Optional[float] = None


@dataclass
class WanLinkNow:
    link: wan_edge.WanInterface
    in_bps: float
    out_bps: float
    from_snmp: bool
    utilization_pct: Optional[float]
    usual: traffic_usual.LinkUsual
    usual_in: Optional[Band]
    usual_out: Optional[Band]
    above_usual_in_pct: Optional[float]
    above_usual_out_pct: Optional[float]
    peak_in_bps: Optional[float]
    peak_in_hour: Optional[datetime.datetime]
    peak_out_bps: Optional[float]
    peak_out_hour: Optional[datetime.datetime]
    # "up", "down", "shut_down", "not_answering", or None when the router isn't polled over SNMP
    # or SNMP doesn't know the port.
    link_state: Optional[str]
    down_since: Optional[datetime.datetime]
    snmp_last_ok_at: Optional[datetime.datetime]
    alerts: List[LinkAlert] = field(default_factory=list)
    series: Optional[LinkSeries] = None


@dataclass
class WanLinksNow:
    window_start: datetime.datetime
    window_end: datetime.datetime
    day_start: datetime.datetime
    tz_name: str
    links: List[WanLinkNow]


def _peak(hours: Sequence[LinkHour], direction: str):
    """Today's busiest minute in one direction as (bits/s, the hour it was in), from the hours'
    peak-minute columns; (None, None) when the link has no rows today."""
    best: Optional[LinkHour] = None
    for h in hours:
        value = h.peak_in_minute_bytes if direction == "in" else h.peak_out_minute_bytes
        if best is None or value > (best.peak_in_minute_bytes if direction == "in" else best.peak_out_minute_bytes):
            best = h
    if best is None:
        return None, None
    value = best.peak_in_minute_bytes if direction == "in" else best.peak_out_minute_bytes
    return bits_per_second(value, 60.0), best.bucket


async def _snmp_view(db: AsyncSession, routers: Sequence[UUID]) -> Dict[str, Any]:
    """Per router: SNMP poll status and last good answer; per link: when it last went down."""
    ids = tuple(str(r) for r in routers)
    if not ids:
        return {"devices": {}, "downs": {}}
    devices: Dict[str, Any] = {}
    downs: Dict[LinkKey, datetime.datetime] = {}
    try:
        res = await db.execute(text("""
            SELECT endpoint_id, status, last_ok_at FROM snmp_device_state
            WHERE endpoint_id = ANY(CAST(:ids AS uuid[]))
        """), {"ids": ids})
        devices = {str(r[0]): (str(r[1]), r[2]) for r in res.all()}
        # The latest up/down event per port; "down since" only counts while that is a down.
        res = await db.execute(text("""
            SELECT DISTINCT ON (endpoint_id, if_index) endpoint_id, if_index, kind, at
            FROM snmp_interface_events
            WHERE endpoint_id = ANY(CAST(:ids AS uuid[])) AND kind IN ('up', 'down') AND if_index IS NOT NULL
            ORDER BY endpoint_id, if_index, at DESC
        """), {"ids": ids})
        for ep, idx, kind, at in res.all():
            if kind == "down":
                downs[(UUID(str(ep)), int(idx))] = at
    except Exception:
        # Before migration 0026 (or a test database without it): no SNMP view.
        return {"devices": {}, "downs": {}}
    return {"devices": devices, "downs": downs}


async def _alerts(db: AsyncSession, routers: Sequence[UUID]) -> Dict[LinkKey, List[LinkAlert]]:
    """Open traffic alerts per link (services/traffic_alerts keeps them in traffic_alert_state)."""
    ids = tuple(str(r) for r in routers)
    if not ids:
        return {}
    try:
        res = await db.execute(text("""
            SELECT exporter_id, interface_idx, kind, since, peak_pct FROM traffic_alert_state
            WHERE active AND exporter_id = ANY(CAST(:ids AS uuid[]))
            ORDER BY since NULLS LAST
        """), {"ids": ids})
    except Exception:
        return {}
    out: Dict[LinkKey, List[LinkAlert]] = {}
    for ep, idx, kind, since, peak in res.all():
        kind = str(kind)
        alert = LinkAlert(
            kind="silent" if kind == "silent" else "high",
            direction=kind.split("_", 1)[1] if kind.startswith("high_") else None,
            since=since,
            peak_pct=float(peak) if peak is not None else None,
        )
        out.setdefault((UUID(str(ep)), int(idx)), []).append(alert)
    return out


def _link_state(link: wan_edge.WanInterface, device: Optional[tuple]) -> Optional[str]:
    if device and device[0] in _SNMP_NOT_ANSWERING:
        return "not_answering"
    if link.admin_status == "down":
        return "shut_down"
    if link.oper_status in ("down", "lowerLayerDown"):
        return "down"
    if link.oper_status:
        return "up"
    return None


async def down_spans(
    db: AsyncSession, routers: Sequence[UUID], start: datetime.datetime, end: datetime.datetime
) -> Dict[LinkKey, List[tuple]]:
    """Spans in [start, end) while SNMP saw each port down, from snmp_interface_events: the
    events in the window plus each port's last one before it (a port down since before the window
    starts its span at `start`)."""
    ids = tuple(str(r) for r in routers)
    if not ids:
        return {}
    try:
        res = await db.execute(text("""
            SELECT endpoint_id, if_index, kind, at FROM snmp_interface_events
            WHERE endpoint_id = ANY(CAST(:ids AS uuid[])) AND kind IN ('up', 'down') AND if_index IS NOT NULL
              AND at >= :start AND at < :end
            UNION ALL
            (SELECT DISTINCT ON (endpoint_id, if_index) endpoint_id, if_index, kind, at FROM snmp_interface_events
             WHERE endpoint_id = ANY(CAST(:ids AS uuid[])) AND kind IN ('up', 'down') AND if_index IS NOT NULL
               AND at < :start
             ORDER BY endpoint_id, if_index, at DESC)
        """), {"ids": ids, "start": start, "end": end})
        rows = res.all()
    except Exception:
        return {}
    events: Dict[LinkKey, List[tuple]] = {}
    for ep, idx, kind, at in rows:
        events.setdefault((UUID(str(ep)), int(idx)), []).append((at, str(kind)))
    spans: Dict[LinkKey, List[tuple]] = {}
    for key, evs in events.items():
        opened: Optional[datetime.datetime] = None
        for at, kind in sorted(evs):
            if kind == "down" and opened is None:
                opened = max(at, start)
            elif kind == "up" and opened is not None:
                if at > start:
                    spans.setdefault(key, []).append((opened, at))
                opened = None
        if opened is not None:
            spans.setdefault(key, []).append((opened, None))
    return spans


def _series(
    link: wan_edge.WanInterface,
    grid: List[datetime.datetime],
    bucket_seconds: int,
    minutes: Optional[Dict[datetime.datetime, tuple]],
    hours: Sequence[LinkHour],
    usual: traffic_usual.LinkUsual,
    spans: List[tuple],
) -> LinkSeries:
    by_hour = {h.bucket: h for h in hours}
    s = LinkSeries(bucket_seconds=bucket_seconds, times=grid, in_bps=[], out_bps=[], peak_in_bps=[],
                   peak_out_bps=[], usual_in_low=[], usual_in_high=[], usual_out_low=[], usual_out_high=[],
                   down_spans=spans)
    busiest = 0.0
    for t in grid:
        if minutes is not None:
            row = minutes.get(t)
            vin, vout = (bits_per_second(row[0], bucket_seconds), bits_per_second(row[1], bucket_seconds)) if row else (None, None)
            pin = pout = None
        else:
            h = by_hour.get(t)
            vin = bits_per_second(h.in_bytes, bucket_seconds) if h else None
            vout = bits_per_second(h.out_bytes, bucket_seconds) if h else None
            pin = bits_per_second(h.peak_in_minute_bytes, 60.0) if h else None
            pout = bits_per_second(h.peak_out_minute_bytes, 60.0) if h else None
        s.in_bps.append(vin)
        s.out_bps.append(vout)
        s.peak_in_bps.append(pin)
        s.peak_out_bps.append(pout)
        band_in, band_out = usual.band(t, "in"), usual.band(t, "out")
        s.usual_in_low.append(band_in.low_bps if band_in else None)
        s.usual_in_high.append(band_in.high_bps if band_in else None)
        s.usual_out_low.append(band_out.low_bps if band_out else None)
        s.usual_out_high.append(band_out.high_bps if band_out else None)
        busiest = max(busiest, *(v for v in (vin, vout, pin, pout) if v is not None), 0.0)
    s.window_peak_bps = busiest
    if link.speed_mbps:
        s.window_peak_pct = busiest / (link.speed_mbps * 1_000_000.0) * 100.0
    return s


def _site_key(site: Optional[str]):
    return (site is None, (site or "").lower())


def _sort(rows: List[WanLinkNow]) -> List[WanLinkNow]:
    """Grouped by site (alphabetical, links of routers with no site last), so a site stays where
    it was from one update to the next; within a site the busiest link first. A link with no speed
    has no utilisation and follows the others, by traffic."""
    def key(r: WanLinkNow):
        util = r.utilization_pct
        return (
            _site_key(r.link.site),
            util is None,
            -(util or 0.0),
            -(r.in_bps + r.out_bps),
            r.link.exporter_hostname.lower(),
            r.link.interface_idx,
        )
    return sorted(rows, key=key)


async def wan_links_now(
    db: AsyncSession,
    routers: Optional[List[UUID]],
    now: Optional[datetime.datetime] = None,
    tz: Optional[datetime.tzinfo] = None,
    window: Optional[datetime.timedelta] = None,
) -> WanLinksNow:
    """Every WAN link of the routers in scope (`routers`, None for all), as of `now`; with a
    `window`, each with its chart over it (LinkSeries)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    tz = tz or server_zone()
    window_end = now.astimezone(datetime.timezone.utc).replace(second=0, microsecond=0)
    window_start = window_end - datetime.timedelta(minutes=NOW_MINUTES)
    hour_start = window_end.replace(minute=0)
    local_today = now.astimezone(tz).date()
    day_start = datetime.datetime.combine(local_today, datetime.time(0), tzinfo=tz).astimezone(datetime.timezone.utc)

    interfaces = await wan_edge.list_wan_interfaces(db, exporter_ids=routers)
    keys = [(w.exporter_id, w.interface_idx) for w in interfaces]
    totals = await wan_edge.wan_link_totals(db, interfaces, window_start, window_end)
    # Chart buckets: complete minutes (or hours) back over the window, the one still filling left
    # out, as on the combined chart.
    grid: List[datetime.datetime] = []
    bucket_seconds = 60
    if window is not None:
        bucket_seconds = 60 if window <= MINUTE_SERIES_UP_TO else 3600
        last = window_end if bucket_seconds == 60 else hour_start
        step = datetime.timedelta(seconds=bucket_seconds)
        count = max(1, int(window.total_seconds() // bucket_seconds))
        grid = [last - step * (count - i) for i in range(count)]
    # One read for the usual range (28 days before this hour), today's peak (this hour too) and,
    # for an hourly chart, its hours.
    hours_from = hour_start - traffic_usual.LOOKBACK
    if grid and bucket_seconds == 3600:
        hours_from = min(hours_from, grid[0])
    hours = await interface_traffic.link_hours(db, keys, hours_from, hour_start + datetime.timedelta(hours=1))
    minutes = None
    if grid and bucket_seconds == 60:
        minutes = await interface_traffic.link_minutes(db, keys, grid[0], window_end)
    usual = traffic_usual.usual_from_hours(hours, now, tz)
    router_ids = sorted({w.exporter_id for w in interfaces}, key=str)
    snmp = await _snmp_view(db, router_ids)
    alerts = await _alerts(db, router_ids)
    spans = await down_spans(db, router_ids, grid[0], window_end) if grid else {}

    seconds = float(NOW_MINUTES * 60)
    rows: List[WanLinkNow] = []
    for w in interfaces:
        key = (w.exporter_id, w.interface_idx)
        t = totals.get(key, {})
        in_bps = bits_per_second(t.get("in_bytes", 0), seconds)
        out_bps = bits_per_second(t.get("out_bytes", 0), seconds)
        link_usual = usual.get(key) or traffic_usual.LinkUsual(days=0, grouping=None, tz=tz)
        band_in, band_out = link_usual.band(now, "in"), link_usual.band(now, "out")
        today = [h for h in hours.get(key, []) if h.bucket >= day_start]
        peak_in, peak_in_hour = _peak(today, "in")
        peak_out, peak_out_hour = _peak(today, "out")
        device = snmp["devices"].get(str(w.exporter_id))
        state = _link_state(w, device)
        rows.append(WanLinkNow(
            link=w,
            in_bps=in_bps,
            out_bps=out_bps,
            from_snmp=bool(t.get("from_snmp")),
            utilization_pct=utilization_pct(max(in_bps, out_bps), w.speed_mbps) if w.speed_mbps else None,
            usual=link_usual,
            usual_in=band_in,
            usual_out=band_out,
            above_usual_in_pct=traffic_usual.above_usual_pct(in_bps, band_in),
            above_usual_out_pct=traffic_usual.above_usual_pct(out_bps, band_out),
            peak_in_bps=peak_in,
            peak_in_hour=peak_in_hour,
            peak_out_bps=peak_out,
            peak_out_hour=peak_out_hour,
            link_state=state,
            # Only while SNMP says the port is down: an up event since closes it.
            down_since=snmp["downs"].get(key) if state == "down" else None,
            snmp_last_ok_at=device[1] if device else None,
            alerts=alerts.get(key, []),
            series=_series(
                w, grid, bucket_seconds, minutes.get(key, {}) if minutes is not None else None,
                hours.get(key, []), link_usual, spans.get(key, []),
            ) if grid else None,
        ))

    return WanLinksNow(
        window_start=window_start,
        window_end=window_end,
        day_start=day_start,
        tz_name=str(getattr(tz, "key", None) or tz),
        links=_sort(rows),
    )
