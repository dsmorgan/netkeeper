"""netkeeper.campaigns.schedule (spec 11.4; P3-06, #338): suggested slots, the default
start, step due times, the overnight spill, and spacing."""

from __future__ import annotations

import random
import statistics
from datetime import UTC, date, datetime, time, timedelta

import pytest

from netkeeper.campaigns import schedule
from netkeeper.campaigns.schedule import (
    ScheduleError,
    Suggested,
    default_start,
    spacing_delay,
    spill,
    step_due,
    suggested,
)
from netkeeper.config import CampaignSettings
from netkeeper.localtime import local_today

NY = "America/New_York"
DEFAULTS = CampaignSettings()


def at(text: str, zone: str = NY) -> datetime:
    """A local wall time as an aware UTC datetime."""
    return datetime.fromisoformat(text).replace(tzinfo=schedule.zone(zone)).astimezone(UTC)


def slots(*holidays: str) -> Suggested:
    return suggested(CampaignSettings(holidays=holidays), NY)


def test_the_suggested_slots_and_default_start_are_pinned() -> None:
    """#338: Tuesday to Thursday, 09:00 to 16:30 local, and a default start of Tuesday at 09:00.
    Written out here, not read from the module, so a change to either fails."""
    assert schedule.SUGGESTED_DAYS == ("Tue", "Wed", "Thu")
    assert schedule.SUGGESTED_HOURS == ("09:00", "16:30")
    assert (schedule.DEFAULT_START_DAY, schedule.DEFAULT_START_TIME) == ("Tue", "09:00")
    assert schedule.SUGGESTION == "Most effective: Tue–Thu mornings."  # noqa: RUF001
    assert schedule.SERVE_REMINDER == (
        "netkeeper sends only while `serve` is running and this Mac is awake."
        " Keep it running from the start time until the batch finishes."
    )
    got = slots()
    assert got.days == frozenset({1, 2, 3})
    assert (got.start, got.end) == (time(9, 0), time(16, 30))
    assert got.holidays == frozenset()
    assert str(got.zone) == NY
    assert schedule.DAY_NAMES == ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    assert schedule.SEARCH_DAYS == 400


# --- the default start ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "start"),
    [
        ("2026-09-28 12:00", "2026-09-29 09:00"),  # Monday: tomorrow
        ("2026-09-29 08:59", "2026-09-29 09:00"),  # Tuesday before 09:00: today
        ("2026-09-29 00:00", "2026-09-29 09:00"),
        ("2026-09-29 09:00", "2026-10-06 09:00"),  # Tuesday at 09:00: next week
        ("2026-09-29 09:01", "2026-10-06 09:00"),  # Tuesday after 09:00: next week
        ("2026-09-30 10:00", "2026-10-06 09:00"),  # Wednesday
        ("2026-10-01 10:00", "2026-10-06 09:00"),  # Thursday
        ("2026-10-02 10:00", "2026-10-06 09:00"),  # Friday
        ("2026-10-03 10:00", "2026-10-06 09:00"),  # Saturday
        ("2026-10-04 10:00", "2026-10-06 09:00"),  # Sunday
        ("2026-10-05 23:59", "2026-10-06 09:00"),  # Monday night
    ],
)
def test_the_default_start_is_the_next_tuesday_at_nine(now: str, start: str) -> None:
    assert default_start(at(now), slots()) == at(start)


def test_the_default_start_skips_a_holiday() -> None:
    """Holidays steer the suggestion: a Tuesday off moves the default to Wednesday 09:00."""
    assert default_start(at("2026-09-28 12:00"), slots("2026-09-29")) == at("2026-09-30 09:00")


def test_the_default_start_is_local_across_daylight_saving() -> None:
    """09:00 in New York is 13:00 UTC in summer time and 14:00 in winter."""
    assert default_start(datetime(2026, 11, 2, tzinfo=UTC), slots()) == datetime(
        2026, 11, 3, 14, 0, tzinfo=UTC
    )
    assert default_start(datetime(2026, 3, 9, tzinfo=UTC), slots()) == datetime(
        2026, 3, 10, 13, 0, tzinfo=UTC
    )


# --- suggested slots --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "slot"),
    [
        ("2026-09-28 12:00", "2026-09-29 09:00"),  # Monday: Tuesday morning
        ("2026-09-29 08:59", "2026-09-29 09:00"),
        ("2026-09-29 09:00", "2026-09-29 09:00"),  # the start is inside
        ("2026-09-29 12:34", "2026-09-29 12:34"),
        ("2026-09-29 16:30", "2026-09-30 09:00"),  # the end is not
        ("2026-10-01 17:00", "2026-10-06 09:00"),  # Thursday evening: next Tuesday
        ("2026-10-03 03:00", "2026-10-06 09:00"),  # Saturday
    ],
)
def test_the_next_slot_is_the_earliest_suggested_instant(now: str, slot: str) -> None:
    assert slots().next_slot(at(now)) == at(slot)
    assert slots().is_suggested(at(now)) == (now == slot)


def test_a_holiday_has_no_suggested_slot() -> None:
    off = slots("2026-09-29", "2026-09-30")
    assert off.next_slot(at("2026-09-29 10:00")) == at("2026-10-01 09:00")
    assert off.is_holiday(at("2026-09-30 12:00"))
    assert not off.is_suggested(at("2026-09-30 12:00"))


def test_holidays_covering_every_day_have_no_slot() -> None:
    every_day_off = tuple(
        (date(2026, 9, 29) + timedelta(days=n)).isoformat() for n in range(schedule.SEARCH_DAYS)
    )
    assert slots(*every_day_off).next_slot(at("2026-09-29 10:00")) is None


def test_the_local_day_follows_the_zone() -> None:
    """The cap counts per local day (spec 11.4): 23:30 in New York is tomorrow in UTC."""
    got = slots()
    late = at("2026-09-29 23:30")
    assert late.date() == date(2026, 9, 30)
    assert got.local_date(late) == date(2026, 9, 29)
    assert got.day_bounds(late) == (at("2026-09-29 00:00"), at("2026-09-30 00:00"))
    start, end = got.day_bounds(at("2026-03-08 12:00"))  # spring forward: 23 hours
    assert end - start == timedelta(hours=23)


# --- step due times -----------------------------------------------------------------------


def test_step_one_with_no_delay_is_due_at_the_start_whenever_it_is() -> None:
    """The start the person chose is honored at any hour: Saturday at 22:00 included."""
    start = at("2026-10-03 22:00")
    assert step_due(start, delay_days=0, send_time=None, slots=slots(), first=True) == start


def test_step_one_with_a_delay_aims_for_the_next_suggested_slot() -> None:
    start = at("2026-09-29 15:00")  # Tuesday
    due = step_due(start, delay_days=2, send_time=None, slots=slots(), first=True)
    assert due == at("2026-10-01 15:00")  # Thursday, inside the slot
    late = step_due(at("2026-09-29 20:00"), delay_days=2, send_time=None, slots=slots(), first=True)
    assert late == at("2026-10-06 09:00")  # Thursday evening: next Tuesday morning


@pytest.mark.parametrize(
    ("latest", "due"),
    [
        ("2026-09-29 10:12", "2026-10-06 10:12"),  # a week on, inside the slot
        ("2026-09-29 18:00", "2026-10-07 09:00"),  # a week on is after hours: next morning
        ("2026-10-02 11:00", "2026-10-13 09:00"),  # Friday + 7: next Tuesday
    ],
)
def test_a_follow_up_lands_in_the_next_suggested_slot_after_its_delay(
    latest: str, due: str
) -> None:
    assert step_due(at(latest), delay_days=7, send_time=None, slots=slots()) == at(due)


def test_a_follow_up_skips_a_holiday() -> None:
    got = step_due(at("2026-09-29 10:00"), delay_days=7, send_time=None, slots=slots("2026-10-06"))
    assert got == at("2026-10-07 09:00")


def test_an_explicit_step_time_is_honored_at_any_hour_and_on_any_day() -> None:
    """Holidays and the suggested slots never move an explicit time."""
    latest = at("2026-09-29 10:12")
    explicit = step_due(
        latest, delay_days=4, send_time="22:00", slots=slots("2026-10-03"), first=False
    )
    assert explicit == at("2026-10-03 22:00")  # Saturday, a holiday, 22:00
    first = step_due(
        at("2026-09-29 09:00"), delay_days=0, send_time="22:00", slots=slots(), first=True
    )
    assert first == at("2026-09-29 22:00")


def test_an_explicit_time_is_never_before_the_step_it_follows() -> None:
    base = at("2026-09-29 15:00")
    assert step_due(base, delay_days=0, send_time="08:00", slots=slots(), first=True) == base


# --- the spill ----------------------------------------------------------------------------


def test_a_due_time_from_an_earlier_day_spills_to_its_own_time_today() -> None:
    """A batch that ran long spills to the next day at the same start time, not overnight."""
    due = at("2026-09-29 22:00")
    assert spill(due, at("2026-09-30 00:01"), slots()) == at("2026-09-30 22:00")
    assert spill(at("2026-09-29 09:00"), at("2026-09-30 03:00"), slots()) == at("2026-09-30 09:00")


def test_a_due_time_from_today_or_one_whose_time_has_come_does_not_spill() -> None:
    assert spill(at("2026-09-29 09:00"), at("2026-09-29 23:00"), slots()) is None
    assert spill(at("2026-09-29 09:00"), at("2026-09-30 09:30"), slots()) is None


# --- what cannot be read ------------------------------------------------------------------


@pytest.mark.parametrize("text", ["9:00", "25:00", "09:60", "0900", "ab:cd", 900, None])
def test_a_time_of_day_that_is_not_hh_mm_is_refused(text: object) -> None:
    with pytest.raises(ScheduleError, match="HH:MM"):
        schedule.parse_clock(text)


def test_a_holiday_that_is_not_a_date_is_refused() -> None:
    with pytest.raises(ScheduleError, match="not a date"):
        slots("2026-13-01")


def test_an_unknown_zone_is_refused() -> None:
    with pytest.raises(ScheduleError, match="not a time zone"):
        suggested(DEFAULTS, "Mars/Olympus_Mons")


def test_a_naive_time_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        slots().next_slot(datetime(2026, 9, 29, 10))
    with pytest.raises(ValueError, match="timezone-aware"):
        default_start(datetime(2026, 9, 29, 10), slots())


# --- spacing --------------------------------------------------------------------------


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


# --- sending hours (#338) ---------------------------------------------------------------

HOURS = schedule.DEFAULT_SENDING_HOURS
ANY_TIME = schedule.sending_hours(enabled=False, days=["Mon"], start="09:00", end="17:00")


def test_the_default_sending_hours_are_pinned() -> None:
    assert schedule.sending_hours_json(HOURS) == {
        "enabled": True,
        "days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "start": "09:00",
        "end": "17:00",
    }
    assert HOURS.describe() == "Mon to Fri, 09:00 to 17:00"
    assert ANY_TIME.describe() == "any time"
    assert schedule.sending_hours_from_json(None) == HOURS


@pytest.mark.parametrize(
    ("days", "start", "end", "match"),
    [
        ([], "09:00", "17:00", "at least one day"),
        (["Funday"], "09:00", "17:00", "not a day"),
        (["Mon"], "17:00", "09:00", "end after"),
        (["Mon"], "09:00", "09:00", "end after"),
        (["Mon"], "9:00", "17:00", "HH:MM"),
    ],
)
def test_sending_hours_that_cannot_be_used_are_refused(
    days: list[str], start: str, end: str, match: str
) -> None:
    with pytest.raises(ScheduleError, match=match):
        schedule.sending_hours(enabled=True, days=days, start=start, end=end)


@pytest.mark.parametrize(
    ("now", "opens"),
    [
        ("2026-09-29 10:00", "2026-09-29 10:00"),  # inside
        ("2026-09-29 16:55", "2026-09-29 16:55"),
        ("2026-09-29 17:00", "2026-09-30 09:00"),  # the end is outside
        ("2026-10-02 22:00", "2026-10-05 09:00"),  # Friday night: Monday
        ("2026-10-03 11:00", "2026-10-05 09:00"),  # Saturday
    ],
)
def test_the_next_opening_of_the_sending_hours(now: str, opens: str) -> None:
    assert schedule.next_opening(at(now), HOURS, slots()) == at(opens)
    assert schedule.next_opening(at(now), ANY_TIME, slots()) == at(now)


def test_hold_exempts_only_the_first_step_on_the_starts_day() -> None:
    start = at("2026-10-02 22:00")  # Friday night
    hold = schedule.hold
    assert (
        hold(
            start,
            at("2026-10-02 23:30"),
            slots=slots(),
            hours=HOURS,
            starts_at=start,
            first_step=True,
        )
        is None
    )
    # A follow-up, the same night: the sending hours.
    assert hold(
        start, at("2026-10-02 23:30"), slots=slots(), hours=HOURS, starts_at=start, first_step=False
    ) == at("2026-10-05 09:00")
    # Step 1 the next day (the cap held it back): the spill, then the sending hours.
    assert hold(
        start, at("2026-10-03 00:05"), slots=slots(), hours=HOURS, starts_at=start, first_step=True
    ) == at("2026-10-05 09:00")
    # Any time: the spill only.
    assert hold(
        start,
        at("2026-10-03 00:05"),
        slots=slots(),
        hours=ANY_TIME,
        starts_at=start,
        first_step=True,
    ) == at("2026-10-03 22:00")


def test_local_today_counts_the_day_in_the_users_zone() -> None:
    now = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
    assert local_today("America/Los_Angeles", now) == date(2026, 9, 20)
    assert local_today("UTC", now) == date(2026, 9, 21)
    assert local_today("Nowhere/Land", now) == date(2026, 9, 21)
    with pytest.raises(ValueError):
        local_today("UTC", datetime(2026, 9, 21, 3, 0))
