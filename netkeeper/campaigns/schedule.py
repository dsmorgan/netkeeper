"""When a campaign may send: the send window, holidays, the local day, and spacing (spec 11.4).

Pure: no session, no clock. :mod:`netkeeper.services.campaign_engine` asks
these questions on every tick.

**The window.** A set of weekdays and a daily span of local time, ``[start,
end)``: the default is Tuesday to Thursday, 09:00 to 16:30 (``[campaigns]
send_window_days`` and ``send_window_hours``). A campaign may override either
half in ``campaigns.send_window_json``, as ``{"days": [...], "hours": [start,
end]}``, the config's own spelling. ``[campaigns] holidays`` (``YYYY-MM-DD``)
are local dates with no window at all, for every campaign.

**Local time** is the user's ``timezone`` (``linkedin.timezone`` keeps it
current, :func:`netkeeper.services.users.ensure_local_user`): the same zone the
LinkedIn budgets count their days in, so "today" means one thing everywhere.

**Safety.** A window that cannot be read (an unknown day name, an hour that is
not ``HH:MM``, an empty span, a holiday that is not a date, an unknown time
zone, a key this module does not know) raises :class:`WindowError`, and the
engine sends nothing for that campaign. A window that never opens (no days, or
every day a holiday) answers ``None`` from :meth:`SendWindow.next_open`, and
the engine sends nothing either. Neither case guesses a window.

Comparisons are made in UTC: two aware datetimes that share a ``ZoneInfo``
compare by wall time, which is wrong across a daylight-saving fold.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from netkeeper.config import CampaignSettings
from netkeeper.linkedin.pacing import human_delay

DAY_NAMES: Final[tuple[str, ...]] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
"""Weekday names as the config spells them, Monday first (``date.weekday()`` order)."""

WINDOW_KEYS: Final[frozenset[str]] = frozenset({"days", "hours"})
"""The keys a campaign's ``send_window_json`` may hold."""

SEARCH_DAYS: Final = 400
"""How far :meth:`SendWindow.next_open` looks: over a year, so a window that opens once
a week still opens after a long run of holidays, and one that never opens ends."""


class WindowError(ValueError):
    """A send window, holiday, or time zone that cannot be read. Nothing is sent under it."""


@dataclass(frozen=True, slots=True)
class SendWindow:
    """When one campaign may send, in the user's local time."""

    days: frozenset[int]
    start: time
    end: time
    holidays: frozenset[date]
    zone: ZoneInfo

    def local_date(self, at: datetime) -> date:
        """The local calendar day ``at`` falls on."""
        return _aware(at).astimezone(self.zone).date()

    def day_bounds(self, at: datetime) -> tuple[datetime, datetime]:
        """The local day ``at`` falls on, as ``[start, end)`` in UTC."""
        day = self.local_date(at)
        return self._utc(day, time()), self._utc(day + timedelta(days=1), time())

    def is_open(self, at: datetime) -> bool:
        return self.next_open(at) == _aware(at).astimezone(UTC)

    def next_open(self, at: datetime) -> datetime | None:
        """The earliest instant at or after ``at`` inside the window, in UTC.

        None when the window never opens.
        """
        now = _aware(at).astimezone(UTC)
        today = self.local_date(now)
        for offset in range(SEARCH_DAYS):
            day = today + timedelta(days=offset)
            if day.weekday() not in self.days or day in self.holidays:
                continue
            opens, closes = self._utc(day, self.start), self._utc(day, self.end)
            if now < opens:
                return opens
            if now < closes:
                return now
        return None

    def _utc(self, day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, tzinfo=self.zone).astimezone(UTC)


def _aware(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("a time for the send window must be timezone-aware")
    return at


def _days(names: object) -> frozenset[int]:
    if not isinstance(names, Sequence) or isinstance(names, str):
        raise WindowError(f"send window days must be a list of day names, not {names!r}")
    days: set[int] = set()
    for name in names:
        if name not in DAY_NAMES:
            raise WindowError(f"{name!r} is not a day; use one of {', '.join(DAY_NAMES)}")
        days.add(DAY_NAMES.index(name))
    return frozenset(days)


def _clock(text: object) -> time:
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        raise WindowError(f"{text!r} is not a time of day as HH:MM")
    try:
        return time(int(text[:2]), int(text[3:]))
    except ValueError as exc:
        raise WindowError(f"{text!r} is not a time of day as HH:MM") from exc


def _hours(span: object) -> tuple[time, time]:
    if not isinstance(span, Sequence) or isinstance(span, str) or len(span) != 2:
        raise WindowError(f"send window hours must be [start, end], not {span!r}")
    start, end = _clock(span[0]), _clock(span[1])
    if start >= end:
        raise WindowError(f"the send window {span[0]} to {span[1]} is empty")
    return start, end


def parse_holidays(values: Iterable[str]) -> frozenset[date]:
    days: set[date] = set()
    for value in values:
        try:
            days.add(date.fromisoformat(value))
        except (TypeError, ValueError) as exc:
            raise WindowError(f"holiday {value!r} is not a date as YYYY-MM-DD") from exc
    return frozenset(days)


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WindowError(f"{name!r} is not a time zone") from exc


def send_window(
    settings: CampaignSettings, timezone: str, override: Mapping[str, object] | None = None
) -> SendWindow:
    """The window a campaign sends in: the config's, with the campaign's ``override`` on top.

    Raises :class:`WindowError` for anything it cannot read, the config's own
    values included.
    """
    days: object = settings.send_window_days
    hours: object = settings.send_window_hours
    if override is not None:
        if not isinstance(override, Mapping):
            raise WindowError("a campaign's send window must be an object")
        unknown = set(override) - WINDOW_KEYS
        if unknown:
            raise WindowError(f"unknown send window keys: {', '.join(sorted(map(str, unknown)))}")
        days = override.get("days", days)
        hours = override.get("hours", hours)
    start, end = _hours(hours)
    return SendWindow(
        days=_days(days),
        start=start,
        end=end,
        holidays=parse_holidays(settings.holidays),
        zone=zone(timezone),
    )


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
