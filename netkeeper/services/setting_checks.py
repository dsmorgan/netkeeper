"""Checks a setting's value earns, shared by the posture report and the Settings page.

Pure: values in, words out. :mod:`netkeeper.services.posture` reads these for its rows,
and :mod:`netkeeper.services.ui_settings` (#343) for the warnings beside each value, so
a window, a weekend multiplier or auto-send reads the same in either place.
"""

from __future__ import annotations

from datetime import time
from typing import Final, Literal

from netkeeper.config import CampaignSettings

#: An active window longer than this has stopped being a window. Appendix B's
#: default is 08:30 to 21:30, which is 13 hours; 16 leaves room for someone who
#: genuinely keeps long hours while still catching a window dialed open to 22.
MAX_ACTIVE_WINDOW_HOURS: Final = 16.0

#: Spec 9.5 damps weekend budgets by multiplying them. At 1.0 the damping does
#: nothing; above it, the "damping" raises weekend budgets above weekday ones.
WEEKEND_DAMPING_CEILING: Final = 1.0

WindowStatus = Literal["on", "off"]


def active_window_check(start: time, end: time) -> tuple[WindowStatus, tuple[str, ...]]:
    """Whether an active window is still a window, and the warnings it earns (spec 9.5).

    :data:`"off"` for a window that starts and ends at the same time (all 24
    hours) or runs longer than :data:`MAX_ACTIVE_WINDOW_HOURS`; an overnight window
    stays on, with a warning. The posture report and the Settings page (#343) both
    read it, so a window reads the same in either place.
    """
    span_hours = window_hours(start, end)
    if start == end:
        return "off", (
            "the window starts and ends at the same time, which means active all 24"
            " hours: no tick is ever parked for a window start",
        )
    if span_hours > MAX_ACTIVE_WINDOW_HOURS:
        return "off", (
            f"the window is {span_hours:.1f} hours long, past the {MAX_ACTIVE_WINDOW_HOURS:.0f}"
            " this report treats as still being a window. Appendix B's default is 08:30"
            " to 21:30, 13 hours",
        )
    if start > end:
        return "on", (
            f"the window runs overnight ({start:%H:%M} to {end:%H:%M}), so netkeeper is"
            " active at hours your own browsing is not. Spec 9.1 leans on your organic"
            " activity as cover traffic, and a sidecar that is busiest while the account"
            " is otherwise asleep has none. If those really are your hours, this is fine",
        )
    return "on", ()


def weekend_multiplier_warnings(multiplier: float) -> tuple[str, ...]:
    """The warnings a weekend multiplier earns (spec 9.5): none for one that damps.

    Shared by the posture report's weekend-damping row and the Settings page (#343).
    """
    if multiplier < 0:
        return (
            f"linkedin.weekend_multiplier is {multiplier:g}, which is not a multiplier"
            " any budget can be scaled by",
        )
    if multiplier >= WEEKEND_DAMPING_CEILING:
        raised = (
            " and raises them above a weekday's" if multiplier > WEEKEND_DAMPING_CEILING else ""
        )
        return (
            f"linkedin.weekend_multiplier is {multiplier:g}, so weekend budgets are not"
            f" damped at all{raised}. Appendix B's default is 0.5",
        )
    return ()


def auto_send_warnings(settings: CampaignSettings) -> tuple[str, ...]:
    """The warning ``linkedin_auto_send`` earns when it is on (ADR 0004), or none."""
    if not settings.linkedin_auto_send:
        return ()
    return (
        "campaigns.linkedin_auto_send is true, so netkeeper sends LinkedIn messages"
        " itself rather than prefilling them for you to send. ADR 0004 defaults it"
        " off: an automated send is the action LinkedIn restricts hardest",
    )


def window_hours(start: time, end: time) -> float:
    """The window's length in hours, wrapping past midnight when it has to."""
    minutes = ((end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)) % (24 * 60)
    return 24.0 if start == end else minutes / 60
