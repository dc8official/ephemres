"""The usual range of a WAN link's traffic for each hour of the week (v3.3.6 build plan 7.5 step 3).

The Traffic page's headline compares each link now with what is usual for this hour ("40% above
usual"), and the per-link charts shade the usual range (review O1, O2, decided with Kenneth
2026-11-13). Worked out on request from the last 28 days of hourly averages, read through
interface_traffic.link_hours, so SNMP counters are used where a link has them and flows
otherwise, the same as every other interface figure. No table: a link has at most 672 hours in
28 days, and one query covers every link in scope.

The rules:
- Hours are taken in the server's timezone (timezone_utils.server_zone), so office hours line
  up and the weeks either side of a clock change still match.
- A link with under 21 days of data groups its hours by weekday vs weekend (Saturday and
  Sunday): 7 days already give about 5 weekday samples for each hour. From 21 days it groups
  by day of week, which separates a Monday morning rush from a quiet Friday afternoon.
- A cell (an hour of a weekday, say) shows a band once it has 3 samples. With 5 or more, the
  band runs from the second-lowest to the second-highest sample, so one odd day (an outage, a
  one-off transfer) does not stretch it; with 3 or 4, from the lowest to the highest.
- An hour with no row on a day the link reported anything counts as 0: the minute tables only
  hold minutes with traffic, so a silent night hour is a real zero. Days with nothing at all
  (before the link was set up, or the router was away all day) are left out, and so are the
  hours before the link's first row in the 28 days.
- "Above usual" means more than 10% above the band's top (ABOVE_USUAL_MARGIN, Kenneth
  2026-11-13); the note gives the percentage over the top. Without the margin, a link a hair
  over a narrow band (steady links have narrow ones) would read "2% above usual" all day.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.core.orm import AsyncSession
from app.services import interface_traffic
from app.services.interface_traffic import LinkHour, LinkKey
from app.services.timezone_utils import server_zone

LOOKBACK = datetime.timedelta(days=28)
DAY_OF_WEEK_FROM_DAYS = 21
MIN_SAMPLES = 3
TRIM_FROM_SAMPLES = 5
# How far over the band's top counts as "above usual" (0.10: more than 10% over it).
ABOVE_USUAL_MARGIN = 0.10
WEEKEND = (5, 6)  # date.weekday(): Saturday, Sunday

WEEKDAY_WEEKEND = "weekday_weekend"
DAY_OF_WEEK = "day_of_week"

_HOUR = datetime.timedelta(hours=1)

# A cell: (group, local hour). The group is 0 (weekday) or 1 (weekend) under WEEKDAY_WEEKEND,
# else the day of week (0 = Monday).
Cell = Tuple[int, int]


@dataclass(frozen=True)
class Band:
    low_bps: float
    high_bps: float
    samples: int


@dataclass
class LinkUsual:
    """One link's usual ranges. `grouping` is None while the link has no data in the 28 days."""
    days: int
    grouping: Optional[str]
    tz: datetime.tzinfo
    bands_in: Dict[Cell, Band] = field(default_factory=dict)
    bands_out: Dict[Cell, Band] = field(default_factory=dict)

    def cell(self, when: datetime.datetime) -> Cell:
        return _cell(when.astimezone(self.tz), self.grouping or WEEKDAY_WEEKEND)

    def band(self, when: datetime.datetime, direction: str = "in") -> Optional[Band]:
        """The usual range for the hour `when` falls in, or None (too few samples so far)."""
        if self.grouping is None:
            return None
        bands = self.bands_in if direction == "in" else self.bands_out
        return bands.get(self.cell(when))


def _cell(local: datetime.datetime, grouping: str) -> Cell:
    day = local.weekday()
    group = day if grouping == DAY_OF_WEEK else (1 if day in WEEKEND else 0)
    return group, local.hour


def band_from(samples: Sequence[float]) -> Optional[Band]:
    """The band for one cell's samples (the rules in the module docstring)."""
    n = len(samples)
    if n < MIN_SAMPLES:
        return None
    ordered = sorted(samples)
    if n >= TRIM_FROM_SAMPLES:
        return Band(low_bps=ordered[1], high_bps=ordered[-2], samples=n)
    return Band(low_bps=ordered[0], high_bps=ordered[-1], samples=n)


def above_usual_pct(value_bps: float, band: Optional[Band]) -> Optional[float]:
    """How far `value_bps` is above the band's top, in percent, once it is more than
    ABOVE_USUAL_MARGIN over it; None otherwise, and when there is no band or its top is 0 (any
    traffic would be "infinitely" above)."""
    if band is None or band.high_bps <= 0:
        return None
    pct = (value_bps - band.high_bps) / band.high_bps * 100.0
    return pct if pct > ABOVE_USUAL_MARGIN * 100.0 else None


def _bps(nbytes: int) -> float:
    return nbytes * 8 / 3600.0


def _utc(when: datetime.datetime) -> datetime.datetime:
    return when.astimezone(datetime.timezone.utc)


def usual_from_hours(
    hours: Mapping[LinkKey, Iterable[LinkHour]],
    now: datetime.datetime,
    tz: datetime.tzinfo,
) -> Dict[LinkKey, LinkUsual]:
    """The usual ranges from each link's hours. Only complete hours in the 28 days before the
    current hour count; the current hour is what gets compared."""
    end = _utc(now).replace(minute=0, second=0, microsecond=0)
    start = end - LOOKBACK
    result: Dict[LinkKey, LinkUsual] = {}
    for key, link_rows in hours.items():
        rows = {_utc(h.bucket): h for h in link_rows if start <= _utc(h.bucket) < end}
        if not rows:
            result[key] = LinkUsual(days=0, grouping=None, tz=tz)
            continue
        observed = {b.astimezone(tz).date() for b in rows}
        grouping = DAY_OF_WEEK if len(observed) >= DAY_OF_WEEK_FROM_DAYS else WEEKDAY_WEEKEND
        samples_in: Dict[Cell, List[float]] = defaultdict(list)
        samples_out: Dict[Cell, List[float]] = defaultdict(list)
        t = min(rows)
        while t < end:
            local = t.astimezone(tz)
            if local.date() in observed:
                row = rows.get(t)
                cell = _cell(local, grouping)
                samples_in[cell].append(_bps(row.in_bytes) if row else 0.0)
                samples_out[cell].append(_bps(row.out_bytes) if row else 0.0)
            t += _HOUR
        usual = LinkUsual(days=len(observed), grouping=grouping, tz=tz)
        for samples, bands in ((samples_in, usual.bands_in), (samples_out, usual.bands_out)):
            for cell, values in samples.items():
                band = band_from(values)
                if band is not None:
                    bands[cell] = band
        result[key] = usual
    return result


async def usual_for_links(
    db: AsyncSession,
    links: Sequence[LinkKey],
    now: Optional[datetime.datetime] = None,
    tz: Optional[datetime.tzinfo] = None,
) -> Dict[LinkKey, LinkUsual]:
    """The usual ranges of `links` (exporter id, interface index), with one query for all of them."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    end = _utc(now).replace(minute=0, second=0, microsecond=0)
    hours = await interface_traffic.link_hours(db, links, end - LOOKBACK, end)
    return usual_from_hours(hours, now, tz or server_zone())
