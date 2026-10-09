"""The one shared IST zone (app.core.timezones) and every place that uses it: day / month boundaries for dashboard
stats and exports, CSV timestamps and bot message times. Semantics are unchanged by the consolidation."""

from datetime import UTC, date, datetime, timedelta

from app.bots import format as fmt
from app.core.timezones import IST
from app.dashboard import csv_export
from app.dashboard import validation as v


def test_ist_is_a_fixed_plus_0530_named_zone():
    assert IST.utcoffset(None) == timedelta(hours=5, minutes=30) and IST.tzname(None) == "IST"
    assert IST.dst(None) is None  # no daylight saving
    assert v.IST is IST and fmt.IST is IST and csv_export.IST is IST  # one definition


def test_utc_to_ist_conversion_and_the_1830_utc_day_boundary():
    before = datetime(2026, 10, 6, 18, 29, 59, tzinfo=UTC)
    after = datetime(2026, 10, 6, 18, 30, 0, tzinfo=UTC)
    assert before.astimezone(IST).isoformat() == "2026-10-06T23:59:59+05:30"
    assert after.astimezone(IST).isoformat() == "2026-10-07T00:00:00+05:30"
    assert v.ist_day_bounds(before) == (datetime(2026, 10, 6, tzinfo=IST), datetime(2026, 10, 7, tzinfo=IST))
    assert v.ist_day_bounds(after) == (datetime(2026, 10, 7, tzinfo=IST), datetime(2026, 10, 8, tzinfo=IST))
    start, end = v.ist_day_bounds(after)
    assert start.astimezone(UTC) == after and end - start == timedelta(days=1)


def test_month_boundaries_including_the_year_end():
    new_year_ist = datetime(2026, 12, 31, 18, 30, tzinfo=UTC)  # 2027-01-01 00:00 IST
    assert v.ist_month_bounds(new_year_ist) == (datetime(2027, 1, 1, tzinfo=IST), datetime(2027, 2, 1, tzinfo=IST))
    last_minute_of_2026 = datetime(2026, 12, 31, 18, 29, tzinfo=UTC)
    assert v.ist_month_bounds(last_minute_of_2026) == (datetime(2026, 12, 1, tzinfo=IST),
                                                       datetime(2027, 1, 1, tzinfo=IST))
    assert v.ist_midnight(date(2026, 2, 28)).astimezone(UTC) == datetime(2026, 2, 27, 18, 30, tzinfo=UTC)


def test_bot_and_csv_formatting_unchanged():
    t = datetime(2026, 10, 6, 18, 45, 30, tzinfo=UTC)  # 00:15:30 IST on 7 October
    assert fmt.when(t) == "07/10/2026, 12:15:30 AM"
    assert fmt.when(datetime(2026, 10, 7, 7, 0, 0, tzinfo=UTC)) == "07/10/2026, 12:30:00 PM"
    assert csv_export.cell(t) == "2026-10-07T00:15:30+05:30"
