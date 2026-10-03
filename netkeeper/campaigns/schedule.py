"""When a campaign sends: its scheduled start, suggested send slots, the local day, and
spacing (spec 11.4, #338).

Pure: no session, no clock. :mod:`netkeeper.services.campaign_engine` asks these
questions on every tick, and activation asks them once.

**No hard window** (#338). netkeeper used to send only inside ``[campaigns]
send_window_days`` and ``send_window_hours``. It no longer limits when a campaign
sends. Each campaign has a **scheduled start** (``campaigns.starts_at``) that it
sends nothing before, and each step can name its own time of day. What is left
here is a *suggestion*:

- **Suggested slots** are Tuesday to Thursday, 09:00 to 16:30 local time
  (:data:`SUGGESTED_DAYS`, :data:`SUGGESTED_HOURS`): the one place these values
  live. ``[campaigns] holidays`` (``YYYY-MM-DD``) are local dates with no
  suggested slot. The suggestion picks the default start and the time a
  follow-up aims for after its delay. It never blocks an explicit time.
- **The default start** is the next Tuesday at 09:00 (:func:`default_start`),
  today when it is Tuesday before 09:00. A holiday moves it to the next
  suggested slot.
- **A step's due time** (:func:`step_due`) is its delay after the step before
  (or after the start, for step 1). A step with an explicit time of day
  (``campaign_steps.send_time``) is due on that day at that time. Without one, a
  follow-up aims for the next suggested slot, and step 1 with no delay is due at
  the start itself.

**Local time** is the user's ``timezone`` (``linkedin.timezone`` keeps it
current, :func:`netkeeper.services.users.ensure_local_user`): the same zone the
LinkedIn budgets count their days in, so "today" means one thing everywhere.

**Safety.** A time zone or holiday that cannot be read, or a time of day that is
not ``HH:MM``, raises :class:`ScheduleError`, and the engine sends nothing for
that campaign. Nothing here guesses.

Comparisons are made in UTC: two aware datetimes that share a ``ZoneInfo``
compare by wall time, which is wrong across a daylight-saving fold.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from netkeeper.config import CampaignSettings
from netkeeper.linkedin.pacing import human_delay

DAY_NAMES: Final[tuple[str, ...]] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
"""Weekday names, Monday first (``date.weekday()`` order)."""

SUGGESTED_DAYS: Final[tuple[str, ...]] = ("Tue", "Wed", "Thu")
"""The days a send is suggested on (#338). A suggestion only: nothing is refused outside it."""

SUGGESTED_HOURS: Final[tuple[str, str]] = ("09:00", "16:30")
"""The local span, ``[start, end)``, a send is suggested in on a suggested day (#338)."""

DEFAULT_START_DAY: Final = "Tue"
DEFAULT_START_TIME: Final = "09:00"
"""The default scheduled start: the next Tuesday at 09:00 local time (#338)."""

SUGGESTION: Final = "Most effective: Tue–Thu mornings."  # noqa: RUF001 -- as the UI says it
"""What the activate dialog and the CLI say about the suggested slots."""

SERVE_REMINDER: Final = (
    "netkeeper sends only while `serve` is running and this Mac is awake."
    " Keep it running from the start time until the batch finishes."
)
"""The reminder the activate dialog and the CLI show (#338, item 5)."""

SEARCH_DAYS: Final = 400
"""How far :meth:`Suggested.next_slot` looks: over a year, so a long run of holidays
still ends, and a list of holidays that covers every day answers None."""


class ScheduleError(ValueError):
    """A time zone, holiday, or time of day that cannot be read. Nothing is sent under it."""


@dataclass(frozen=True, slots=True)
class Suggested:
    """The suggested send slots, and the user's local day, for one user."""

    days: frozenset[int]
    start: time
    end: time
    holidays: frozenset[date]
    zone: ZoneInfo

    def local_date(self, at: datetime) -> date:
        """The local calendar day ``at`` falls on."""
        return _aware(at).astimezone(self.zone).date()

    def local_time(self, at: datetime) -> time:
        """The local time of day of ``at``."""
        return _aware(at).astimezone(self.zone).time().replace(tzinfo=None)

    def day_bounds(self, at: datetime) -> tuple[datetime, datetime]:
        """The local day ``at`` falls on, as ``[start, end)`` in UTC."""
        day = self.local_date(at)
        return self.at_local(day, time()), self.at_local(day + timedelta(days=1), time())

    def at_local(self, day: date, clock: time) -> datetime:
        """``clock`` on the local ``day``, in UTC."""
        return datetime.combine(day, clock, tzinfo=self.zone).astimezone(UTC)

    def is_holiday(self, at: datetime) -> bool:
        return self.local_date(at) in self.holidays

    def is_suggested(self, at: datetime) -> bool:
        """Whether ``at`` falls inside a suggested slot (a suggested day, not a holiday)."""
        return self.next_slot(at) == _aware(at).astimezone(UTC)

    def next_slot(self, at: datetime) -> datetime | None:
        """The earliest instant at or after ``at`` inside a suggested slot, in UTC.

        None when holidays cover every suggested day for :data:`SEARCH_DAYS`.
        """
        now = _aware(at).astimezone(UTC)
        today = self.local_date(now)
        for offset in range(SEARCH_DAYS):
            day = today + timedelta(days=offset)
            if day.weekday() not in self.days or day in self.holidays:
                continue
            opens, closes = self.at_local(day, self.start), self.at_local(day, self.end)
            if now < opens:
                return opens
            if now < closes:
                return now
        return None


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("a time for the schedule must be timezone-aware")
    return at


def _days(names: Iterable[str]) -> frozenset[int]:
    return frozenset(DAY_NAMES.index(name) for name in names)


def parse_clock(text: object) -> time:
    """``HH:MM`` as a time of day. Raises :class:`ScheduleError` for anything else."""
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        raise ScheduleError(f"{text!r} is not a time of day as HH:MM")
    if not (text[:2].isdigit() and text[3:].isdigit()):
        raise ScheduleError(f"{text!r} is not a time of day as HH:MM")
    try:
        return time(int(text[:2]), int(text[3:]))
    except ValueError as exc:
        raise ScheduleError(f"{text!r} is not a time of day as HH:MM") from exc


def parse_holidays(values: Iterable[str]) -> frozenset[date]:
    days: set[date] = set()
    for value in values:
        try:
            days.add(date.fromisoformat(value))
        except (TypeError, ValueError) as exc:
            raise ScheduleError(f"holiday {value!r} is not a date as YYYY-MM-DD") from exc
    return frozenset(days)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"{name!r} is not a time zone") from exc


def suggested(settings: CampaignSettings, timezone: str) -> Suggested:
    """The suggested slots in ``timezone``, skipping ``[campaigns] holidays``.

    Raises :class:`ScheduleError` for a time zone or holiday it cannot read.
    """
    return Suggested(
        days=_days(SUGGESTED_DAYS),
        start=parse_clock(SUGGESTED_HOURS[0]),
        end=parse_clock(SUGGESTED_HOURS[1]),
        holidays=parse_holidays(settings.holidays),
        zone=zone(timezone),
    )


def default_start(now: datetime, slots: Suggested) -> datetime:
    """The next Tuesday at 09:00 local time, in UTC: today when it is Tuesday before 09:00.

    A Tuesday that is a holiday moves to the next suggested slot after it.
    """
    today = slots.local_date(now)
    clock = parse_clock(DEFAULT_START_TIME)
    ahead = (DAY_NAMES.index(DEFAULT_START_DAY) - today.weekday()) % 7
    candidate = slots.at_local(today + timedelta(days=ahead), clock)
    if candidate <= _aware(now).astimezone(UTC):
        candidate = slots.at_local(today + timedelta(days=ahead + 7), clock)
    if slots.is_holiday(candidate):
        return slots.next_slot(candidate) or candidate
    return candidate


def step_due(
    base: datetime,
    *,
    delay_days: int,
    send_time: str | None,
    slots: Suggested,
    first: bool = False,
) -> datetime:
    """When a step is due, ``delay_days`` after ``base``, in UTC.

    ``base`` is the scheduled start for step 1 (``first``) and the latest sent
    message for a follow-up.

    - An explicit ``send_time`` (``HH:MM``) is honored at any hour, on any day: the
      step is due that time on the local day ``delay_days`` after ``base``'s, and
      never before ``base``.
    - Without one, step 1 with no delay is due at ``base`` itself (the start the
      person chose). Any other step aims for the next suggested slot at or after
      ``base`` plus its delay.
    """
    if send_time is not None:
        day = slots.local_date(base) + timedelta(days=delay_days)
        return max(slots.at_local(day, parse_clock(send_time)), _aware(base).astimezone(UTC))
    due = _aware(base).astimezone(UTC) + timedelta(days=delay_days)
    if first and delay_days == 0:
        return due
    return slots.next_slot(due) or due


def spill(due: datetime, now: datetime, slots: Suggested) -> datetime | None:
    """Where a due time left over from an earlier local day goes (#338).

    A batch that runs long (its day's cap was reached, or ``serve`` was not
    running) does not run on overnight: what is left of it goes on the next day
    at the same local time it was due. ``None`` when ``due`` is on today's local
    day, or when today's time has already come: it is due now.
    """
    if slots.local_date(due) >= slots.local_date(now):
        return None
    resume = slots.at_local(slots.local_date(now), slots.local_time(due))
    return resume if resume > _aware(now).astimezone(UTC) else None


def in_suggested_hours(clock: time, slots: Suggested) -> bool:
    """Whether a local time of day is inside the suggested hours, ``[09:00, 16:30)``."""
    return slots.start <= clock < slots.end


def unchosen_allowed(at: datetime, slots: Suggested) -> bool:
    """Whether a send whose time nobody chose may go out at ``at`` (#338 review, B1).

    The one place this rule lives. Today it is the hours rule: inside 09:00 to 16:30
    local time. A day rule (no weekends, say) would join it here, and every caller
    (the overdue check, a retry, a guard's re-check) would follow.
    """
    return in_suggested_hours(slots.local_time(at), slots)


def next_time_of_day(clock: time, at: datetime, slots: Suggested) -> datetime:
    """The first instant at or after ``at`` whose local time of day is ``clock``, in UTC."""
    now = _aware(at).astimezone(UTC)
    day = slots.local_date(now)
    candidate = slots.at_local(day, clock)
    return candidate if candidate >= now else slots.at_local(day + timedelta(days=1), clock)


def release(due: datetime, now: datetime, slots: Suggested) -> datetime | None:
    """When a step due at ``due`` may really go, or None for now (#338).

    Two rules, in order:

    1. **The spill** (:func:`spill`): a due time from an earlier local day moves to
       today at its own time of day, when that is still to come.
    2. **Off hours** (#338 review, B1): a due time whose time of day is inside the
       suggested hours was never explicitly set outside them, so it never goes out
       while :func:`unchosen_allowed` says no. It waits for the next occurrence of
       its own time of day. A Tuesday 09:00 step that ``serve`` wakes for at 22:00
       goes on Wednesday at 09:00.

    A due time outside the suggested hours was chosen (an explicit start or step
    time), and keeps firing at the chosen time.
    """
    spilled = spill(due, now, slots)
    if spilled is not None:
        return spilled
    clock = slots.local_time(due)
    if in_suggested_hours(clock, slots) and not unchosen_allowed(now, slots):
        return next_time_of_day(clock, now, slots)
    return None


def aim_unchosen(anchor: datetime, due: datetime, slots: Suggested) -> datetime:
    """A due time nobody chose (a retry, a guard's re-check), aimed at ``anchor``'s hours.

    ``anchor`` is when the step was last claimed or considered. When that was inside
    the suggested hours and ``due`` is not allowed (:func:`unchosen_allowed`) or falls
    on a later local day, ``due`` moves to the next occurrence of ``anchor``'s time of
    day at or after it. Otherwise it is kept: an anchor outside the hours was an
    explicit time, and keeps its batch going.
    """
    clock = slots.local_time(anchor)
    if not in_suggested_hours(clock, slots):
        return due
    if unchosen_allowed(due, slots) and slots.local_date(due) == slots.local_date(anchor):
        return due
    return next_time_of_day(clock, due, slots)


def spacing_delay(rng: random.Random, *, median_s: float, floor_s: float) -> timedelta:
    """The gap after one send before the next may go (spec 11.4): ``human_delay``, floored.

    Spec 11.4: a median of 4 minutes (``send_spacing_median_s``) and a floor of 90
    seconds (``send_spacing_floor_s``). A median or floor that is not a positive
    number is refused rather than read as "no spacing": sends are never a burst.
    """
    if not (median_s > 0 and math.isfinite(median_s)):
        raise ValueError("the send spacing median must be a positive number of seconds")
    if not (floor_s > 0 and math.isfinite(floor_s)):
        raise ValueError("the send spacing floor must be a positive number of seconds")
    return timedelta(seconds=max(floor_s, human_delay(rng, median=median_s)))
