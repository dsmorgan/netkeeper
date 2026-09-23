"""Per-day and per-week action budgets for the LinkedIn extractor (spec 9.6, 9.10).

**The boundary seam.** Spec 9.6 says the counters "live in ``settings_kv``,
keyed by local day and week, per action class" -- persistence is the point of
this module, and persistence needs a session and a ``User`` (for their
timezone and their ``settings_kv`` rows). Nothing under ``netkeeper/linkedin/``
may import ``netkeeper.models`` or open a session (ADR 0005, spec 9.10), so
this module lives here, on the core side, alongside ``netkeeper.services.heat``
rather than as a sibling of ``netkeeper.linkedin.heat``. Unlike heat, spec 9.6
names no pure module for budget math, and there is barely any: the local-day
and local-week key derivation (:class:`LocalPeriod`) needs ``User.timezone``
already, so splitting it out to stay "pure" would not remove the boundary
crossing, only add an extra hop. The one genuinely pure piece -- comparing a
count to a limit -- is a single inequality, not worth a module of its own.

**The account key.** Spec 9.6 keys budgets "by LinkedIn account", and spec
8.4 has that as a future ``linkedin_account`` row -- but that model belongs to
P2-06, which *depends on* this item, so it cannot exist yet. Every function
here therefore takes ``account_id: int`` as a plain, caller-supplied
identifier rather than a model or a relationship. P2-06 passes
``linkedin_account.id``; nothing here changes when that row is added.

**What is not here.** Warm-up ramp and the weekend/holiday multiplier (spec
9.5) belong to ``linkedin.pacing`` (P2-04): they adjust what limit a caller
*asks* this module to enforce, not how enforcement itself works. Likewise
``li_messages_auto``'s "only when auto-send is enabled" gate (spec 9.6) is the
caller's decision -- this module enforces whatever action class it is asked
to check, unconditionally. ``contact_info_fetches`` has no counter of its own:
spec 9.6 calls it "tied to ``profile_visits``, one per visit" because
enrichment's step 3 (spec 9.4) fetches contact info as part of the same
profile-visit unit of work, so counting it separately would double-enforce
one real limit under two names.

**Enforcement is between units of work, never mid-unit** (spec 9.4, 9.9): call
:func:`consume` once, before a unit starts, and let the unit run to
completion regardless of what happens inside it. Because the check and the
increment happen together in one call, and no other call can interleave
between them (SQLite's writer lock, spec 15, serializes writer sessions; the
extractor also holds one activity lock per account, spec 9.9), the count can
run at most one unit over its limit, never two: refusal only fires once the
count already *exceeds* the limit (see :func:`consume`'s docstring), so a
unit that starts exactly at the limit is still let through -- it is that
unit's own increment which pushes the count one over, and the very next call
sees that and refuses.
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from netkeeper.config import BudgetSettings
from netkeeper.db import is_writer
from netkeeper.models import User
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)


class ActionClass(enum.StrEnum):
    """The action classes spec 9.6's table budgets. See the module docstring for
    why ``contact_info_fetches`` has no entry of its own."""

    CONNECTION_PAGES = "connection_pages"
    PROFILE_VISITS = "profile_visits"
    INBOX_POLLS = "inbox_polls"
    LI_MESSAGES_AUTO = "li_messages_auto"


Period = Literal["day", "week"]

# Hard ceilings from spec 9.6's table. Not configurable, unlike the "default
# per day" values in `BudgetSettings`: they are the absolute stop regardless
# of how a user dials `config.toml` down or up, so `_limits_for` clamps the
# configured default to this before it is ever enforced.
HARD_MAX_PER_DAY: Final[dict[ActionClass, int]] = {
    ActionClass.CONNECTION_PAGES: 400,
    ActionClass.PROFILE_VISITS: 100,
    ActionClass.INBOX_POLLS: 24,
    ActionClass.LI_MESSAGES_AUTO: 30,
}
HARD_MAX_PER_WEEK: Final[dict[ActionClass, int]] = {
    ActionClass.PROFILE_VISITS: 500,
}

_DAY_DEFAULT: Final[dict[ActionClass, Callable[[BudgetSettings], int]]] = {
    ActionClass.CONNECTION_PAGES: lambda s: s.connection_pages_per_day,
    ActionClass.PROFILE_VISITS: lambda s: s.profile_visits_per_day,
    ActionClass.INBOX_POLLS: lambda s: s.inbox_polls_per_day,
    ActionClass.LI_MESSAGES_AUTO: lambda s: s.li_messages_auto_per_day,
}

# One entry per key in HARD_MAX_PER_WEEK, not one entry per ActionClass: only
# `PROFILE_VISITS` has a weekly limit today. Keying `_limits_for`'s week clamp
# off this dict, rather than reading `settings.profile_visits_per_week`
# unconditionally, means a second action class added to HARD_MAX_PER_WEEK
# without a matching default here fails loudly (KeyError) instead of silently
# inheriting profile-visits' weekly setting.
_WEEK_DEFAULT: Final[dict[ActionClass, Callable[[BudgetSettings], int]]] = {
    ActionClass.PROFILE_VISITS: lambda s: s.profile_visits_per_week,
}


class BudgetExceeded(RuntimeError):
    """:func:`consume` refused: ``period`` already has ``count`` recorded against ``limit``."""

    def __init__(self, action: ActionClass, period: Period, *, count: int, limit: int) -> None:
        self.action = action
        self.period = period
        self.count = count
        self.limit = limit
        super().__init__(
            f"{action.value}: {period} budget exceeded ({count} already recorded against a "
            f"limit of {limit})"
        )


@dataclass(frozen=True, slots=True)
class PeriodBudget:
    """One period's (day or week) count against its limit."""

    count: int
    limit: int

    @property
    def over(self) -> bool:
        """True once ``count`` has already gone past ``limit`` (spec 9.6's "overshoot by one")."""
        return self.count > self.limit

    @property
    def remaining(self) -> int:
        """How many more units fit under ``limit`` right now; never negative."""
        return max(self.limit - self.count, 0)


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """``action``'s day count, and its week count when spec 9.6 gives it one."""

    action: ActionClass
    day: PeriodBudget
    week: PeriodBudget | None


@dataclass(frozen=True, slots=True)
class LocalPeriod:
    """``now`` translated into the account owner's local calendar day and ISO week.

    Datetimes are stored naive UTC and returned aware (see ``CLAUDE.md``), so
    every counter key is derived here, in one place, from an aware ``now`` and
    ``user.timezone`` -- never from a bare UTC date, which would roll counters
    over at UTC midnight instead of the user's own midnight (the defect spec
    9.6's local-day acceptance test exists to catch).
    """

    day: date
    iso_year: int
    iso_week: int

    @classmethod
    def at(cls, user: User, now: datetime) -> LocalPeriod:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(_zone(user))
        iso_year, iso_week, _ = local.isocalendar()
        return cls(day=local.date(), iso_year=iso_year, iso_week=iso_week)


def status(
    session: Session,
    user: User,
    account_id: int,
    action: ActionClass,
    *,
    now: datetime,
    settings: BudgetSettings,
) -> BudgetSnapshot:
    """``action``'s current counts against its limits. Read-only: no writer session needed."""
    period = LocalPeriod.at(user, now)
    limits = _limits_for(action, settings)
    day_count = _read_count(session, user, _day_key(account_id, action, period.day))
    day = PeriodBudget(count=day_count, limit=limits.day)
    week = _week_budget(session, user, account_id, action, period, limits)
    return BudgetSnapshot(action=action, day=day, week=week)


def consume(
    session: Session,
    user: User,
    account_id: int,
    action: ActionClass,
    *,
    now: datetime,
    settings: BudgetSettings,
) -> BudgetSnapshot:
    """Enforce ``action``'s budget for one unit of work, then record it as spent.

    Call this once, immediately *before* the unit starts (spec 9.4, 9.9:
    enforcement is between units of work, never mid-unit) -- never after, and
    never partway through. It refuses only when a period's count *already
    exceeds* its limit, not merely when the count has reached it: a unit that
    finds the count sitting exactly at the limit is still let through, and it
    is that unit's own increment which puts the count one over. The very next
    call then sees a count already past the limit and refuses, so the count
    can end a run one unit over its limit but never two -- "overshoot by one
    unit is the maximum" (this item's acceptance criterion), not a design
    that merely happens to stay close.

    Needs a writer session (``session_scope(factory, write=True)``): every
    call reads the current counts before writing the incremented ones back.
    """
    _require_writer(session, "budgets.consume")
    period = LocalPeriod.at(user, now)
    limits = _limits_for(action, settings)

    day_key = _day_key(account_id, action, period.day)
    day_count = _read_count(session, user, day_key)
    if day_count > limits.day:
        raise BudgetExceeded(action, "day", count=day_count, limit=limits.day)

    week_key = None if limits.week is None else _week_key(account_id, action, period)
    week_count = None if week_key is None else _read_count(session, user, week_key)
    if limits.week is not None and week_count is not None and week_count > limits.week:
        raise BudgetExceeded(action, "week", count=week_count, limit=limits.week)

    day_count += 1
    set_setting(session, user, day_key, day_count)
    week = None
    if week_key is not None and limits.week is not None:
        week_count = (week_count or 0) + 1
        set_setting(session, user, week_key, week_count)
        week = PeriodBudget(count=week_count, limit=limits.week)

    day = PeriodBudget(count=day_count, limit=limits.day)
    return BudgetSnapshot(action=action, day=day, week=week)


def configured_default(
    action: ActionClass, settings: BudgetSettings, period: Period = "day"
) -> int | None:
    """What ``settings`` *asks* for, before spec 9.6's hard max clamps it.

    :func:`status` and :func:`consume` only ever report the clamped limit,
    which is the right number to enforce and the wrong number to answer "has
    anything been configured past its ceiling" with -- a ``config.toml``
    asking for 10,000 profile visits a day and one asking for exactly 100 are
    indistinguishable once the clamp has run. ``netkeeper.services.posture``
    compares the two to warn about the first; a settings UI wants the same
    pair. ``None`` means this action has no limit for ``period`` (only
    ``profile_visits`` has a weekly one).

    Reads the same ``_DAY_DEFAULT``/``_WEEK_DEFAULT`` tables :func:`_limits_for`
    does, so a caller cannot drift from enforcement by keeping its own copy of
    which config field backs which action class.
    """
    if period == "day":
        return _DAY_DEFAULT[action](settings)
    week_default = _WEEK_DEFAULT.get(action)
    return None if week_default is None else week_default(settings)


@dataclass(frozen=True, slots=True)
class _Limits:
    day: int
    week: int | None


def _limits_for(action: ActionClass, settings: BudgetSettings) -> _Limits:
    day = min(_DAY_DEFAULT[action](settings), HARD_MAX_PER_DAY[action])
    week_hard = HARD_MAX_PER_WEEK.get(action)
    week = None if week_hard is None else min(_WEEK_DEFAULT[action](settings), week_hard)
    return _Limits(day=day, week=week)


def _week_budget(
    session: Session,
    user: User,
    account_id: int,
    action: ActionClass,
    period: LocalPeriod,
    limits: _Limits,
) -> PeriodBudget | None:
    if limits.week is None:
        return None
    count = _read_count(session, user, _week_key(account_id, action, period))
    return PeriodBudget(count=count, limit=limits.week)


def _day_key(account_id: int, action: ActionClass, day: date) -> str:
    return f"linkedin.budget.{account_id}.{action.value}.day.{day.isoformat()}"


def _week_key(account_id: int, action: ActionClass, period: LocalPeriod) -> str:
    week = f"{period.iso_year:04d}-W{period.iso_week:02d}"
    return f"linkedin.budget.{account_id}.{action.value}.week.{week}"


def _read_count(session: Session, user: User, key: str) -> int:
    value = get_setting(session, user, key, default=0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"budget counter {key!r} is not an int: {value!r}")
    return value


def _zone(user: User) -> tzinfo:
    try:
        return ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "user %s has unknown timezone %r; budgets count from UTC", user.id, user.timezone
        )
        return UTC


def _require_writer(session: Session, where: str) -> None:
    if not is_writer(session):
        raise RuntimeError(
            f"{where} needs a writer session; use session_scope(factory, write=True)"
        )
