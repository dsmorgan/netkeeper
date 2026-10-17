"""Moving a user to another time zone without opening extra budget (#597).

Every daily limit counts a local day: the LinkedIn budgets by counter keys named for
the local date (:mod:`netkeeper.services.budgets`), the campaign and mailbox email caps
by the sends inside the local day's bounds (``campaign_engine.mailbox_count``). At one
instant two or three local dates exist somewhere, so a zone change alone would hand out
a fresh day. :func:`change_zone` therefore, in one writer transaction:

1. carries each LinkedIn counter for the old zone's current day and week into the new
   zone's, keeping the larger (``budgets.carry_counters``);
2. keeps the email caps counting from the old zone's day start until the new zone's day
   ends (:func:`count_start`), so sends made earlier today still count;
3. logs the change, and records when it happened. The Settings page refuses a second
   change within :data:`MIN_INTERVAL` (``ui_settings.write``); a start that follows an
   edited config.toml is never refused, but is carried and logged the same way.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from netkeeper.models import User
from netkeeper.services import budgets
from netkeeper.services.linkedin_accounts import find_account
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

MIN_INTERVAL: Final = timedelta(hours=1)
"""The Settings page changes a user's time zone at most once in this long."""

CHANGED_AT_KEY: Final = "timezone.changed_at"
"""``settings_kv``: when the zone last changed, as an ISO instant."""

COUNT_FLOOR_KEY: Final = "timezone.count_floor"
"""``settings_kv``: ``{"start": ISO, "until": ISO}``. Until ``until``, the email caps
count sends from ``start`` instead of from the local day's start."""


def change_zone(session: Session, user: User, zone: str, *, now: datetime) -> bool:
    """Set ``user.timezone`` to ``zone``, carrying today's counts across. True when it
    changed. Needs a writer session."""
    if user.timezone == zone:
        return False
    old_zone, new_zone = _zone(user.timezone), _zone(zone)
    account = find_account(session, user)
    if account is not None:
        budgets.carry_counters(
            session, user, account.id, old_zone=old_zone, new_zone=new_zone, now=now
        )
    _extend_count_floor(session, user, old_zone, new_zone, now)
    log.warning(
        "user %d: time zone %s -> %s; today's budget counts carried to the new zone's day",
        user.id,
        user.timezone,
        zone,
    )
    user.timezone = zone
    set_setting(session, user, CHANGED_AT_KEY, now.astimezone(UTC).isoformat())
    session.flush()
    return True


def last_changed(session: Session, user: User) -> datetime | None:
    """When ``user``'s zone last changed, or None. Read-only."""
    raw = get_setting(session, user, CHANGED_AT_KEY)
    return _instant(raw)


def count_start(session: Session, user: User, day_start: datetime, now: datetime) -> datetime:
    """Where today's email caps start counting: ``day_start``, or earlier for the rest
    of a day the zone changed on. Read-only."""
    raw = get_setting(session, user, COUNT_FLOOR_KEY)
    if not isinstance(raw, dict):
        return day_start
    start, until = _instant(raw.get("start")), _instant(raw.get("until"))
    if start is None or until is None or now >= until:
        return day_start
    return min(day_start, start)


def _extend_count_floor(
    session: Session, user: User, old_zone: tzinfo, new_zone: tzinfo, now: datetime
) -> None:
    start = _day_start(now, old_zone)
    until = _day_start(now, new_zone) + timedelta(days=1)
    raw = get_setting(session, user, COUNT_FLOOR_KEY)
    if isinstance(raw, dict):
        kept_start, kept_until = _instant(raw.get("start")), _instant(raw.get("until"))
        if kept_start is not None and kept_until is not None and now < kept_until:
            start = min(start, kept_start)
            until = max(until, kept_until)
    set_setting(
        session,
        user,
        COUNT_FLOOR_KEY,
        {"start": start.isoformat(), "until": until.isoformat()},
    )


def _day_start(now: datetime, zone: tzinfo) -> datetime:
    local = now.astimezone(zone)
    return datetime.combine(local.date(), time(), tzinfo=zone).astimezone(UTC)


def _instant(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC
