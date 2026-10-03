"""``/settings/sending-hours``: when campaign email may go out (#338).

A global, per-user setting in ``settings_kv`` (:mod:`netkeeper.services.sending_hours`),
editable on the Settings page. Only a campaign's start ignores it: the first batch
goes at the scheduled start, whatever the hour, and keeps going that local day until
the caps stop it. Everything after obeys it.

- ``GET /settings/sending-hours`` answers the current value (the default when none is
  stored: Monday to Friday, 09:00 to 17:00).
- ``PUT /settings/sending-hours`` replaces it. ``422`` for no day, an unknown day, a time
  that is not ``HH:MM``, or an end that is not after the start.

``netkeeper campaigns sending-hours`` mirrors both.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from netkeeper.campaigns import schedule
from netkeeper.services import sending_hours as service
from netkeeper.web.deps import CurrentUser, SessionDep

router = APIRouter(tags=["settings"])

Day = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
Clock = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


class SendingHoursIn(BaseModel):
    enabled: bool
    """False is "any time": sends go whenever they are due."""
    days: Annotated[list[Day], Field(min_length=1, max_length=7)]
    start: Clock
    """Local time of day, ``HH:MM``, in your time zone."""
    end: Clock
    """Local time of day, ``HH:MM``; after ``start``."""


class SendingHoursOut(SendingHoursIn):
    timezone: str
    """Your time zone (``linkedin.timezone``), which the hours are read in."""
    summary: str
    """The hours as a sentence fragment: "Mon to Fri, 09:00 to 17:00", or "any time"."""


def _out(hours: schedule.SendingHours, timezone: str) -> SendingHoursOut:
    days = [cast(Day, schedule.DAY_NAMES[d]) for d in sorted(hours.days)]
    return SendingHoursOut(
        enabled=hours.enabled,
        days=days,
        start=f"{hours.start:%H:%M}",
        end=f"{hours.end:%H:%M}",
        timezone=timezone,
        summary=hours.describe(),
    )


@router.get(
    "/settings/sending-hours",
    operation_id="get_sending_hours",
    responses={422: {"description": "The stored value cannot be read"}},
)
def get_sending_hours(session: SessionDep, user: CurrentUser) -> SendingHoursOut:
    """The sending hours: when campaign email may go out, after each campaign's start."""
    try:
        return _out(service.read(session, user), user.timezone)
    except schedule.ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put(
    "/settings/sending-hours",
    operation_id="set_sending_hours",
    responses={422: {"description": "Hours that cannot be used"}},
)
def set_sending_hours(
    body: SendingHoursIn, session: SessionDep, user: CurrentUser
) -> SendingHoursOut:
    """Replace the sending hours. Applies from the next tick to every campaign."""
    try:
        hours = service.write(
            session, user, enabled=body.enabled, days=body.days, start=body.start, end=body.end
        )
    except schedule.ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _out(hours, user.timezone)
