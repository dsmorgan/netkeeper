"""Sending hours: when campaign email may go out, a global per-user setting (#338).

Stored in ``settings_kv`` under :data:`KEY`, not in ``config.toml``, so the Settings
page can change it without writing a file. Nothing stored means
:data:`netkeeper.campaigns.schedule.DEFAULT_SENDING_HOURS`: Monday to Friday, 09:00 to
17:00 local time. ``GET``/``PUT /settings/sending-hours`` and ``netkeeper campaigns
sending-hours`` read and write it; the campaign engine reads it every tick.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from sqlalchemy.orm import Session

from netkeeper.campaigns import schedule
from netkeeper.models import User
from netkeeper.services.settings_kv import get_setting, set_setting

KEY: Final = "campaigns.sending_hours"
"""The ``settings_kv`` key."""


def read(session: Session, user: User) -> schedule.SendingHours:
    """The user's sending hours, or the default. Raises
    :class:`~netkeeper.campaigns.schedule.ScheduleError` for a stored value that cannot be
    read: the engine then sends nothing rather than guessing."""
    return schedule.sending_hours_from_json(get_setting(session, user, KEY))


def write(
    session: Session,
    user: User,
    *,
    enabled: bool,
    days: Iterable[str],
    start: str,
    end: str,
) -> schedule.SendingHours:
    """Validate and store new sending hours. Raises
    :class:`~netkeeper.campaigns.schedule.ScheduleError` for a value it refuses."""
    hours = schedule.sending_hours(enabled=enabled, days=days, start=start, end=end)
    set_setting(session, user, KEY, schedule.sending_hours_json(hours))
    return hours
