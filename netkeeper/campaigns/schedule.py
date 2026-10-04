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
from datetime import UTC, date, datetime, time, timedelta, tzinfo
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


def local_today(timezone: str, now: datetime) -> date:
    """``now`` as a calendar day in ``timezone``: the user's "today".

    The one helper for a day that is not about to send (a preview, a review render,
    an export), so it agrees with the engine's ``Suggested.local_date``. A zone it
    cannot read falls back to UTC rather than raising: those paths should still
    answer, and nothing is sent from them. ``now`` must be timezone-aware.
    """
    try:
        tz: tzinfo = zone(timezone)
    except ScheduleError:
        tz = UTC
    return _aware(now).astimezone(tz).date()


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

    - An explicit ``send_time`` (``HH:MM``): the step is due that time on the local
      day ``delay_days`` after ``base``'s, and never before ``base``. The sending
      hours apply on top (:func:`next_opening`, by the caller).
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
    running) goes on the next day at the same local time it was due, when that
    time is still ahead. ``None`` when ``due`` is on today's local day, or when
    today's time has already come: it is due now. The sending hours apply on top
    (:func:`hold`).
    """
    if slots.local_date(due) >= slots.local_date(now):
        return None
    resume = slots.at_local(slots.local_date(now), slots.local_time(due))
    return resume if resume > _aware(now).astimezone(UTC) else None


# --- sending hours (#338, David's requirement on PR #353) -----------------------------


@dataclass(frozen=True, slots=True)
class SendingHours:
    """When campaign email may go out, in the user's local time: a global setting.

    ``enabled`` False is "any time". Otherwise a send goes only on ``days`` (weekday
    numbers, Monday 0) in ``[start, end)``. Only a campaign's start is exempt: its
    first batch goes at the scheduled start, whatever the hour, and keeps going that
    local day until the caps stop it (:func:`hold`).
    """

    enabled: bool
    days: frozenset[int]
    start: time
    end: time

    def describe(self) -> str:
        """For example "Mon to Fri, 09:00 to 17:00", or "any time"."""
        if not self.enabled:
            return "any time"
        return f"{describe_days(self.days)}, {self.start:%H:%M} to {self.end:%H:%M}"


FULL_DAY_NAMES: Final = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

DEFAULT_SENDING_HOURS: Final = SendingHours(
    enabled=True, days=frozenset({0, 1, 2, 3, 4}), start=time(9, 0), end=time(17, 0)
)
"""Monday to Friday, 09:00 to 17:00 local time, on (#338)."""


def describe_days(days: frozenset[int]) -> str:
    """Weekday numbers as "Mon to Fri", "Tue, Thu", or "every day"."""
    ordered = sorted(days)
    if ordered == list(range(7)):
        return "every day"
    if len(ordered) > 2 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{DAY_NAMES[ordered[0]]} to {DAY_NAMES[ordered[-1]]}"
    return ", ".join(DAY_NAMES[d] for d in ordered)


def sending_hours(*, enabled: bool, days: Iterable[str], start: str, end: str) -> SendingHours:
    """Validated sending hours. Raises :class:`ScheduleError`: an unknown day, no day at
    all, a time that is not ``HH:MM``, or an end that is not after the start."""
    numbers: set[int] = set()
    for name in days:
        key = str(name).strip().lower()
        short = [d.lower() for d in DAY_NAMES]
        if key in short:
            numbers.add(short.index(key))
        elif key in FULL_DAY_NAMES:
            numbers.add(FULL_DAY_NAMES.index(key))
        else:
            raise ScheduleError(f"{name!r} is not a day; use one of {', '.join(DAY_NAMES)}")
    if not numbers:
        raise ScheduleError("sending hours need at least one day")
    opens, closes = parse_clock(start), parse_clock(end)
    if closes <= opens:
        raise ScheduleError(f"sending hours must end after they start, not {start} to {end}")
    return SendingHours(enabled=enabled, days=frozenset(numbers), start=opens, end=closes)


def sending_hours_from_json(raw: object) -> SendingHours:
    """The stored value (``settings_kv``), or the default when nothing is stored.
    Raises :class:`ScheduleError` for a stored value that cannot be read."""
    if raw is None:
        return DEFAULT_SENDING_HOURS
    if not isinstance(raw, dict):
        raise ScheduleError("stored sending hours are not an object")
    enabled, days, start, end = (raw.get(k) for k in ("enabled", "days", "start", "end"))
    if not isinstance(enabled, bool) or not isinstance(days, list):
        raise ScheduleError("stored sending hours are malformed")
    return sending_hours(
        enabled=enabled, days=[str(d) for d in days], start=str(start), end=str(end)
    )


def sending_hours_json(hours: SendingHours) -> dict[str, object]:
    """How ``settings_kv`` stores sending hours."""
    return {
        "enabled": hours.enabled,
        "days": [DAY_NAMES[d] for d in sorted(hours.days)],
        "start": f"{hours.start:%H:%M}",
        "end": f"{hours.end:%H:%M}",
    }


def within_sending_hours(at: datetime, hours: SendingHours, slots: Suggested) -> bool:
    """Whether a send may go out at ``at`` under ``hours``. The one rule; "any time"
    allows everything."""
    if not hours.enabled:
        return True
    local = _aware(at).astimezone(slots.zone)
    return (
        local.weekday() in hours.days
        and hours.start <= local.time().replace(tzinfo=None) < hours.end
    )


def next_opening(at: datetime, hours: SendingHours, slots: Suggested) -> datetime:
    """The earliest instant at or after ``at`` inside ``hours``, in UTC (``at`` itself
    when it already is, or with "any time")."""
    now = _aware(at).astimezone(UTC)
    if within_sending_hours(now, hours, slots):
        return now
    today = slots.local_date(now)
    for offset in range(8):
        day = today + timedelta(days=offset)
        if day.weekday() not in hours.days:
            continue
        # Conservative at a daylight-saving fall-back (#338 review N4): ``at_local``
        # takes the first of a repeated local time. With hours inside the repeated hour
        # (Sun 01:30 to 02:00, say) and ``at`` at the second 01:10, today's opening is
        # the first 01:30, already past, so this answers next week although the second
        # 01:30 is open. It only ever waits longer, never sends outside the hours.
        opens = slots.at_local(day, hours.start)
        if opens >= now:
            return opens
    raise ScheduleError("sending hours never open")  # unreachable: validated non-empty days


def hold(
    due: datetime,
    now: datetime,
    *,
    slots: Suggested,
    hours: SendingHours,
    starts_at: datetime | None,
    first_step: bool,
) -> datetime | None:
    """When a step due at ``due`` may really go, or None for now (#338). The one function
    the tick, a retry, a re-check and posture ask.

    1. **The start is unbounded.** Step 1 on the local day of the campaign's start
       goes now, whatever the hour or day: the first batch runs at the scheduled
       start until the caps stop it. ``starts_at`` is None for a start nobody chose
       (one 0031 backfilled), which has no exemption.
    2. **Sending hours on:** anything else goes now when now is inside them, and
       otherwise waits for their next opening. A leftover is not moved to its old
       time of day first: that would skip a day after a night start, or squeeze a
       16:00 start's next days into one hour (#338 review S1).
    3. **"Any time":** the spill only. A due time from an earlier local day moves to
       today at its own time of day when that is still ahead (:func:`spill`).
    """
    if (
        first_step
        and starts_at is not None
        and slots.local_date(now) == slots.local_date(starts_at)
    ):
        return None
    if not hours.enabled:
        return spill(due, now, slots)
    if not within_sending_hours(now, hours, slots):
        return next_opening(now, hours, slots)
    return None


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
