"""netkeeper.campaigns.schedule (spec 11.4; item P3-06): send windows, holidays, spacing."""

from __future__ import annotations

import random
import statistics
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from netkeeper.campaigns import schedule
from netkeeper.campaigns.schedule import SendWindow, WindowError, send_window, spacing_delay
from netkeeper.config import CampaignSettings

NY = "America/New_York"
DEFAULTS = CampaignSettings()


def at(text: str, zone: str = NY) -> datetime:
    """A local wall time as an aware UTC datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=schedule.zone(zone)).astimezone(UTC)


def window(override: dict[str, Any] | None = None, **settings: Any) -> SendWindow:
    return send_window(CampaignSettings(**settings), NY, override)


def test_the_default_window_is_the_spec_one() -> None:
    """Spec 11.4: Tuesday to Thursday, 09:00 to 16:30, local time."""
    got = window()
    assert got.days == frozenset({1, 2, 3})
    assert (got.start, got.end) == (time(9, 0), time(16, 30))
    assert got.holidays == frozenset()
    assert str(got.zone) == NY


@pytest.mark.parametrize(
    ("now", "opens"),
    [
        ("2026-09-28 12:00", "2026-09-29 09:00"),  # Monday: Tuesday morning
        ("2026-09-29 08:59", "2026-09-29 09:00"),
        ("2026-09-29 09:00", "2026-09-29 09:00"),  # the start is inside
        ("2026-09-29 12:34", "2026-09-29 12:34"),
        ("2026-09-29 16:29", "2026-09-29 16:29"),
        ("2026-09-29 16:30", "2026-09-30 09:00"),  # the end is not
        ("2026-10-01 17:00", "2026-10-06 09:00"),  # Thursday evening: next Tuesday
        ("2026-10-03 03:00", "2026-10-06 09:00"),  # Saturday
    ],
)
def test_next_open_is_the_earliest_instant_inside(now: str, opens: str) -> None:
    assert window().next_open(at(now)) == at(opens)
    assert window().is_open(at(now)) == (now == opens)


def test_a_holiday_has_no_window() -> None:
    """``[campaigns] holidays``: the whole local day is shut, for every campaign."""
    shut = window(holidays=("2026-09-29", "2026-09-30"))
    assert shut.next_open(at("2026-09-29 10:00")) == at("2026-10-01 09:00")
    assert not shut.is_open(at("2026-09-30 12:00"))


def test_a_campaign_overrides_days_hours_or_both() -> None:
    weekend = window({"days": ["Sat", "Sun"]})
    assert weekend.next_open(at("2026-09-29 10:00")) == at("2026-10-03 09:00")
    late = window({"hours": ["18:00", "20:00"]})
    assert late.next_open(at("2026-09-29 10:00")) == at("2026-09-29 18:00")
    assert window({}) == window()


def test_the_window_is_local_to_the_users_zone_across_daylight_saving() -> None:
    """09:00 in New York is 13:00 UTC in summer time and 14:00 in winter."""
    before = window().next_open(datetime(2026, 11, 2, tzinfo=UTC))  # Monday after the change
    assert before == datetime(2026, 11, 3, 14, 0, tzinfo=UTC)
    after = window().next_open(datetime(2026, 3, 9, tzinfo=UTC))  # Monday after the change
    assert after == datetime(2026, 3, 10, 13, 0, tzinfo=UTC)


def test_the_local_day_follows_the_zone() -> None:
    """The cap counts per local day (spec 11.4): 23:30 in New York is tomorrow in UTC."""
    got = window()
    late = at("2026-09-29 23:30")
    assert late.date() == date(2026, 9, 30)
    assert got.local_date(late) == date(2026, 9, 29)
    assert got.day_bounds(late) == (at("2026-09-29 00:00"), at("2026-09-30 00:00"))
    # The spring-forward day is 23 hours long.
    start, end = got.day_bounds(at("2026-03-08 12:00"))
    assert end - start == timedelta(hours=23)


def test_a_window_that_never_opens_says_so() -> None:
    """The engine sends nothing rather than guessing a window."""
    assert window({"days": []}).next_open(at("2026-09-29 10:00")) is None
    every_day_off = tuple(
        (date(2026, 9, 29) + timedelta(days=n)).isoformat() for n in range(schedule.SEARCH_DAYS)
    )
    assert (
        window(send_window_days=("Tue",), holidays=every_day_off).next_open(at("2026-09-29 10:00"))
        is None
    )


@pytest.mark.parametrize(
    ("override", "settings", "match"),
    [
        ({"days": ["Tuesday"]}, {}, "not a day"),
        ({"days": "Tue"}, {}, "list of day names"),
        ({"hours": ["9:00", "16:30"]}, {}, "HH:MM"),
        ({"hours": ["09:00", "25:00"]}, {}, "HH:MM"),
        ({"hours": ["16:30", "09:00"]}, {}, "empty"),
        ({"hours": ["09:00", "09:00"]}, {}, "empty"),
        ({"hours": ["09:00"]}, {}, r"\[start, end\]"),
        ({"days": ["Tue"], "timezone": "UTC"}, {}, "unknown send window keys: timezone"),
        (None, {"holidays": ("2026-13-01",)}, "not a date"),
        (None, {"send_window_days": ("Tue", "Funday")}, "not a day"),
        (None, {"send_window_hours": ("09:00", "08:00")}, "empty"),
    ],
)
def test_a_window_that_cannot_be_read_is_refused(
    override: dict[str, Any] | None, settings: dict[str, Any], match: str
) -> None:
    with pytest.raises(WindowError, match=match):
        window(override, **settings)


def test_an_unknown_zone_is_refused() -> None:
    with pytest.raises(WindowError, match="not a time zone"):
        send_window(DEFAULTS, "Mars/Olympus_Mons")


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        window().next_open(datetime(2026, 9, 29, 10))


def test_spacing_is_human_and_never_under_the_floor() -> None:
    """Spec 11.4: ``human_delay`` with a median of 4 minutes and a floor of 90 seconds."""
    rng = random.Random(7)
    gaps = [spacing_delay(rng, median_s=240, floor_s=90).total_seconds() for _ in range(4000)]
    assert min(gaps) >= 90
    assert 200 < statistics.median(gaps) < 300
    assert len({round(g) for g in gaps}) > 500  # jittered, not a fixed beat


@pytest.mark.parametrize(("median", "floor"), [(0, 90), (240, 0), (-1, 90), (240, float("nan"))])
def test_spacing_refuses_no_spacing(median: float, floor: float) -> None:
    with pytest.raises(ValueError, match="positive"):
        spacing_delay(random.Random(1), median_s=median, floor_s=floor)


def test_the_window_keys_and_day_names_are_pinned() -> None:
    assert frozenset({"days", "hours"}) == schedule.WINDOW_KEYS
    assert schedule.DAY_NAMES == ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert schedule.SEARCH_DAYS == 400
