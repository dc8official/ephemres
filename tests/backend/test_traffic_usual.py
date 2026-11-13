"""The usual band of a WAN link (v3.3.6 build plan 7.5 step 3): services/traffic_usual.

The rules are pure functions over a link's hours, tested here without a database: weeks of
history can't be seeded into the minute tables (kept 7 days) or read back through the hourly
aggregates below their watermark. tests/integration/test_link_hours.py covers the query.
"""
import datetime
import uuid
from zoneinfo import ZoneInfo

import pytest

from app.services.interface_traffic import LinkHour
from app.services.traffic_usual import (
    DAY_OF_WEEK,
    WEEKDAY_WEEKEND,
    Band,
    above_usual_pct,
    band_from,
    usual_from_hours,
)

UTC = datetime.timezone.utc
LAGOS = ZoneInfo("Africa/Lagos")  # UTC+1, no summer time
LONDON = ZoneInfo("Europe/London")
LINK = (uuid.UUID("55555555-0000-0000-0000-0000000000f1"), 1)
MBPS = 1_000_000


def _hour(when, in_bps, out_bps=0.0):
    """One hourly row whose average is `in_bps` / `out_bps`."""
    return LinkHour(
        bucket=when.astimezone(UTC),
        in_bytes=int(in_bps * 3600 / 8),
        out_bytes=int(out_bps * 3600 / 8),
        peak_in_minute_bytes=0,
        peak_out_minute_bytes=0,
        source="flow",
    )


def _days(first_local_midnight, count, tz, rate_for):
    """`count` days of 24 hours from a local midnight; `rate_for(local_dt)` gives each hour's
    in-rate, or None for no row."""
    rows = []
    for d in range(count):
        day = first_local_midnight + datetime.timedelta(days=d)
        for h in range(24):
            local = datetime.datetime(day.year, day.month, day.day, h, tzinfo=tz)
            rate = rate_for(local)
            if rate is not None:
                rows.append(_hour(local, rate))
    return rows


def _now_after(first_local_midnight, count, tz):
    end = first_local_midnight + datetime.timedelta(days=count)
    return datetime.datetime(end.year, end.month, end.day, 0, 30, tzinfo=tz)


@pytest.mark.parametrize("samples, expected", [
    ([5.0, 1.0], None),  # under 3 samples: no band yet
    ([5.0, 1.0, 3.0], Band(1.0, 5.0, 3)),  # 3 or 4: lowest to highest
    ([5.0, 1.0, 3.0, 9.0], Band(1.0, 9.0, 4)),
    ([5.0, 1.0, 3.0, 9.0, 0.0], Band(1.0, 5.0, 5)),  # 5 or more: second-lowest to second-highest
    ([4.0] * 6 + [100.0], Band(4.0, 4.0, 7)),  # one odd day does not stretch it
])
def test_band_from_samples(samples, expected):
    assert band_from(samples) == expected


def test_above_usual():
    band = Band(2.0 * MBPS, 10.0 * MBPS, 5)
    assert above_usual_pct(14.0 * MBPS, band) == pytest.approx(40.0)
    assert above_usual_pct(10.0 * MBPS, band) is None  # at the top is usual
    # Within 10% over the top is still usual (a steady link has a narrow band); past it, the
    # note gives the whole percentage over the top.
    assert above_usual_pct(11.0 * MBPS, band) is None
    assert above_usual_pct(11.2 * MBPS, band) == pytest.approx(12.0)
    assert above_usual_pct(1.0 * MBPS, band) is None  # below the band isn't "above usual"
    assert above_usual_pct(5.0, None) is None
    assert above_usual_pct(5.0, Band(0.0, 0.0, 5)) is None


def test_a_week_groups_weekdays_and_weekends():
    # Mon 2 Nov 2026 to Sun 8 Nov: office traffic 09:00-17:00 on weekdays, rising each day;
    # 1 Mb/s at weekends' 09:00; nothing (no rows) at night.
    monday = datetime.date(2026, 11, 2)

    def rate(local):
        if local.weekday() >= 5:
            return 1 * MBPS if local.hour == 9 else None
        return (10 + local.weekday()) * MBPS if 9 <= local.hour < 17 else None

    rows = _days(monday, 7, LAGOS, rate)
    usual = usual_from_hours({LINK: rows}, _now_after(monday, 7, LAGOS), LAGOS)[LINK]
    assert (usual.days, usual.grouping) == (7, WEEKDAY_WEEKEND)

    # Monday's 09:00 is compared with every weekday's 09:00: 10..14 Mb/s, trimmed to 11..13.
    next_monday_9 = datetime.datetime(2026, 11, 9, 9, 20, tzinfo=LAGOS)
    assert usual.band(next_monday_9) == Band(11 * MBPS, 13 * MBPS, 5)
    assert above_usual_pct(18.2 * MBPS, usual.band(next_monday_9)) == pytest.approx(40.0)
    # Weekend 09:00 has 2 samples so far: no band.
    assert usual.band(datetime.datetime(2026, 11, 14, 9, tzinfo=LAGOS)) is None
    # A weekday night hour with no rows on days the link reported is a real zero (Tue to Fri:
    # Monday 03:00 is before the link's first row).
    assert usual.band(datetime.datetime(2026, 11, 9, 3, tzinfo=LAGOS)) == Band(0.0, 0.0, 4)
    # Out has its own band (nothing sent here).
    assert usual.band(next_monday_9, "out") == Band(0.0, 0.0, 5)


def test_from_21_days_each_day_of_the_week_is_its_own():
    # Three weeks from Mon 19 Oct 2026: Mondays are busy (30 Mb/s at 09:00), Fridays quiet (5).
    first = datetime.date(2026, 10, 19)

    def rate(local):
        if local.hour != 9:
            return None
        return {0: 30 * MBPS, 4: 5 * MBPS}.get(local.weekday(), 12 * MBPS)

    rows = _days(first, 21, LAGOS, rate)
    usual = usual_from_hours({LINK: rows}, _now_after(first, 21, LAGOS), LAGOS)[LINK]
    assert (usual.days, usual.grouping) == (21, DAY_OF_WEEK)
    assert usual.band(datetime.datetime(2026, 11, 9, 9, tzinfo=LAGOS)) == Band(30 * MBPS, 30 * MBPS, 3)
    assert usual.band(datetime.datetime(2026, 11, 13, 9, tzinfo=LAGOS)) == Band(5 * MBPS, 5 * MBPS, 3)
    # Under the weekday grouping, Monday and Friday would share one band.
    shorter = usual_from_hours({LINK: rows[-20:]}, _now_after(first, 21, LAGOS), LAGOS)[LINK]
    assert shorter.grouping == WEEKDAY_WEEKEND


def test_hours_are_the_servers_local_hours():
    # 08:00 UTC is 09:00 in Lagos: the morning peak lands in the 09:00 cell.
    monday = datetime.date(2026, 11, 2)
    rows = _days(monday, 5, LAGOS, lambda local: 20 * MBPS if local.hour == 9 else None)
    assert rows[0].bucket.hour == 8
    usual = usual_from_hours({LINK: rows}, _now_after(monday, 5, LAGOS), LAGOS)[LINK]
    assert usual.band(datetime.datetime(2026, 11, 9, 9, tzinfo=LAGOS)).high_bps == 20 * MBPS
    assert usual.band(datetime.datetime(2026, 11, 9, 8, tzinfo=LAGOS)).high_bps == 0.0


def test_summer_time_changes_follow_the_clock():
    # London leaves summer time on Sun 25 Oct 2026. Office traffic at 09:00 local every day is
    # 08:00 UTC before the change and 09:00 UTC after: all of it stays in the 09:00 cell.
    first = datetime.date(2026, 10, 19)
    rows = _days(first, 14, LONDON, lambda local: 50 * MBPS if local.hour == 9 and local.weekday() < 5 else None)
    assert {r.bucket.hour for r in rows} == {8, 9}
    usual = usual_from_hours({LINK: rows}, _now_after(first, 14, LONDON), LONDON)[LINK]
    nine = usual.band(datetime.datetime(2026, 11, 2, 9, tzinfo=LONDON))
    assert nine == Band(50 * MBPS, 50 * MBPS, 10)
    assert usual.band(datetime.datetime(2026, 11, 2, 8, tzinfo=LONDON)).high_bps == 0.0


def test_days_without_data_and_hours_before_the_first_row_are_left_out():
    monday = datetime.date(2026, 11, 2)

    def rate(local):
        # The link starts reporting Monday 12:00; Wednesday nothing at all (router away).
        if local.date() == datetime.date(2026, 11, 2) and local.hour < 12:
            return None
        if local.weekday() == 2:
            return None
        return 8 * MBPS if 9 <= local.hour < 17 else None

    rows = _days(monday, 5, LAGOS, rate)
    usual = usual_from_hours({LINK: rows}, _now_after(monday, 5, LAGOS), LAGOS)[LINK]
    assert usual.days == 4
    # 09:00: Tue, Thu, Fri (Monday 09:00 is before the first row, Wednesday has no data).
    assert usual.band(datetime.datetime(2026, 11, 9, 9, tzinfo=LAGOS)) == Band(8 * MBPS, 8 * MBPS, 3)
    # 13:00: Mon, Tue, Thu, Fri.
    assert usual.band(datetime.datetime(2026, 11, 9, 13, tzinfo=LAGOS)).samples == 4


def test_the_current_hour_and_older_than_28_days_are_not_samples():
    monday = datetime.date(2026, 11, 2)
    rows = _days(monday, 5, UTC, lambda local: 4 * MBPS if local.hour == 10 else None)
    now = datetime.datetime(2026, 11, 6, 10, 40, tzinfo=UTC)  # Friday 10:40: still filling
    rows.append(_hour(datetime.datetime(2026, 9, 1, 10, tzinfo=UTC), 99 * MBPS))  # too old
    usual = usual_from_hours({LINK: rows}, now, UTC)[LINK]
    assert usual.band(now) == Band(4 * MBPS, 4 * MBPS, 4)  # Mon to Thu
    assert usual.days == 4  # Friday's only row is in the current hour


def test_a_link_with_no_data():
    usual = usual_from_hours({LINK: []}, datetime.datetime(2026, 11, 6, tzinfo=UTC), UTC)[LINK]
    assert (usual.days, usual.grouping) == (0, None)
    assert usual.band(datetime.datetime(2026, 11, 6, 9, tzinfo=UTC)) is None
