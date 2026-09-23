"""netkeeper.services.posture: every protection, and a warning for anything off (P2-11).

The module exists for one property, so most of this file tests that property:

    **turn any protection off and the report warns about that one, and only
    that one.**

``test_turning_one_protection_off_warns_about_exactly_that_one`` is where that
lives. It asserts set equality on the protections that warned, not membership,
which is what makes it mutation-proof in both directions: a report that stopped
warning fails the equality, and a report that warned about everything fails it
too. ``test_a_clean_setup_is_all_clear`` is the other half -- without a baseline
that genuinely says "all clear", a blanket warning would satisfy every case
above it.

Two traps this project keeps hitting, both guarded here:

* **constants pinned only against themselves.** Every threshold this module
  judges by is asserted against a literal written out below, never against
  ``posture.THING`` (CLAUDE.md).
* **fixtures too uniform for the case to arise.** The baseline ``NOW`` is a
  Wednesday so the weekend test has something to change; the weekend test uses
  a real Saturday; and ``test_the_warm_up_ramp_counts_local_days`` uses an
  Auckland user at a UTC instant whose UTC date and local date differ, so a
  UTC-based day count gives a different answer and the test can actually fail.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import factories
import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.cli import _session_probe
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.browser import AttachBrowserProvider
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.preflight import preflight
from netkeeper.models import SettingKV, User
from netkeeper.scoping import unscoped
from netkeeper.services import heat as heat_rows
from netkeeper.services import posture as posture_module
from netkeeper.services import scheduler as scheduler_module
from netkeeper.services.budgets import ActionClass, consume
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.posture import (
    ATTACH_ONLY,
    LOOPBACK_HOSTS,
    MAX_ACTIVE_WINDOW_HOURS,
    MAX_BLOCKS_BEFORE_SKIP,
    MIN_HALF_LIFE_HOURS,
    SINGLE_ACCOUNT_ID,
    WEEKEND_DAMPING_CEILING,
    PostureReport,
    Protection,
    SessionProbe,
    Status,
    posture,
    render,
)
from netkeeper.services.scheduler import (
    CATCHUP_MAX_MINUTES,
    CATCHUP_MIN_MINUTES,
    DEFAULT_SCHEDULES,
    HEAT_SKIP_DISABLED,
    MIN_JOB_KIND_GAP,
    HeatGate,
    JobKind,
    sync_account_schedule,
)
from netkeeper.services.settings_kv import delete_setting

#: A Wednesday, 14:00 in New York (the default configured zone), inside the
#: default 08:30-21:30 window. Deliberately not a weekend and deliberately
#: inside the window, so the cases that change either have something to change.
NOW = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
#: The Saturday of the same week, same local hour.
SATURDAY = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)

ACCOUNT = SINGLE_ACCOUNT_ID
DEFAULTS = Settings()
ZONE = DEFAULTS.linkedin.timezone

LOGGED_IN = SessionProbe(
    attached=True,
    logged_in=True,
    browser_version="Chrome/140.0.7339.80",
    cookie_names=("li_at", "JSESSIONID"),
)


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


def _make_user(writer: Session, *, timezone: str = ZONE, schedule: bool = True) -> User:
    """A local user as first start creates one, with a schedule established.

    The schedule is part of the baseline because the scheduler-side protections
    (active hours deferral, the catch-up rule, the heat skip gate) need one to
    act on, and ``_scheduled_jobs`` says so. A user without one is the
    "nothing scheduled" case, which is a warning on purpose.
    """
    user = factories.make_user(writer, timezone=timezone, created_at=NOW - timedelta(days=3))
    if schedule:
        sync_account_schedule(writer, user, ACCOUNT, now=NOW, rng=random.Random(4), tz=timezone)
    return user


@pytest.fixture
def user(writer: Session) -> User:
    """The local user as first start creates one: timezone from the config."""
    return _make_user(writer)


def _linkedin(**overrides: Any) -> Settings:
    """``Settings()`` with ``[linkedin]`` keys replaced."""
    return replace(DEFAULTS, linkedin=replace(DEFAULTS.linkedin, **overrides))


def _budget(**overrides: Any) -> Settings:
    return _linkedin(budget=replace(DEFAULTS.linkedin.budget, **overrides))


def _heat(**overrides: Any) -> Settings:
    return _linkedin(heat=replace(DEFAULTS.linkedin.heat, **overrides))


def _campaigns(**overrides: Any) -> Settings:
    return replace(DEFAULTS, campaigns=replace(DEFAULTS.campaigns, **overrides))


def _report(
    session: Session,
    user: User,
    *,
    settings: Settings = DEFAULTS,
    probe: SessionProbe | None = LOGGED_IN,
    browser_mode: str = ATTACH_ONLY,
    now: datetime = NOW,
    heat_gate: HeatGate | None = None,
) -> PostureReport:
    return posture(
        session,
        user,
        ACCOUNT,
        now=now,
        settings=settings,
        browser_mode=browser_mode,
        probe=probe,
        heat_gate=heat_gate,
    )


def _warned(report: PostureReport) -> set[str]:
    return {protection.name for protection in report.protections if protection.warnings}


def _not_in_force(report: PostureReport) -> set[str]:
    return {protection.name for protection in report.disabled}


# --- the baseline -------------------------------------------------------------


def test_a_clean_setup_is_all_clear(writer: Session, user: User) -> None:
    """Without this, a report that warned about everything would pass every case below."""
    report = _report(writer, user)

    assert _warned(report) == set()
    assert _not_in_force(report) == set()
    assert report.ok
    assert report.warnings == ()


def test_the_baseline_covers_every_protection_the_item_asks_for(
    writer: Session, user: User
) -> None:
    """The list of what a posture report must cover, as a list somebody edits on purpose."""
    names = [protection.name for protection in _report(writer, user).protections]

    assert names == [
        "attach-only browser",
        "linkedin session",
        "session flag",
        "active hours",
        "one local midnight",
        "weekend damping",
        "warm-up ramp",
        "manual linkedin sends",
        "budget connection_pages",
        "budget profile_visits",
        "budget inbox_polls",
        "budget li_messages_auto",
        "heat",
        "heat skip gate",
        "scheduled jobs",
    ]


# --- one protection off at a time ---------------------------------------------


def _raise_heat(times: int) -> Callable[[Session, User], None]:
    def prepare(session: Session, user: User) -> None:
        for _ in range(times):
            heat_rows.raise_heat(session, user, ACCOUNT, now=NOW, settings=DEFAULTS.linkedin.heat)

    return prepare


def _spend(action: ActionClass, times: int) -> Callable[[Session, User], None]:
    def prepare(session: Session, user: User) -> None:
        for _ in range(times):
            consume(session, user, ACCOUNT, action, now=NOW, settings=DEFAULTS.linkedin.budget)

    return prepare


def _drop_one_job(kind: JobKind) -> Callable[[Session, User], None]:
    """Wipe one job kind's stored due time, leaving a half-established schedule.

    Through ``delete_setting`` on the scheduler's own key, so this stays a real
    state the scheduler could leave behind rather than a shape invented here.
    """

    def prepare(session: Session, user: User) -> None:
        deleted = delete_setting(session, user, scheduler_module._key(ACCOUNT, kind))
        assert deleted, "the fixture did not actually remove a scheduled job"

    return prepare


def _flag(session: Session, user: User) -> None:
    # A linkedin.com string in a fixture is fine; nothing fetches it (CLAUDE.md).
    flag_session(
        session, user, Outcome.CHECKPOINT, url="https://www.linkedin.com/checkpoint/challenge"
    )


@dataclass(frozen=True)
class Case:
    """One protection turned off, and what the report must then say."""

    id: str
    #: Protections that must carry at least one warning. Set equality: nothing else may.
    warns: tuple[str, ...]
    #: Of those, the ones whose status must no longer be ``ON``.
    off: tuple[str, ...] = ()
    settings: Settings = DEFAULTS
    probe: SessionProbe | None = LOGGED_IN
    browser_mode: str = ATTACH_ONLY
    now: datetime = NOW
    prepare: Callable[[Session, User], None] | None = field(default=None)
    user_timezone: str = ZONE
    heat_gate: HeatGate | None = None
    #: False leaves the account with no schedule established.
    schedule: bool = True


CASES = [
    Case(
        id="a provider that could launch a browser",
        browser_mode="launch",
        warns=("attach-only browser",),
        off=("attach-only browser",),
    ),
    Case(
        id="a debug port on another machine",
        settings=_linkedin(cdp_url="http://192.168.1.50:9222"),
        warns=("attach-only browser",),
    ),
    Case(
        id="no browser probe at all",
        probe=None,
        warns=("linkedin session",),
        off=("linkedin session",),
    ),
    Case(
        id="no LinkedIn session in the profile",
        probe=replace(LOGGED_IN, logged_in=False, cookie_names=()),
        warns=("linkedin session",),
        off=("linkedin session",),
    ),
    Case(
        id="an unreadable cookie jar",
        probe=replace(LOGGED_IN, logged_in=None),
        warns=("linkedin session",),
        off=("linkedin session",),
    ),
    Case(
        id="Chrome not reachable",
        probe=SessionProbe(attached=False, logged_in=None, problems=("cannot attach",)),
        warns=("linkedin session",),
        off=("linkedin session",),
    ),
    Case(id="a checkpoint flag standing", prepare=_flag, warns=("session flag",)),
    Case(
        id="active hours open all day",
        settings=_linkedin(active_hours=("09:00", "09:00")),
        warns=("active hours",),
        off=("active hours",),
    ),
    Case(
        id="an active window longer than a waking day",
        settings=_linkedin(active_hours=("06:00", "23:59")),
        warns=("active hours",),
        off=("active hours",),
    ),
    Case(
        id="active hours that are not times",
        settings=_linkedin(active_hours=("oops", "21:30")),
        warns=("active hours",),
        off=("active hours",),
    ),
    Case(
        id="a timezone this machine does not know",
        settings=_linkedin(timezone="Mars/Phobos"),
        warns=("active hours", "one local midnight"),
        off=("one local midnight",),
    ),
    Case(
        id="budget days and active hours in different zones",
        user_timezone="UTC",
        warns=("one local midnight",),
        off=("one local midnight",),
    ),
    Case(
        id="weekend damping switched off",
        settings=_linkedin(weekend_multiplier=1.0),
        warns=("weekend damping",),
        off=("weekend damping",),
    ),
    Case(
        id="weekend damping that raises weekend budgets",
        settings=_linkedin(weekend_multiplier=1.5),
        warns=("weekend damping",),
        off=("weekend damping",),
    ),
    Case(
        id="a warm-up ramp that starts at the cap",
        settings=_budget(warmup_start=60),
        warns=("warm-up ramp",),
        off=("warm-up ramp",),
    ),
    Case(
        id="LinkedIn auto-send turned on",
        settings=_campaigns(linkedin_auto_send=True),
        warns=("manual linkedin sends",),
        off=("manual linkedin sends",),
    ),
    Case(
        id="a daily budget configured past spec 9.6's hard max",
        settings=_budget(connection_pages_per_day=10_000),
        warns=("budget connection_pages",),
    ),
    Case(
        id="a weekly budget configured past spec 9.6's hard max",
        settings=_budget(profile_visits_per_week=9_999),
        warns=("budget profile_visits",),
    ),
    Case(
        id="a day's budget already spent",
        prepare=_spend(ActionClass.INBOX_POLLS, 9),
        warns=("budget inbox_polls",),
    ),
    Case(
        id="heat that never rises",
        settings=_heat(per_block=0.0),
        warns=("heat",),
        off=("heat",),
    ),
    Case(
        id="heat that decays before the next burst",
        settings=_heat(half_life_hours=0),
        warns=("heat",),
        off=("heat",),
    ),
    Case(
        id="a skip threshold too high to be a brake",
        settings=_heat(skip_threshold=100.0),
        warns=("heat",),
        off=("heat",),
    ),
    Case(
        id="a skip threshold that skips everything",
        settings=_heat(skip_threshold=0.0),
        warns=("heat",),
    ),
    Case(id="heat over its skip threshold", prepare=_raise_heat(3), warns=("heat",)),
    Case(
        id="the scheduler's heat skip gate disabled",
        heat_gate=HEAT_SKIP_DISABLED,
        warns=("heat skip gate",),
        off=("heat skip gate",),
    ),
    Case(
        id="no schedule established at all",
        schedule=False,
        warns=("scheduled jobs",),
        off=("scheduled jobs",),
    ),
    Case(
        id="a schedule with a job kind missing",
        prepare=_drop_one_job(JobKind.ENRICH),
        warns=("scheduled jobs",),
        off=("scheduled jobs",),
    ),
]


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_turning_one_protection_off_warns_about_exactly_that_one(
    writer: Session, case: Case
) -> None:
    """The property the module exists for, one protection at a time.

    Set equality in both directions: a report that stopped warning fails, and a
    report that warns about everything fails the baseline test above.
    """
    user = _make_user(writer, timezone=case.user_timezone, schedule=case.schedule)
    if case.prepare is not None:
        case.prepare(writer, user)

    report = _report(
        writer,
        user,
        settings=case.settings,
        probe=case.probe,
        browser_mode=case.browser_mode,
        now=case.now,
        heat_gate=case.heat_gate,
    )

    assert _warned(report) == set(case.warns)
    assert _not_in_force(report) == set(case.off)
    assert not report.ok


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_no_protection_is_ever_off_and_silent(writer: Session, case: Case) -> None:
    """The structural half of the property, checked on every case as it is actually built."""
    user = _make_user(writer, timezone=case.user_timezone, schedule=case.schedule)
    if case.prepare is not None:
        case.prepare(writer, user)

    report = _report(
        writer,
        user,
        settings=case.settings,
        probe=case.probe,
        browser_mode=case.browser_mode,
        now=case.now,
        heat_gate=case.heat_gate,
    )

    for protection in report.protections:
        if protection.status is not Status.ON:
            assert protection.warnings, f"{protection.name} is {protection.status} and silent"


def test_each_warning_says_which_thing_is_wrong_not_merely_that_something_is(
    writer: Session,
) -> None:
    """Two protections have more than one way to be off, and the messages differ.

    A report whose 24-hour window was reported as "a long window", or whose
    half-established schedule was reported as "nothing scheduled", still warns
    -- and still sends the reader to the wrong setting. Asserting only that
    *something* warned cannot tell those apart, so the wording a reviewer acts
    on is pinned here.
    """
    user = _make_user(writer)
    all_day = _report(writer, user, settings=_linkedin(active_hours=("09:00", "09:00")))
    long_window = _report(writer, user, settings=_linkedin(active_hours=("06:00", "23:59")))
    _drop_one_job(JobKind.ENRICH)(writer, user)
    partial = _report(writer, user)
    bare = _report(writer, _make_user(writer, schedule=False))

    assert "active all 24 hours" in _warning_for(all_day, "active hours")
    assert "18.0 hours long" in _warning_for(long_window, "active hours")
    assert "enrich" in _warning_for(partial, "scheduled jobs")
    assert "no job kind has a due time" in _warning_for(bare, "scheduled jobs")


def _warning_for(report: PostureReport, name: str) -> str:
    protection = next(p for p in report.protections if p.name == name)
    assert protection.warnings, f"{name} did not warn"
    return " ".join(protection.warnings)


def test_a_protection_cannot_be_built_off_and_silent() -> None:
    """The invariant is enforced by the type, not by every builder remembering it."""
    with pytest.raises(ValueError, match="with no warning"):
        Protection(name="budgets", status=Status.OFF, value="off")
    with pytest.raises(ValueError, match="with no warning"):
        Protection(name="budgets", status=Status.UNKNOWN, value="?")
    # ON with no warning is the ordinary case, and ON *with* one is allowed: a
    # clamp that held is worth saying out loud without being a failure.
    assert Protection(name="budgets", status=Status.ON, value="fine").warnings == ()
    assert Protection(name="budgets", status=Status.ON, value="fine", warnings=("hm",)).warnings


def test_several_protections_off_at_once_all_warn(writer: Session, user: User) -> None:
    """Warnings accumulate rather than the first one masking the rest."""
    settings = replace(
        _linkedin(weekend_multiplier=1.0, heat=replace(DEFAULTS.linkedin.heat, per_block=0.0)),
        campaigns=replace(DEFAULTS.campaigns, linkedin_auto_send=True),
    )

    report = _report(writer, user, settings=settings, browser_mode="launch", probe=None)

    assert _not_in_force(report) == {
        "attach-only browser",
        "linkedin session",
        "weekend damping",
        "manual linkedin sends",
        "heat",
    }
    assert len(report.warnings) >= 5


# --- the thresholds, pinned to literals ----------------------------------------
# Asserted against numbers written out here, never against the module's own name
# for them: `assert X == posture.X` passes whatever X says (CLAUDE.md).


def test_attach_is_the_only_mode() -> None:
    assert ATTACH_ONLY == "attach"


def test_the_provider_still_reports_the_mode_this_module_expects() -> None:
    """The other half: a provider that renamed its mode must not pass silently."""
    assert AttachBrowserProvider.mode == "attach"


def test_loopback_hosts_are_the_three_spellings_of_this_machine() -> None:
    assert {"127.0.0.1", "localhost", "::1"} == LOOPBACK_HOSTS


def test_the_skip_threshold_ceiling_is_five_blocks() -> None:
    assert MAX_BLOCKS_BEFORE_SKIP == 5


def test_the_half_life_floor_is_one_hour() -> None:
    assert MIN_HALF_LIFE_HOURS == 1.0


def test_the_active_window_ceiling_is_sixteen_hours() -> None:
    assert MAX_ACTIVE_WINDOW_HOURS == 16.0


def test_weekend_damping_stops_damping_at_one() -> None:
    assert WEEKEND_DAMPING_CEILING == 1.0


def test_the_single_account_id_is_one() -> None:
    assert SINGLE_ACCOUNT_ID == 1


def test_the_catch_up_window_this_report_states_is_the_scheduler_s() -> None:
    """Spec 9.4's "catch-up after downtime (once, 5 to 20 minutes after start)".

    The scheduler pins its own constants; this pins the numbers *this report
    prints*, against the same literals, so a report that quietly rendered a
    different window would fail here rather than mislead a reviewer.
    """
    assert (CATCHUP_MIN_MINUTES, CATCHUP_MAX_MINUTES) == (5.0, 20.0)
    assert timedelta(minutes=2) == MIN_JOB_KIND_GAP


def test_the_cadences_this_report_states_are_the_scheduler_s() -> None:
    """Spec 9.4: incremental daily, full weekly. The other two are the scheduler's own."""
    assert {kind.value: schedule.interval for kind, schedule in DEFAULT_SCHEDULES.items()} == {
        "connections_incremental": timedelta(days=1),
        "connections_full": timedelta(days=7),
        "enrich": timedelta(hours=3),
        "inbox": timedelta(hours=3),
    }


def test_the_report_states_the_scheduler_s_settings_and_next_fires(
    writer: Session, user: User
) -> None:
    report = _report(writer, user)

    assert report.scheduler.heat_skip is True
    assert report.scheduler.catchup_minutes == (5.0, 20.0)
    assert report.scheduler.job_kind_gap_minutes == 2.0
    assert report.scheduler.unscheduled == ()
    assert len(report.scheduler.jobs) == len(DEFAULT_SCHEDULES)
    for kind, _, due in report.scheduler.jobs:
        assert due is not None, kind
        assert due > NOW


def test_a_disabled_heat_skip_is_visible_in_the_structure_too(writer: Session, user: User) -> None:
    """Not only in the warning: a JSON consumer (P2-10) reads the flag."""
    assert _report(writer, user, heat_gate=HEAT_SKIP_DISABLED).scheduler.heat_skip is False
    assert _report(writer, user).scheduler.heat_skip is True


def test_the_heat_gate_can_carry_its_own_threshold(writer: Session, user: User) -> None:
    """A scheduler run with heat settings other than the config's is reported as run.

    Reading the config here instead would describe a gate nobody is using.
    """
    strict = replace(DEFAULTS.linkedin.heat, skip_threshold=1.0)

    report = _report(writer, user, heat_gate=strict)

    assert report.heat.threshold == 1.0
    assert "of 1" in next(p for p in report.protections if p.name == "heat").value
    assert report.ok


def test_the_rendered_report_shows_when_each_job_next_fires(writer: Session, user: User) -> None:
    text = render(_report(writer, user))

    assert "JOB" in text and "CADENCE" in text and "NEXT DUE" in text
    for kind in DEFAULT_SCHEDULES:
        assert kind.value in text
    assert "every 1 d" in text and "every 3 h" in text
    assert "5 to 20 minutes" in text
    assert "kept 2 minutes apart" in text


def test_the_defaults_this_report_calls_clean_are_appendix_c_s(writer: Session, user: User) -> None:
    """Appendix C's numbers are what an all-clear report is all-clear *about*.

    CP3 asks "are the defaults in Appendix C what the code does". If a default
    drifted, the baseline above would keep passing -- it only checks that
    nothing warned -- so the numbers themselves are pinned here.
    """
    assert DEFAULTS.linkedin.active_hours == ("08:30", "21:30")
    assert DEFAULTS.linkedin.weekend_multiplier == 0.5
    assert DEFAULTS.linkedin.budget.profile_visits_per_day == 60
    assert DEFAULTS.linkedin.budget.profile_visits_per_week == 300
    assert DEFAULTS.linkedin.budget.warmup_start == 20
    assert DEFAULTS.linkedin.budget.warmup_step == 10
    assert DEFAULTS.linkedin.heat.half_life_hours == 6
    assert DEFAULTS.linkedin.heat.skip_threshold == 2.5
    assert DEFAULTS.campaigns.linkedin_auto_send is False


# --- the numbers the report carries ---------------------------------------------


def test_now_is_a_parameter_not_a_clock_read(writer: Session, user: User) -> None:
    """Two instants, one database, two different answers. A ``datetime.now()`` inside
    would make these agree."""
    inside = _report(writer, user, now=NOW).protections[3]
    outside = _report(writer, user, now=NOW - timedelta(hours=10)).protections[3]

    assert inside.name == outside.name == "active hours"
    assert "inside" in inside.value
    assert "outside" in outside.value


def test_the_weekend_multiplier_halves_saturdays_budget(writer: Session, user: User) -> None:
    weekday = _report(writer, user, now=NOW).today
    saturday = _report(writer, user, now=SATURDAY).today

    assert weekday.after_weekend == weekday.ramp
    assert saturday.after_weekend == saturday.ramp // 2
    assert saturday.ramp > 0


def test_the_warm_up_ramp_counts_local_days_not_utc_days(writer: Session) -> None:
    """An Auckland user whose local date and UTC date disagree.

    Installed at 23:00 local on the 20th (11:00 UTC the same day) and read at
    01:00 local on the 23rd (13:00 UTC on the 22nd): three local days have
    turned over and only two UTC ones. A UTC-based count answers 40 here, so
    this test can genuinely fail.
    """
    auckland = _linkedin(timezone="Pacific/Auckland")
    user = factories.make_user(
        writer,
        timezone="Pacific/Auckland",
        created_at=datetime(2026, 9, 20, 11, 0, tzinfo=UTC),
    )

    report = _report(writer, user, settings=auckland, now=datetime(2026, 9, 22, 13, 0, tzinfo=UTC))

    assert report.today.ramp == 50  # 20 + 10 * 3 local days, not 20 + 10 * 2


def test_the_ramp_starts_at_twenty_on_install_day(writer: Session) -> None:
    user = factories.make_user(writer, timezone=ZONE, created_at=NOW)

    assert _report(writer, user).today.ramp == 20


def test_todays_budget_shows_each_protections_bite_in_order(writer: Session, user: User) -> None:
    """Warm-up, then the weekend, then heat -- the order a run applies them in.

    Exact numbers rather than inequalities, so a reordering that happened to
    produce a smaller number would still fail: day 6 of a ramp that tops out at
    60, halved to 30 for a Saturday, then divided by the 2.0 cooldown multiplier
    one block of heat produces.
    """
    heat_rows.raise_heat(writer, user, ACCOUNT, now=SATURDAY, settings=DEFAULTS.linkedin.heat)

    today = _report(writer, user, now=SATURDAY).today

    assert today.ramp == 60
    assert today.after_weekend == 30
    assert today.after_heat == 15
    assert today.after_heat >= 1  # spec 9.7: shrinks, never to zero


def test_spent_budget_shows_up_against_the_limit(writer: Session, user: User) -> None:
    _spend(ActionClass.PROFILE_VISITS, 4)(writer, user)

    report = _report(writer, user)
    visits = next(p for p in report.protections if p.name == "budget profile_visits")

    assert report.today.spent == 4
    assert "4/60 today" in visits.value
    assert "4/300 this week" in visits.value
    assert report.ok


def test_heat_reports_when_runs_resume(writer: Session, user: User) -> None:
    """Spec 9.7: "when runs resume". Checked by decaying to that instant, not by
    restating the arithmetic."""
    _raise_heat(3)(writer, user)

    report = _report(writer, user)

    assert report.heat.tripped
    assert report.heat.last_raised_at == NOW
    resumes_at = report.heat.resumes_at
    assert resumes_at is not None
    assert NOW < resumes_at < NOW + timedelta(hours=6)
    settled = heat_rows.read(writer, user, ACCOUNT, now=resumes_at, settings=DEFAULTS.linkedin.heat)
    assert math.isclose(settled, DEFAULTS.linkedin.heat.skip_threshold, rel_tol=1e-9)


def test_heat_that_was_never_raised_says_so(writer: Session, user: User) -> None:
    report = _report(writer, user)

    assert report.heat.last_raised_at is None
    assert report.heat.resumes_at is None
    assert report.heat.score == 0.0
    assert "never raised" in next(p for p in report.protections if p.name == "heat").value


def test_a_broken_half_life_is_reported_rather_than_raised(writer: Session, user: User) -> None:
    """The decay math refuses a non-positive half-life. A posture report must survive
    the one config it exists to complain about."""
    report = _report(writer, user, settings=_heat(half_life_hours=0))

    assert not report.heat.readable
    assert "score unreadable" in next(p for p in report.protections if p.name == "heat").value


# --- read-only, and quiet about secrets -----------------------------------------


def test_posture_writes_nothing(session_factory: sessionmaker[Session]) -> None:
    """Read-only, on a plain reader session: no writer needed and no row changed."""
    with session_scope(session_factory, write=True) as setup:
        user = factories.make_user(setup, timezone=ZONE, created_at=NOW - timedelta(days=3))
        _spend(ActionClass.PROFILE_VISITS, 2)(setup, user)
        _raise_heat(1)(setup, user)
        user_id = user.id

    with session_scope(session_factory) as reader:
        stored = reader.get(User, user_id)
        assert stored is not None
        count = unscoped(select(func.count()).select_from(SettingKV))
        before = reader.scalar(count)

        report = _report(reader, stored)

        assert report.today.spent == 2
        assert not reader.new and not reader.dirty and not reader.deleted
        assert reader.scalar(count) == before


async def test_a_cookie_value_never_reaches_the_report(writer: Session, user: User) -> None:
    """Preflight reads cookie names and not values (spec 9.1). Posture must not undo that.

    The fake jar below really does hold a value, so a report that started
    carrying one would fail here rather than pass vacuously.
    """
    secret = "AQEDATOTALLYFAKESESSIONVALUE"
    context = FakeContext(
        cookies=[
            {"name": "li_at", "value": secret, "domain": ".linkedin.com", "expires": -1},
            {"name": "JSESSIONID", "value": f'"{secret}"', "domain": ".www.linkedin.com"},
        ],
        evaluate_result={"userAgent": "Chrome/140.0.7339.80", "timezone": ZONE},
    )
    provider = AttachBrowserProvider(
        "http://127.0.0.1:9222", connector=FakeConnector([FakeBrowser([context])])
    )

    probe = _session_probe(await preflight(provider))
    text = render(_report(writer, user, probe=probe))

    assert probe.cookie_names == ("li_at", "JSESSIONID")
    assert secret not in text
    assert "li_at" in text  # the names are the point; without this the check is vacuous


# --- the rendered table ----------------------------------------------------------


def test_the_rendered_report_names_every_protection(writer: Session, user: User) -> None:
    report = _report(writer, user)
    text = render(report)

    for protection in report.protections:
        assert protection.name in text
    assert "all clear" in text
    assert "not covered by this report:" in text


def test_the_rendered_report_never_reads_as_all_clear_with_something_off(
    writer: Session, user: User
) -> None:
    text = render(_report(writer, user, browser_mode="launch"))

    assert "NOT all clear" in text
    assert "second device" in text


def test_the_report_states_the_gaps_it_cannot_see(writer: Session, user: User) -> None:
    """The scheduler is on another branch; saying so is better than a silent omission."""
    gaps = " ".join(_report(writer, user).gaps)

    assert "activity lock guards one process" in gaps
    assert "scheduler" in gaps
    assert posture_module.GAPS  # a report with no gaps listed would pass the two above
