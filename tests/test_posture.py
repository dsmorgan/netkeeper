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

import importlib
import math
import os
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import call_targets
import factories
import pytest
import test_browser_safety as browser_safety
from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.cli import _session_probe
from netkeeper.config import Settings
from netkeeper.db import session_scope
from netkeeper.linkedin import activity_lock
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
    ENFORCED_BY,
    LOOPBACK_HOSTS,
    MAX_ACTIVE_WINDOW_HOURS,
    MAX_BLOCKS_BEFORE_SKIP,
    MAX_PLAUSIBLE_HOLD_HOURS,
    MIN_HALF_LIFE_HOURS,
    SINGLE_ACCOUNT_ID,
    UNENFORCED_TODAY,
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


def _pacing(**overrides: Any) -> Settings:
    return _linkedin(pacing=replace(DEFAULTS.linkedin.pacing, **overrides))


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
    lock: activity_lock.LockState | None = None,
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
        lock=lock,
    )


def _row(report: PostureReport, name: str) -> Protection:
    return next(protection for protection in report.protections if protection.name == name)


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
        "one browser client",
        "linkedin session",
        "session flag",
        "active hours",
        "one local midnight",
        "weekend damping",
        "human-like pacing",
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


def _clear_heat(session: Session, user: User) -> None:
    """Raise heat, then clear it by hand -- spec 9.7's "the block was something else"."""
    heat_rows.raise_heat(
        session, user, ACCOUNT, now=NOW - timedelta(hours=30), settings=DEFAULTS.linkedin.heat
    )
    heat_rows.clear(session, user, ACCOUNT, now=NOW - timedelta(minutes=5))


def _flag(session: Session, user: User) -> None:
    # A linkedin.com string in a fixture is fine; nothing fetches it (CLAUDE.md).
    flag_session(
        session, user, Outcome.CHECKPOINT, url="https://www.linkedin.com/checkpoint/challenge"
    )


#: A pid no process has: above every platform's pid_max, inside a 32-bit pid_t.
NO_SUCH_PID = 2**31 - 1

_UNSET: Any = object()


def _held(
    *,
    holder: Any = _UNSET,
    pid: int | None = None,
    command: str = "netkeeper serve",
    since: datetime | None = None,
) -> activity_lock.LockState:
    """The lock as posture would inspect it while a run holds it. Alive, ours, recent."""
    if holder is _UNSET:
        holder = activity_lock.Holder(
            pid=os.getpid() if pid is None else pid,
            command=command,
            since=NOW - timedelta(minutes=10) if since is None else since,
        )
    return activity_lock.LockState(
        account=activity_lock.SINGLE_ACCOUNT_KEY,
        path=Path("/nonexistent/locks/browser-local.lock"),
        held=True,
        holder=holder,
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
    #: The activity lock as inspected; ``None`` inspects the (free) real one.
    lock: activity_lock.LockState | None = None
    #: True for a case that must leave the report *clean*: a state worth
    #: covering because getting it wrong would produce a spurious warning,
    #: rather than because it should produce one.
    expect_ok: bool = False


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
        id="heat that reads cold the moment it is raised",
        settings=_heat(per_block=0.005, skip_threshold=0.02),
        warns=("heat",),
        off=("heat",),
    ),
    Case(
        id="heat exactly at the cold cutoff, which never accumulates",
        settings=_heat(per_block=0.01, skip_threshold=0.04),
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
        id="no wait at all between profile views",
        settings=_pacing(profile_delay_median_s=0),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="a wait too short to be a person reading",
        settings=_pacing(profile_delay_median_s=2),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="the same wait every time",
        settings=_pacing(profile_delay_sigma=0.0),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="a session that is never interrupted",
        settings=_pacing(distraction_p=0.0),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="a distraction pause of zero seconds",
        settings=_pacing(distraction_range_s=(0, 0)),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="a burst bigger than the whole day's budget",
        settings=_pacing(burst_size=(10_000, 10_000)),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="a burst size pacing would refuse",
        settings=_pacing(burst_size=(0, 15)),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="no break between bursts",
        settings=_pacing(burst_break_s=(0, 0)),
        warns=("human-like pacing",),
        off=("human-like pacing",),
    ),
    Case(
        id="an active window that runs overnight",
        settings=_linkedin(active_hours=("21:30", "08:30")),
        warns=("active hours",),
    ),
    Case(
        id="heat cleared by hand rather than never raised",
        prepare=_clear_heat,
        warns=(),
        expect_ok=True,
    ),
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
    Case(
        id="an ordinary run holding the activity lock",
        lock=_held(),
        warns=(),
        expect_ok=True,
    ),
    Case(
        id="an activity lock held with no readable note",
        lock=_held(holder=None),
        warns=("one browser client",),
    ),
    Case(
        id="an activity lock held by a pid that is not running",
        lock=_held(pid=NO_SUCH_PID),
        warns=("one browser client",),
    ),
    Case(
        id="an activity lock held by something that is not netkeeper",
        lock=_held(command="python3 some-other-tool.py"),
        warns=("one browser client",),
    ),
    Case(
        id="an activity lock held longer than any run takes",
        lock=_held(since=NOW - timedelta(hours=4, minutes=30)),
        warns=("one browser client",),
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
        lock=case.lock,
    )

    assert _warned(report) == set(case.warns)
    assert _not_in_force(report) == set(case.off)
    assert report.ok is case.expect_ok


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
        lock=case.lock,
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

    # The pacing row has six ways to be off and they send you to six different
    # config keys, so "something about pacing is wrong" is not good enough.
    for overrides, expected in (
        ({"profile_delay_median_s": 0}, "raises part-way through a run"),
        ({"profile_delay_median_s": 2}, "under the 5s this report treats as human"),
        ({"profile_delay_sigma": 0.0}, "every wait is exactly the median"),
        ({"distraction_p": 0.0}, "never pauses for a distraction"),
        ({"burst_size": (10_000, 10_000)}, "a burst never reaches its own end"),
        ({"burst_break_s": (0, 0)}, "sessions, not streams"),
    ):
        report = _report(writer, user, settings=_pacing(**overrides))
        assert expected in _warning_for(report, "human-like pacing"), overrides

    overnight = _report(writer, user, settings=_linkedin(active_hours=("21:30", "08:30")))
    assert "runs overnight" in _warning_for(overnight, "active hours")


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


# --- the claim the report is entitled to make -----------------------------------


def _callers_of(function: str, *, live_only: bool = True) -> set[Path]:
    """Every file under ``netkeeper/`` with a call that resolves to ``function``.

    Resolves each call through the file's imports (``call_targets``, #162) and
    matches ``function`` fully qualified, so it sees the call however the
    name was imported. ``posture.py`` is excluded because reading a counter is
    not enforcing a limit, and ``rehearse.py`` because a rehearsal is by
    definition not the live run whose budget has to be enforced.

    ``simulate_run.py`` is excluded for the same reason as ``rehearse.py``,
    P2-11's other rehearsal command: its injected job handler calls
    ``budgets.consume``, ``raise_heat``, ``warmup_budget``, and
    ``apply_weekend_multiplier`` for real, against a scratch database it
    creates and deletes itself (see that module's docstring) -- but spending a
    *simulated* account's budget is not production enforcement, whatever
    functions it happens to call to do it convincingly. Counting it here would
    shrink ``UNENFORCED_TODAY`` over a gap that has not actually closed: no
    live job calls any of these four yet. #156's PR added this line rather
    than let the list shrink on its own.

    With ``live_only`` (the default), a caller also has to be *live*: reached,
    through imports, from something netkeeper actually runs (:func:`_live_modules`).
    A job that exists but that no command, route, or server start ever reaches
    enforces nothing yet -- the connections sync runner (P2-06) called
    ``consume``, ``raise_heat``, and ``flag_session`` for real, and nothing
    started it (#167's review).
    """
    package = Path(browser_safety.PACKAGE)
    live = _live_modules() if live_only else None
    found: set[Path] = set()
    for path in browser_safety.python_files(package):
        if path in _not_production(package):
            continue
        if live is not None and path not in live:
            continue
        if _calls(path.read_text(encoding="utf-8"), path, function):
            found.add(path)
    return found


def _not_production(package: Path) -> set[Path]:
    """Modules that call enforcement for a rehearsal or a report, never for a live run."""
    return {
        package / "services" / "posture.py",
        package / "linkedin" / "rehearse.py",
        package / "services" / "simulate_run.py",
    }


def _entry_points() -> set[Path]:
    """What netkeeper runs: the CLI (``[project.scripts]``), the app factory ``serve``
    starts, and the API modules that factory discovers by package scan rather than
    by import (so no import edge would reach them)."""
    package = Path(browser_safety.PACKAGE)
    api = package / "web" / "api"
    return {package / "cli.py", package / "web" / "app.py", *api.glob("*.py")}


def _module_file(name: str) -> Path | None:
    """The file a dotted ``netkeeper.*`` name is, or lives in; None outside the package."""
    parts = name.split(".")
    if parts[0] != "netkeeper":
        return None
    root = Path(browser_safety.REPO_ROOT)
    while parts:
        candidate = root.joinpath(*parts)
        if candidate.with_suffix(".py").is_file():
            return candidate.with_suffix(".py")
        if (candidate / "__init__.py").is_file():
            return candidate / "__init__.py"
        parts = parts[:-1]
    return None


def _live_modules() -> set[Path]:
    """Every module an entry point reaches through imports, module-level or in a function.

    Liveness here is *import* reachability, not call reachability: an import
    inside a function nobody calls still counts, and so does a module imported
    only for a constant. It is a necessary condition for enforcement, not proof
    of it. So treat any shrink of ``UNENFORCED_TODAY`` as a claim to verify by
    reading the path from the entry point to the call, not as something this
    scan has established.

    The rehearsal and report modules (:func:`_not_production`) are reached but not
    followed: ``netkeeper simulate`` importing the scheduler runs a *simulated*
    schedule, and ``netkeeper posture`` importing it reads constants. Neither makes
    the scheduler part of a live run.
    """
    package = Path(browser_safety.PACKAGE)
    not_followed = _not_production(package)
    reached: set[Path] = set()
    queue = sorted(_entry_points())
    while queue:
        path = queue.pop()
        if path in reached:
            continue
        reached.add(path)
        if path in not_followed:
            continue
        source = path.read_text(encoding="utf-8")
        for _, name in browser_safety.imported_names(source, path):
            target = _module_file(name)
            if target is not None and target not in reached:
                queue.append(target)
    return reached


def _calls(source: str, path: Path, function: str) -> bool:
    """Whether ``source``, as the file at ``path``, uses the fully qualified ``function``.

    A key written ``function[reference]`` needs both: a use of ``function`` and
    a use of ``reference`` in the same file. That is how a budget is keyed per
    action class (``consume[...ActionClass.CONNECTION_PAGES]``): one caller of
    ``consume`` enforces the classes it names, not every class ``consume``
    could be handed.

    A use is any reference that resolves to ``function``, called or handed over.
    A definition site calling itself is the implementation, not a consumer
    enforcing a limit, so the defining module never counts. An unrelated
    method of the same name (``queue.consume()``) does not count either, and a
    file that defines its own ``consume`` still counts its real call. Nothing
    is followed transitively: a helper that happens to reach a key is not the
    key (see ``ENFORCED_BY`` on ``plan_enrichment``).
    """
    function, reference = _split_key(function)
    module = call_targets.module_name(path, browser_safety.REPO_ROOT)
    if module == function.rpartition(".")[0]:
        return False
    references = set(
        call_targets.qualified_references(source, module, is_package=path.name == "__init__.py")
    )
    return function in references and (reference is None or reference in references)


def _split_key(key: str) -> tuple[str, str | None]:
    """``"f[r]"`` -> ``("f", "r")``; a plain ``"f"`` -> ``("f", None)``."""
    if key.endswith("]") and "[" in key:
        function, _, reference = key[:-1].partition("[")
        return function, reference
    return key, None


def test_the_unenforced_list_is_what_the_package_actually_shows() -> None:
    """The gap about unwired protections is derived, not a hand-written claim.

    A posture report reads configuration and counters; it cannot see whether
    the code that does the work calls the enforcement. Saying so is only
    honest if the list stays true, and a hand-maintained "not wired up yet"
    list is wrong the week after it is written. So this scans the package: when
    P2-06's enrichment job lands and calls ``budgets.consume``, this fails and
    the gap has to be shortened before the suite is green again.
    """
    unenforced = {name for name in ENFORCED_BY if not _callers_of(name)}

    assert unenforced == set(UNENFORCED_TODAY), (
        "the set of protections with no enforcing caller changed.\n"
        f"  the package shows: {sorted(unenforced)}\n"
        f"  UNENFORCED_TODAY:  {sorted(UNENFORCED_TODAY)}\n"
        "Shorten UNENFORCED_TODAY when an enforcement gains its caller, and"
        " lengthen it if one lost its caller -- which would be a regression"
        " worth stopping for."
    )


def test_the_scanner_can_actually_find_a_caller() -> None:
    """A scan that matched nothing would make the test above pass forever.

    Every protection is unenforced today, so the live scan is shown finding a
    live caller of something else: the CLI calls ``ensure_local_user``.
    """
    package = Path(browser_safety.PACKAGE)
    assert package / "cli.py" in _callers_of("netkeeper.services.users.ensure_local_user")
    assert _callers_of("netkeeper.linkedin.pacing.is_active_at", live_only=False) == {
        package / "services" / "scheduler.py"
    }
    assert not _callers_of("netkeeper.linkedin.pacing.a_function_nobody_wrote", live_only=False)


def test_a_caller_nothing_starts_is_not_live() -> None:
    """The runner and the scheduler call enforcement for real and are still not live.

    Each is reachable only through a report or a rehearsal (``posture`` reads the
    scheduler's constants, ``simulate`` runs a fake schedule), or not at all.
    """
    package = Path(browser_safety.PACKAGE)
    live = _live_modules()
    assert package / "services" / "connections_sync.py" not in live
    assert package / "services" / "scheduler.py" not in live
    assert package / "services" / "users.py" in live  # the CLI and the app both reach it
    assert package / "web" / "api" / "contacts.py" in live  # discovered, not imported
    assert _callers_of("netkeeper.services.heat.raise_heat", live_only=False) == {
        package / "services" / "connections_sync.py"
    }
    assert not _callers_of("netkeeper.services.heat.raise_heat")


# The scanner, shown snippets as if they were a file in the package (#162).
_CALLER = Path(browser_safety.PACKAGE) / "services" / "some_job.py"
_CONSUME = "netkeeper.services.budgets.consume"
_PLAN = "netkeeper.linkedin.pacing.plan_enrichment"


def test_an_unrelated_consume_method_is_not_enforcement() -> None:
    """The dangerous direction: a queue's ``consume`` must not read as the budget wired."""
    assert not _calls("queue.consume()\n", _CALLER, _CONSUME)
    assert not _calls("import queue\nqueue.consume()\n", _CALLER, _CONSUME)
    assert not _calls("def go(stream):\n    stream.consume()\n", _CALLER, _CONSUME)
    assert not _calls("consume()\n", _CALLER, _CONSUME)  # unbound: not ours to claim


def test_an_aliased_call_is_enforcement() -> None:
    source = (
        "from netkeeper.services.budgets import consume as spend\n"
        "def run(session):\n"
        "    spend(session)\n"
    )
    assert _calls(source, _CALLER, _CONSUME)


def test_every_spelling_of_the_real_call_is_enforcement() -> None:
    spellings = (
        "from netkeeper.services.budgets import consume\nconsume()\n",
        "from netkeeper.services import budgets\nbudgets.consume()\n",
        "import netkeeper.services.budgets as b\nb.consume()\n",
        "import netkeeper.services.budgets\nnetkeeper.services.budgets.consume()\n",
        "from . import budgets\nbudgets.consume()\n",
        "from .budgets import consume\nconsume()\n",
        "def run():\n    from netkeeper.services import budgets\n    budgets.consume()\n",
    )
    for source in spellings:
        assert _calls(source, _CALLER, _CONSUME), source


def test_a_file_with_its_own_consume_still_counts_its_real_call() -> None:
    """Used to be skipped outright on the text ``def consume(``."""
    source = (
        "from netkeeper.services import budgets\n"
        "def consume(message):\n"
        "    return message\n"
        "def run(session):\n"
        "    budgets.consume(session)\n"
    )
    assert _calls(source, _CALLER, _CONSUME)


def test_a_local_consume_is_not_the_real_one() -> None:
    """The bare name resolves to whatever it is bound to last, as Python would."""
    shadowed = (
        "from netkeeper.services.budgets import consume\n"
        "def consume(message):\n"
        "    return message\n"
        "consume(1)\n"
    )
    local_variable = (
        "from netkeeper.services.budgets import consume\ndef run(consume):\n    consume()\n"
    )
    assert not _calls(shadowed, _CALLER, _CONSUME)
    assert not _calls(local_variable, _CALLER, _CONSUME)


def test_the_defining_module_is_not_its_own_caller() -> None:
    budgets = Path(browser_safety.PACKAGE) / "services" / "budgets.py"
    assert not _calls("def consume():\n    consume()\n", budgets, _CONSUME)


def test_a_passed_reference_is_enforcement() -> None:
    """Handler injection: the function handed over, not called here."""
    spellings = (
        "import functools\nfrom netkeeper.services import budgets\n"
        "spend = functools.partial(budgets.consume, action=1)\n",
        "from netkeeper.services.budgets import consume\n"
        "def build(runner):\n    runner.register(on_visit=consume)\n",
        "from netkeeper.services import budgets\nclass Job:\n    charge = budgets.consume\n",
    )
    for source in spellings:
        assert _calls(source, _CALLER, _CONSUME), source


def test_an_import_alone_is_not_enforcement() -> None:
    assert not _calls("from netkeeper.services.budgets import consume\n", _CALLER, _CONSUME)


def test_pacing_is_enforced_by_the_plan_not_by_scrolling() -> None:
    """A job that plans its visits paces them; one that only scrolls pages does
    not, although ``scroll_like_a_person`` calls ``human_delay`` for its dwell."""
    planned = (
        "from netkeeper.linkedin import pacing\n"
        "def run(rng):\n"
        "    return pacing.plan_enrichment(rng, 10)\n"
    )
    scrolled = (
        "from netkeeper.linkedin import pacing\n"
        "async def sync(page, rng):\n"
        "    await pacing.scroll_like_a_person(page, rng)\n"
    )
    assert _calls(planned, _CALLER, _PLAN)
    assert not _calls(scrolled, _CALLER, _PLAN)


def test_human_like_pacing_is_keyed_on_the_plan_alone() -> None:
    """Keyed on a helper (``human_delay``), a scroll-only job would close the gap."""
    keys = {function for function, names in ENFORCED_BY.items() if "human-like pacing" in names}
    assert keys == {"netkeeper.linkedin.pacing.plan_enrichment"}


def test_the_rehearsal_is_ignored_because_it_does_pace() -> None:
    """The ``rehearse.py`` ignore entry is load-bearing: without it, the rehearsal's
    ``plan_enrichment`` would mark human-like pacing as enforced by a live job."""
    rehearse = Path(browser_safety.PACKAGE) / "linkedin" / "rehearse.py"
    assert _calls(rehearse.read_text(encoding="utf-8"), rehearse, _PLAN)
    assert rehearse not in _callers_of(_PLAN)


def test_every_enforcement_target_is_a_function_that_exists() -> None:
    """A misspelled key would read as "nothing calls it" forever."""
    for key in ENFORCED_BY:
        function, reference = _split_key(key)
        module, _, name = function.rpartition(".")
        assert callable(getattr(importlib.import_module(module), name, None)), key
        if reference is not None:
            assert _resolves(reference), key


def _resolves(dotted: str) -> bool:
    """Whether ``a.b.C.D`` names something: the longest importable prefix, then attributes."""
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            target: object = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for attribute in parts[split:]:
            if not hasattr(target, attribute):
                return False
            target = getattr(target, attribute)
        return True
    return False


def test_every_unenforced_name_maps_to_protections_the_report_has(
    writer: Session, user: User
) -> None:
    """The gap names rows a reader can find in the table above it."""
    names = {protection.name for protection in _report(writer, user).protections}

    for function, protections in ENFORCED_BY.items():
        assert set(protections) <= names, f"{function} names a protection the report lacks"


def test_a_clean_report_claims_configuration_and_not_enforcement(
    writer: Session, user: User
) -> None:
    """The verdict is the one sentence most likely to be read on its own.

    "every protection is in force" would be the same sentence on the day a
    protection works and the day nothing calls it, which for five of them is
    today. "nothing is misconfigured" is what this module actually checks.
    """
    text = render(_report(writer, user))

    assert "nothing is misconfigured" in text
    assert "in force" not in text.split("nothing is misconfigured")[1]
    assert "never callers" in text
    for name in _report(writer, user).protections:
        if name.name in ("budget profile_visits", "warm-up ramp", "weekend damping"):
            assert name.name in text.split("not covered by this report:")[1]


def test_the_gap_lists_the_protections_nothing_enforces_yet(writer: Session, user: User) -> None:
    gaps = " ".join(_report(writer, user).gaps)

    # The sentence after the colon, up to its period. (This used to split on
    # "never callers", whose next character is the period ending that bold
    # phrase, so every "not in" below it compared against an empty string.)
    unwired = gaps.split("no enforcing caller that netkeeper runs yet:")[1].split(".")[0]
    assert unwired.strip()

    # Nothing netkeeper runs reaches the scheduler or the connections sync runner
    # yet, so every protection either of them would enforce is listed.
    for name in (
        "budget connection_pages",
        "budget profile_visits",
        "warm-up ramp",
        "active hours",
        "heat skip gate",
        "session flag",
        "heat",
    ):
        assert name in unwired, name
    assert "connections sync runner" in gaps


_CONNECTION_PAGES = f"{_CONSUME}[netkeeper.services.budgets.ActionClass.CONNECTION_PAGES]"
_PROFILE_VISITS = f"{_CONSUME}[netkeeper.services.budgets.ActionClass.PROFILE_VISITS]"


def test_a_budget_is_enforced_per_action_class() -> None:
    """One ``consume`` caller wires the classes it names and no others.

    Keyed on ``consume`` alone, the connections sync spending connection pages
    read as every budget enforced, profile visits included, while nothing
    spent one.
    """
    source = (
        "from netkeeper.services import budgets\n"
        "from netkeeper.services.budgets import ActionClass\n"
        "def page(session):\n"
        "    budgets.consume(session, ActionClass.CONNECTION_PAGES)\n"
    )
    assert _calls(source, _CALLER, _CONNECTION_PAGES)
    assert not _calls(source, _CALLER, _PROFILE_VISITS)


def test_a_named_action_class_without_consume_is_not_enforcement() -> None:
    source = (
        "from netkeeper.services import budgets\n"
        "def page(session):\n"
        "    budgets.status(session, budgets.ActionClass.CONNECTION_PAGES)\n"
    )
    assert not _calls(source, _CALLER, _CONNECTION_PAGES)


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
    assert MAX_PLAUSIBLE_HOLD_HOURS == 4.0


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


def test_the_defaults_this_report_calls_clean_are_appendix_c_s() -> None:
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
    assert DEFAULTS.linkedin.budget.connection_pages_per_day == 150
    assert DEFAULTS.linkedin.budget.inbox_polls_per_day == 8
    assert DEFAULTS.linkedin.budget.li_messages_auto_per_day == 15
    assert DEFAULTS.linkedin.heat.per_block == 1.0
    assert DEFAULTS.linkedin.heat.half_life_hours == 6
    assert DEFAULTS.linkedin.heat.skip_threshold == 2.5
    assert DEFAULTS.campaigns.linkedin_auto_send is False
    # Appendix C's pacing rows, which `_pacing` is the report's row for.
    assert DEFAULTS.linkedin.pacing.profile_delay_median_s == 25
    assert DEFAULTS.linkedin.pacing.profile_delay_sigma == 0.6
    assert DEFAULTS.linkedin.pacing.distraction_p == 0.08
    assert DEFAULTS.linkedin.pacing.distraction_range_s == (120, 480)
    assert DEFAULTS.linkedin.pacing.burst_size == (8, 15)
    assert DEFAULTS.linkedin.pacing.burst_break_s == (300, 1200)


# --- the numbers the report carries ---------------------------------------------


def test_now_is_a_parameter_not_a_clock_read(writer: Session, user: User) -> None:
    """Two instants, one database, two different answers. A ``datetime.now()`` inside
    would make these agree."""
    inside = _row(_report(writer, user, now=NOW), "active hours")
    outside = _row(_report(writer, user, now=NOW - timedelta(hours=10)), "active hours")

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


def test_one_throttle_two_days_ago_leaves_todays_budget_whole(writer: Session, user: User) -> None:
    """#160 through posture: the residue of a throttle 48 hours back (score 0.0039)
    used to floor a 30 Saturday budget to 29 and keep doing so for about 13 days."""
    heat_rows.raise_heat(
        writer, user, ACCOUNT, now=SATURDAY - timedelta(hours=48), settings=DEFAULTS.linkedin.heat
    )

    today = _report(writer, user, now=SATURDAY).today

    assert today.after_weekend == 30
    assert today.after_heat == 30


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


def test_heat_cleared_by_hand_is_not_reported_as_last_raised(writer: Session, user: User) -> None:
    """A manual clear writes a state exactly as a raise does (spec 9.7's manual clear).

    Reading the stored timestamp without looking at the score reports the
    clear as the last raise: "0.00 of 2.5, last raised five minutes ago",
    which is self-contradictory and sends someone hunting a throttle that did
    not happen. The fixture raises 30 hours ago and clears 5 minutes ago, so
    the two timestamps are far enough apart that the wrong one is unmistakable.
    """
    _clear_heat(writer, user)

    report = _report(writer, user)
    row = next(p for p in report.protections if p.name == "heat")

    assert report.heat.last_raised_at is None
    assert report.heat.cleared_at == NOW - timedelta(minutes=5)
    assert report.heat.score == 0.0
    assert "cleared 2026-09-23 17:55 UTC" in row.value
    assert "last raised" not in row.value
    assert report.ok, "a cleared account is a clean one, not a warning"


def test_a_raised_score_is_still_reported_as_raised(writer: Session, user: User) -> None:
    """Without this, reporting every state as "cleared" would pass the test above."""
    _raise_heat(1)(writer, user)

    report = _report(writer, user)

    assert report.heat.last_raised_at == NOW
    assert report.heat.cleared_at is None
    assert "last raised" in next(p for p in report.protections if p.name == "heat").value


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


def test_the_pacing_row_shows_the_numbers_a_reviewer_would_check(
    writer: Session, user: User
) -> None:
    """CP3 asks "are the defaults in Appendix C what the code does". This is where
    four of its six rows are visible without reading the config file."""
    row = next(p for p in _report(writer, user).protections if p.name == "human-like pacing")

    assert row.value == "25s median (sigma 0.6), 8% distraction, bursts of 8-15 then 300-1200s"
    assert row.status is Status.ON


def test_an_overnight_window_is_allowed_but_not_silent(writer: Session, user: User) -> None:
    """Somebody really may keep those hours, so this warns rather than refusing.

    But spec 9.1 leans on the owner's own browsing as cover traffic, and a
    sidecar busiest while the account is otherwise asleep has none.
    """
    report = _report(writer, user, settings=_linkedin(active_hours=("21:30", "08:30")))
    row = next(p for p in report.protections if p.name == "active hours")

    assert row.status is Status.ON, "an overnight window is a choice, not a disabled protection"
    assert row.warnings
    assert not report.ok


def test_the_rendered_report_names_every_protection(writer: Session, user: User) -> None:
    report = _report(writer, user)
    text = render(report)

    for protection in report.protections:
        assert protection.name in text
    assert "nothing is misconfigured" in text
    assert "not covered by this report:" in text


def test_the_rendered_report_never_reads_as_all_clear_with_something_off(
    writer: Session, user: User
) -> None:
    text = render(_report(writer, user, browser_mode="launch"))

    assert "NOT clear" in text
    assert "second device" in text


def test_the_report_states_the_gaps_it_cannot_see(writer: Session, user: User) -> None:
    """The scheduler is on another branch; saying so is better than a silent omission."""
    gaps = " ".join(_report(writer, user).gaps)

    assert "scheduler" in gaps
    assert "different NETKEEPER_DATA" in gaps, "the file lock's scope is stated"
    assert "Deleting `locks/` or the file while netkeeper holds the browser" in gaps
    assert "only a manual `rm` can cause this" in gaps
    assert posture_module.GAPS  # a report with no gaps listed would pass the two above


def test_the_closed_per_process_gap_is_no_longer_claimed(writer: Session, user: User) -> None:
    """Issue #153 closed it; a report still confessing it would be wrong the other way."""
    gaps = " ".join(_report(writer, user).gaps)

    assert "guards one process" not in gaps
    assert "builds its own" not in gaps


# --- the activity lock ------------------------------------------------------------


def test_a_free_activity_lock_reads_as_free(writer: Session, user: User) -> None:
    row = _row(_report(writer, user), "one browser client")

    assert row.status is Status.ON
    assert row.value.startswith("free")
    assert not row.warnings


def test_a_held_activity_lock_names_its_holder(writer: Session, user: User) -> None:
    """While `netkeeper serve` runs a job, posture says so -- by pid, from the lock itself."""
    claim = activity_lock.try_claim(activity_lock.SINGLE_ACCOUNT_KEY)
    assert claim is not None
    try:
        row = _row(_report(writer, user), "one browser client")
    finally:
        claim.release()

    assert row.status is Status.ON, "held is the lock working, not a protection off"
    assert row.value.startswith("held by")
    assert f"pid {os.getpid()}" in row.value
    assert _row(_report(writer, user), "one browser client").value.startswith("free")


def test_an_unreadable_activity_lock_warns(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(account: str, directory: object = None) -> activity_lock.LockState:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(activity_lock, "inspect", unreadable)
    report = _report(writer, user)
    row = _row(report, "one browser client")

    assert row.status is Status.UNKNOWN
    assert row.warnings
    assert not report.ok
