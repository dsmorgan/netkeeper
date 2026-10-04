"""The user's local day, in one place.

Depends on nothing else in netkeeper, so :mod:`netkeeper.crm` and
:mod:`netkeeper.campaigns` can both use it without importing each other.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def find_zone(name: str) -> ZoneInfo | None:
    """The time zone called ``name``, or ``None`` when it cannot be read."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_today(timezone: str, now: datetime) -> date:
    """``now`` as a calendar day in ``timezone``: the user's "today".

    The one helper for a day that is not about to send (a preview, a review render,
    an export), so it agrees with the engine's ``Suggested.local_date``. A zone it
    cannot read falls back to UTC rather than raising: those paths should still
    answer, and nothing is sent from them. ``now`` must be timezone-aware.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("a time for the local day must be timezone-aware")
    tz: tzinfo = find_zone(timezone) or UTC
    return now.astimezone(tz).date()
