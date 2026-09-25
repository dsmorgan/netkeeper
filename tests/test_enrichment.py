"""netkeeper.services.enrichment: a whole enrichment run against a fake tab.

P2-07's "done when", end to end: a run visits the planned contacts in spec
9.6's order and writes their harvests; cancel stops it between profiles; a
resume reuses the stored plan, skips what completed, and never re-plans. And
the properties that keep the account safe: never more visits than today's
warm-up-ramped, weekend-damped, heat-shrunk allowance; nothing at all while the
session is flagged or heat is over the skip threshold; the budget spent before
the navigation, never after.

The tab is :class:`profile_fakes.FakeBrowser`: invented people, served from
memory, every navigation recorded. No socket, no browser, no real sleeping.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import factories
import pytest
from profile_fakes import (
    BAD_REQUEST,
    CHECKPOINT,
    LOGGED_OUT,
    NOT_FOUND,
    PROFILES,
    THROTTLED,
    UNRECOGNIZED,
    FakeBrowser,
    Profile,
    Scripted,
)
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.config import BudgetSettings, LinkedInSettings, Settings
from netkeeper.db import session_scope
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import StopReason
from netkeeper.linkedin.pacing import plan_enrichment
from netkeeper.models import Contact, ContactMet, SyncRun, SyncRunKind, SyncRunTrigger, User
from netkeeper.scoping import get_scoped
from netkeeper.services import budgets, enrich_plan, runs
from netkeeper.services import heat as heat_service
from netkeeper.services.budgets import ActionClass
from netkeeper.services.enrichment import (
    CANCEL_SLICE_S,
    EnrichRunReport,
    HeatSkipped,
    SessionFlagged,
    enrich_contacts,
    resume_enrichment,
    todays_visits,
)
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session, session_flag
from netkeeper.services.pacing import profiles
from netkeeper.services.posture import _todays_budget

#: A Wednesday, 11:00 in New York: inside the active window, not a weekend.
NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)
ESTABLISHED = NOW - timedelta(days=90)  # far past the warm-up ramp
SMALL = LinkedInSettings(budget=BudgetSettings(profile_visits_per_day=10, warmup_start=4))
SEED = 11


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def _people(count: int) -> list[Profile]:
    """``count`` invented people: the five named ones, then numbered ones."""
    extra = [
        Profile(300 + i, f"Given{i}", f"Family{i}", headline=f"Role {i} at Invented Firm {i}")
        for i in range(max(count - len(PROFILES), 0))
    ]
    return [*PROFILES, *extra][:count]


def _setup(
    factory: sessionmaker[Session],
    people: list[Profile],
    *,
    created_at: datetime = ESTABLISHED,
) -> tuple[int, dict[int, int]]:
    """A user whose contacts are ``people``, as a sync left them, newest connection first.

    Returns the user id and each person's contact id by ``Profile.n``.
    """
    with session_scope(factory, write=True) as session:
        user = factories.make_user(session, created_at=created_at)
        ids: dict[int, int] = {}
        for rank, person in enumerate(people):
            contact = factories.make_contact(
                session,
                user,
                li_urn=person.urn,
                li_public_id=person.slug,
                first_name=person.first,
                last_name=person.last,
                headline=None,
                current_title=None,
                current_company=None,
                connected_on=date(2026, 9, 1) - timedelta(days=rank),
            )
            ids[person.n] = contact.id
        return user.id, ids


async def _enrich(
    factory: sessionmaker[Session],
    user_id: int,
    browser: FakeBrowser,
    *,
    settings: LinkedInSettings = SMALL,
    clock: Callable[[], datetime] | None = None,
    sleeps: Sleeps | None = None,
    resume_of: int | None = None,
) -> EnrichRunReport:
    sleep = sleeps or Sleeps()
    kwargs = {
        "settings": settings,
        "clock": clock or Clock(),
        "sleep": sleep,
        "rng": random.Random(SEED),
    }
    source = browser.source(sleep=sleep)
    if resume_of is None:
        return await enrich_contacts(factory, user_id, source, **kwargs)  # type: ignore[arg-type]
    return await resume_enrichment(factory, user_id, resume_of, source, **kwargs)  # type: ignore[arg-type]


def _read[T](factory: sessionmaker[Session], user_id: int, read: Callable[[Session, User], T]) -> T:
    with session_scope(factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        return read(session, user)


def _spent(factory: sessionmaker[Session], user_id: int, at: datetime = NOW) -> int:
    def read(session: Session, user: User) -> int:
        account = ensure_account(session, user).id
        return budgets.status(
            session, user, account, ActionClass.PROFILE_VISITS, now=at, settings=SMALL.budget
        ).day.count

    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        return read(session, user)


def _contact(factory: sessionmaker[Session], user_id: int, contact_id: int) -> Contact:
    def read(session: Session, user: User) -> Contact:
        contact = get_scoped(session, user, Contact, contact_id)
        assert contact is not None
        session.expunge(contact)
        return contact

    return _read(factory, user_id, read)


def _plan(factory: sessionmaker[Session], user_id: int, report: EnrichRunReport):  # type: ignore[no-untyped-def]
    return _read(
        factory,
        user_id,
        lambda s, u: enrich_plan.load_plan(s, u, report.run_id),
    )


def _last_run(factory: sessionmaker[Session], user_id: int) -> SyncRun:
    """The newest enrichment run, detached: how a run that raised is looked at."""

    def read(session: Session, user: User) -> SyncRun:
        run = runs.latest_run(session, user, SyncRunKind.ENRICH)
        assert run is not None
        session.expunge(run)
        return run

    return _read(factory, user_id, read)


def _stop_reason(factory: sessionmaker[Session], user_id: int, run_id: int) -> str | None:
    return _read(factory, user_id, lambda s, u: runs.get_run(s, u, run_id).stop_reason)


# --- done when: a run visits in order and writes what it harvested ---------------------------


async def test_a_run_visits_in_priority_order_and_writes_every_harvest(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(5)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    report = await _enrich(session_factory, user_id, browser)

    assert browser.visited() == [p.slug for p in people]  # newest connection first
    assert report.result.reason is StopReason.END_OF_PLAN
    assert report.harvests.applied == 5
    priya = _contact(session_factory, user_id, ids[101])
    assert priya.headline == PROFILES[0].headline and priya.last_enriched_at == NOW
    plan = _plan(session_factory, user_id, report)
    assert plan.status == "completed" and plan.completed == tuple(ids[p.n] for p in people)
    assert _spent(session_factory, user_id) == 5


async def test_the_budget_is_spent_before_each_navigation(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(3)
    user_id, _ = _setup(session_factory, people)
    spent_at_goto: list[int] = []
    browser = FakeBrowser.of(people)

    def on_event(kind: str, value: object) -> None:
        if kind == "goto":
            spent_at_goto.append(_spent(session_factory, user_id))

    browser.on_event = on_event
    await _enrich(session_factory, user_id, browser)

    assert spent_at_goto == [1, 2, 3]


async def test_the_pinned_contact_goes_first_and_is_unpinned_once_visited(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        enrich_plan.pin(session, user, account, ids[104])
    browser = FakeBrowser.of(people)

    await _enrich(session_factory, user_id, browser)

    assert browser.visited()[0] == people[3].slug
    assert _read(session_factory, user_id, lambda s, u: enrich_plan.pinned(s, u, account)) == []


# --- today's visits: warm-up, weekend, heat, and what is already spent -----------------------


@pytest.mark.parametrize(
    ("created_at", "at", "expected"),
    [
        pytest.param(NOW, NOW, 4, id="install-day"),
        pytest.param(NOW - timedelta(days=2), NOW, 8, id="day-two"),
        pytest.param(ESTABLISHED, NOW, 10, id="ramped"),
        pytest.param(ESTABLISHED, SATURDAY, 5, id="saturday"),
        pytest.param(NOW, SATURDAY, 5, id="day-three-on-saturday"),
    ],
)
async def test_the_warm_up_and_the_weekend_set_how_many_are_visited(
    session_factory: sessionmaker[Session], created_at: datetime, at: datetime, expected: int
) -> None:
    """Spec 9.5 with a cap of 10: 4 on install day, +2 a day, halved on Saturday."""
    settings = replace(SMALL, budget=replace(SMALL.budget, warmup_step=2))
    people = _people(12)
    user_id, _ = _setup(session_factory, people, created_at=created_at)
    browser = FakeBrowser.of(people)

    report = await _enrich(session_factory, user_id, browser, settings=settings, clock=Clock(at))

    assert len(browser.visited()) == expected
    assert report.visits.remaining == expected
    assert _spent(session_factory, user_id, at) == expected


async def test_warm_heat_shrinks_the_run_and_stretches_the_waits(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(12)
    user_id, _ = _setup(session_factory, people)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        heat_service.raise_heat(session, user, account, now=NOW, settings=SMALL.heat)
    browser = FakeBrowser.of(people)
    sleeps = Sleeps()

    report = await _enrich(session_factory, user_id, browser, sleeps=sleeps)

    assert len(browser.visited()) == 5  # 10 / (1 + 1.0)
    configured = profiles(SMALL.pacing)
    warm = replace(configured.delay, median=configured.delay.median * 2.0)
    expected = plan_enrichment(random.Random(SEED), 5, delay=warm, burst=configured.burst)
    assert report.result.plan == expected
    gaps = sum(g or 0.0 for g in report.result.click_pauses_s)  # between each visit's fetches
    assert sum(sleeps.waits) == pytest.approx(expected.total_delay_s + gaps)


async def test_visits_already_spent_today_come_off_the_run(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(12)
    user_id, _ = _setup(session_factory, people)
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        for _ in range(7):
            budgets.consume(
                session, user, account, ActionClass.PROFILE_VISITS, now=NOW, settings=SMALL.budget
            )
    browser = FakeBrowser.of(people)

    await _enrich(session_factory, user_id, browser)

    assert len(browser.visited()) == 3
    assert _spent(session_factory, user_id) == 10


async def test_the_week_ceiling_caps_the_run(session_factory: sessionmaker[Session]) -> None:
    settings = replace(SMALL, budget=replace(SMALL.budget, profile_visits_per_week=3))
    people = _people(12)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    report = await _enrich(session_factory, user_id, browser, settings=settings)

    assert len(browser.visited()) == 3 and report.visits.week_left == 3


async def test_budget_spent_elsewhere_mid_run_stops_the_next_visit(
    session_factory: sessionmaker[Session],
) -> None:
    """Another process spends the day between two visits: the gate refuses the next one."""
    people = _people(6)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)
    fired = False

    def spend_the_rest(kind: str, value: object) -> None:
        nonlocal fired
        if kind == "click" and not fired:
            fired = True
            with session_scope(session_factory, write=True) as session:
                user = session.get(User, user_id)
                assert user is not None
                account = ensure_account(session, user).id
                for _ in range(10):
                    budgets.consume(
                        session,
                        user,
                        account,
                        ActionClass.PROFILE_VISITS,
                        now=NOW,
                        settings=SMALL.budget,
                    )

    browser.on_event = spend_the_rest
    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.BUDGET
    assert len(browser.visited()) == 1


@pytest.mark.parametrize("days", [0, 1, 3, 9])
@pytest.mark.parametrize("at", [NOW, SATURDAY], ids=["weekday", "saturday"])
@pytest.mark.parametrize("multiplier", [1.0, 1.7, 3.2])
def test_todays_visits_is_what_posture_reports(
    session_factory: sessionmaker[Session], days: int, at: datetime, multiplier: float
) -> None:
    """One chain, two readers: the run spends exactly what ``netkeeper posture`` says it may."""
    settings = Settings(linkedin=SMALL)
    with session_scope(session_factory, write=True) as session:
        user = factories.make_user(session, created_at=at - timedelta(days=days))
        ours = todays_visits(session, user, 1, now=at, settings=SMALL, multiplier=multiplier)
        local = at.astimezone(ZoneInfo(SMALL.timezone))
        theirs = _todays_budget(
            user, settings=settings, local_now=local, multiplier=multiplier, spent=0
        )
    assert (ours.ramp, ours.after_weekend, ours.after_heat) == (
        theirs.ramp,
        theirs.after_weekend,
        theirs.after_heat,
    )


# --- nothing while the session is flagged, heat is over, or the window is shut ---------------


@pytest.mark.parametrize("outcome", [Outcome.CHECKPOINT, Outcome.LOGGED_OUT])
async def test_no_run_starts_while_the_session_is_flagged(
    session_factory: sessionmaker[Session], outcome: Outcome
) -> None:
    user_id, _ = _setup(session_factory, _people(3))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        flag_session(session, user, outcome, url="https://www.linkedin.com/checkpoint/x")
    browser = FakeBrowser.of(_people(3))

    with pytest.raises(SessionFlagged):
        await _enrich(session_factory, user_id, browser)

    assert browser.events == [] and _spent(session_factory, user_id) == 0


async def test_heat_over_the_skip_threshold_stops_the_run_before_anything(
    session_factory: sessionmaker[Session],
) -> None:
    user_id, _ = _setup(session_factory, _people(3))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        for _ in range(3):
            heat_service.raise_heat(session, user, account, now=NOW, settings=SMALL.heat)
    browser = FakeBrowser.of(_people(3))

    with pytest.raises(HeatSkipped):
        await _enrich(session_factory, user_id, browser)

    assert browser.events == [] and _spent(session_factory, user_id) == 0


async def test_outside_the_active_window_nothing_is_visited_or_spent(
    session_factory: sessionmaker[Session],
) -> None:
    user_id, _ = _setup(session_factory, _people(3))
    browser = FakeBrowser.of(_people(3))
    three_am = datetime(2026, 9, 23, 7, 0, tzinfo=UTC)  # 03:00 in New York

    report = await _enrich(session_factory, user_id, browser, clock=Clock(three_am))

    assert report.result.reason is StopReason.INACTIVE
    assert browser.events == [] and _spent(session_factory, user_id, three_am) == 0


async def test_a_run_that_outlasts_the_window_stops_at_its_edge(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(5)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)
    clock = Clock(datetime(2026, 9, 24, 1, 20, tzinfo=UTC))  # 21:20 in New York

    def time_passes(kind: str, value: object) -> None:
        if kind == "goto":
            clock.at += timedelta(minutes=6)

    browser.on_event = time_passes
    report = await _enrich(session_factory, user_id, browser, clock=clock)

    assert report.result.reason is StopReason.INACTIVE
    assert len(browser.visited()) == 2  # 21:20 and 21:26; 21:32 is past 21:30


# --- spec 9.7: what the stopping response does --------------------------------------------


@pytest.mark.parametrize(
    ("scripted", "heat", "flag"),
    [
        pytest.param(THROTTLED, True, False, id="throttled"),
        pytest.param(CHECKPOINT, True, True, id="checkpoint"),
        pytest.param(LOGGED_OUT, False, True, id="logged-out"),
        pytest.param(BAD_REQUEST, False, False, id="route-changed"),
    ],
)
async def test_the_stopping_response_raises_heat_and_the_flag_as_spec_9_7_says(
    session_factory: sessionmaker[Session], scripted: Scripted, heat: bool, flag: bool
) -> None:
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, script={2: scripted})

    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.RESPONSE
    assert (report.heat_raised, report.session_flagged) == (heat, flag)
    flagged = _read(session_factory, user_id, session_flag)
    assert (flagged is not None) is flag
    if flagged is not None:
        assert "fake" not in flagged.url  # the slug never reaches the flag
    plan = _plan(session_factory, user_id, report)
    assert (plan.status, plan.completed) == ("aborted", (ids[101],))
    assert _contact(session_factory, user_id, ids[102]).last_enriched_at is None


async def test_a_profile_not_found_is_recorded_and_the_run_goes_on(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(3)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, script={2: NOT_FOUND})

    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.END_OF_PLAN
    assert (report.harvests.applied, report.harvests.not_found) == (2, 1)
    mateo = _contact(session_factory, user_id, ids[102])
    assert (mateo.li_not_found_count, mateo.li_not_found_at) == (1, NOW)
    assert mateo.last_enriched_at is None


# --- cancel and resume (spec 9.9) --------------------------------------------------------------


def _cancel_after(
    factory: sessionmaker[Session], user_id: int, visits: int
) -> Callable[[str, object], None]:
    """An ``on_event`` that asks the running plan to stop once ``visits`` clicks are made."""
    seen = 0

    def on_event(kind: str, value: object) -> None:
        nonlocal seen
        if kind != "click":
            return
        seen += 1
        if seen != visits:
            return
        with session_scope(factory, write=True) as session:
            user = session.get(User, user_id)
            assert user is not None
            running = runs.running_run(session, user, ensure_account(session, user).id)
            assert running is not None
            runs.request_cancel(session, user, running.id, now=NOW)

    return on_event


async def test_cancel_stops_between_profiles_and_keeps_what_completed(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(5)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, on_event=_cancel_after(session_factory, user_id, 2))
    sleeps = Sleeps()

    report = await _enrich(session_factory, user_id, browser, sleeps=sleeps)

    assert report.result.reason is StopReason.CANCELLED
    assert browser.visited() == [p.slug for p in people[:2]]
    # The whole wait after the first profile, then one slice of the next and the check.
    steps = report.result.plan.steps
    gaps = sum(g or 0.0 for g in report.result.click_pauses_s)
    assert sum(sleeps.waits[:-1]) == pytest.approx((steps[0].delay_after_s or 0.0) + gaps)
    assert sleeps.waits[-1] == min(CANCEL_SLICE_S, steps[1].delay_after_s or 0.0)
    plan = _plan(session_factory, user_id, report)
    assert plan.status == "aborted"
    assert _stop_reason(session_factory, user_id, report.run_id) == "cancelled"
    assert plan.completed == (ids[101], ids[102])
    assert _contact(session_factory, user_id, ids[102]).headline == PROFILES[1].headline
    assert _spent(session_factory, user_id) == 2


async def test_resume_skips_what_completed_and_never_re_plans(
    session_factory: sessionmaker[Session],
) -> None:
    """Done when: resume skips completed contacts. Between the two runs a newer
    connection arrives and another contact is pinned; neither joins the plan."""
    people = _people(5)
    user_id, ids = _setup(session_factory, people)
    first = await _enrich(
        session_factory,
        user_id,
        FakeBrowser.of(people, on_event=_cancel_after(session_factory, user_id, 2)),
    )
    newcomer = Profile(400, "Newest", "Person")
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        late = factories.make_contact(
            session,
            user,
            li_urn=newcomer.urn,
            li_public_id=newcomer.slug,
            connected_on=date(2026, 9, 22),
            met=ContactMet.MET,
        )
        enrich_plan.pin(session, user, first.account_id, late.id)
    browser = FakeBrowser.of([*people, newcomer])

    second = await _enrich(
        session_factory,
        user_id,
        browser,
        resume_of=first.run_id,
        clock=Clock(NOW + timedelta(hours=1)),
    )

    assert browser.visited() == [p.slug for p in people[2:]]
    assert second.run_id != first.run_id
    assert second.result.reason is StopReason.END_OF_PLAN
    plan = _plan(session_factory, user_id, second)
    assert plan.status == "completed"
    assert plan.contact_ids == plan.completed == tuple(ids[p.n] for p in people[2:])
    with pytest.raises(enrich_plan.PlanFinished):
        await _enrich(session_factory, user_id, browser, resume_of=first.run_id)
    with pytest.raises(enrich_plan.PlanFinished):
        await _enrich(session_factory, user_id, browser, resume_of=second.run_id)


async def test_a_resume_spends_only_todays_budget_and_can_be_resumed_again(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(6)
    user_id, _ = _setup(session_factory, people)
    first = await _enrich(
        session_factory,
        user_id,
        FakeBrowser.of(people, on_event=_cancel_after(session_factory, user_id, 1)),
    )
    tomorrow = Clock(NOW + timedelta(days=1))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        for _ in range(7):
            budgets.consume(
                session,
                user,
                first.account_id,
                ActionClass.PROFILE_VISITS,
                now=tomorrow.at,
                settings=SMALL.budget,
            )
    browser = FakeBrowser.of(people)

    second = await _enrich(
        session_factory, user_id, browser, resume_of=first.run_id, clock=tomorrow
    )

    assert browser.visited() == [p.slug for p in people[1:4]]
    assert second.result.reason is StopReason.VISIT_BUDGET
    assert _plan(session_factory, user_id, second).status == "aborted"
    third_browser = FakeBrowser.of(people)
    third = await _enrich(
        session_factory,
        user_id,
        third_browser,
        resume_of=second.run_id,
        clock=Clock(NOW + timedelta(days=2)),
    )
    assert third_browser.visited() == [p.slug for p in people[4:]]
    assert third.result.reason is StopReason.END_OF_PLAN


async def test_a_run_that_dies_is_marked_aborted_and_resumes_where_it_stopped(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    def tab_dies(kind: str, value: object) -> None:
        if kind == "goto" and len(browser.visited()) == 3:
            raise RuntimeError("the browser went away")

    browser.on_event = tab_dies
    with pytest.raises(RuntimeError, match="went away"):
        await _enrich(session_factory, user_id, browser)

    died = _last_run(session_factory, user_id)
    plan = _read(session_factory, user_id, lambda s, u: enrich_plan.load_plan(s, u, died.id))
    assert (plan.status, died.stop_reason, len(plan.completed)) == ("failed", "error", 2)
    assert died.error == "RuntimeError: the browser went away"

    again = FakeBrowser.of(people)
    await _enrich(session_factory, user_id, again, resume_of=died.id)
    assert again.visited() == [p.slug for p in people[2:]]


async def test_resuming_an_unknown_plan_is_refused(session_factory: sessionmaker[Session]) -> None:
    user_id, _ = _setup(session_factory, _people(1))
    with pytest.raises(enrich_plan.PlanNotFound):
        await _enrich(session_factory, user_id, FakeBrowser.of([]), resume_of=9999)


async def test_a_resume_leaves_out_who_can_no_longer_be_visited(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, ids = _setup(session_factory, people)
    first = await _enrich(
        session_factory,
        user_id,
        FakeBrowser.of(people, on_event=_cancel_after(session_factory, user_id, 1)),
    )
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        archived = get_scoped(session, user, Contact, ids[103])
        assert archived is not None
        archived.archived_at = NOW
    browser = FakeBrowser.of(people)

    second = await _enrich(session_factory, user_id, browser, resume_of=first.run_id)

    assert browser.visited() == [people[1].slug, people[3].slug]
    assert second.skipped == 1


# --- pacing -------------------------------------------------------------------------------------


async def test_the_waits_between_profiles_are_sliced_for_cancel(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    sleeps = Sleeps()

    report = await _enrich(session_factory, user_id, FakeBrowser.of(people), sleeps=sleeps)

    gaps = [g for g in report.result.click_pauses_s if g is not None]
    pauses = list(sleeps.waits)
    for gap in gaps:  # the pause between two fetches is not a cancel slice
        pauses.remove(gap)
    assert all(0 < wait <= CANCEL_SLICE_S for wait in pauses)
    assert sum(pauses) == pytest.approx(report.result.plan.total_delay_s)
    assert CANCEL_SLICE_S == 5.0


async def test_never_more_navigations_than_budget_units(
    session_factory: sessionmaker[Session],
) -> None:
    """Whatever stops a run, every navigation it made was paid for first."""
    people = _people(12)
    for script in ({}, {4: NOT_FOUND}, {6: THROTTLED}):
        user_id, _ = _setup(session_factory, people)
        browser = FakeBrowser.of(people, script=dict(script))
        report = await _enrich(session_factory, user_id, browser)
        assert len(browser.visited()) == _spent(session_factory, user_id) == report.result.visits
        assert len(browser.visited()) <= 10


async def test_the_gate_checks_the_cancel_flag_before_a_visit_as_well(
    session_factory: sessionmaker[Session],
) -> None:
    """A cancel that lands after the last slice of a wait is still caught before the visit.

    The runner's own waits read the flag between slices, so the gate's check only
    matters for a cancel that arrives in between; asked directly, it refuses and
    spends nothing.
    """
    from netkeeper.services.enrichment import _Gate

    user_id, _ = _setup(session_factory, _people(1))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        run = runs.create_run(
            session, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW
        )
        runs.request_cancel(session, user, run.id, now=NOW)
    gate = _Gate(
        factory=session_factory,
        user_id=user_id,
        account_id=account,
        run_id=run.id,
        settings=SMALL,
        window=(time(8, 30), time(21, 30)),
        clock=Clock(),
        sleep=Sleeps(),
    )

    assert await gate.before_visit(0) is StopReason.CANCELLED
    assert _spent(session_factory, user_id) == 0


async def test_a_broken_observation_aborts_the_plan_and_says_nothing_about_the_session(
    session_factory: sessionmaker[Session],
) -> None:
    """``ObservationFailed`` carries no answer: no heat, no flag, the plan resumable."""
    from netkeeper.linkedin.observe import ObservationFailed

    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    def plumbing_breaks(kind: str, value: object) -> None:
        if kind == "details" and browser.kinds().count("details") == 3:
            raise ObservationFailed("a matching response was dropped")

    browser.on_event = plumbing_breaks
    with pytest.raises(ObservationFailed):
        await _enrich(session_factory, user_id, browser)

    died = _last_run(session_factory, user_id)
    with session_scope(session_factory) as session:
        user = session.get(User, user_id)
        assert user is not None
        account = ensure_account(session, user).id
        plan = enrich_plan.load_plan(session, user, died.id)
        assert session_flag(session, user) is None
        assert heat_service.state(session, user, account) is None
    assert (plan.status, died.stop_reason, len(plan.completed)) == ("failed", "error", 2)


# --- #171 review --------------------------------------------------------------------------------


async def test_a_contact_whose_visit_wrote_nothing_is_not_first_again_the_next_day(
    session_factory: sessionmaker[Session],
) -> None:
    """F1: Priya's slug now leads to someone else. Without an attempt time she would head
    every day's queue and cost a profile visit each time; she waits a week instead."""
    people = _people(3)
    user_id, ids = _setup(session_factory, people)
    somebody_else = replace(people[0], urn_prefix="ACoAANEW")
    day_one = FakeBrowser.of([somebody_else, *people[1:]])
    first = await _enrich(session_factory, user_id, day_one, settings=_one_a_day())
    assert first.harvests.mismatch == 1 and day_one.visited() == [people[0].slug]

    day_two = FakeBrowser.of(people)
    await _enrich(
        session_factory,
        user_id,
        day_two,
        settings=_one_a_day(),
        clock=Clock(NOW + timedelta(days=1)),
    )
    assert day_two.visited() == [people[1].slug]

    week_later = FakeBrowser.of(people)
    await _enrich(
        session_factory,
        user_id,
        week_later,
        settings=_one_a_day(),
        clock=Clock(NOW + timedelta(days=7)),
    )
    assert week_later.visited() == [people[0].slug]
    assert _contact(session_factory, user_id, ids[101]).li_enrich_attempted_at == NOW + timedelta(
        days=7
    )


def _one_a_day() -> LinkedInSettings:
    return replace(SMALL, budget=replace(SMALL.budget, profile_visits_per_day=1, warmup_start=1))


async def test_an_unreadable_profile_is_recorded_and_the_run_goes_on(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(3)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, script={2: UNRECOGNIZED})

    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.END_OF_PLAN
    assert (report.harvests.applied, report.harvests.unreadable) == (2, 1)
    mateo = _contact(session_factory, user_id, ids[102])
    assert (mateo.last_enriched_at, mateo.li_enrich_attempted_at) == (None, NOW)
    assert _plan(session_factory, user_id, report).status == "completed"


async def test_two_unreadable_profiles_in_a_row_abort_without_heat_or_flag(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people, script={2: UNRECOGNIZED, 3: UNRECOGNIZED})

    report = await _enrich(session_factory, user_id, browser)

    assert report.result.reason is StopReason.RESPONSE
    assert report.result.outcome is Outcome.ROUTE_CHANGED
    assert (report.heat_raised, report.session_flagged) == (False, False)
    plan = _plan(session_factory, user_id, report)
    assert plan.status == "aborted" and len(plan.completed) == 3


async def test_a_harvest_and_its_completion_mark_commit_together(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M19: if marking the contact done fails, the harvest it was written with rolls back."""
    people = _people(2)
    user_id, ids = _setup(session_factory, people)

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the plan record could not be written")

    monkeypatch.setattr(enrich_plan, "mark_completed", refuse)
    with pytest.raises(RuntimeError, match="plan record"):
        await _enrich(session_factory, user_id, FakeBrowser.of(people))

    priya = _contact(session_factory, user_id, ids[101])
    assert priya.headline is None and priya.last_enriched_at is None


async def test_a_harvest_that_fails_to_write_is_not_marked_done(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M19, the other way round: no harvest written, no completion recorded."""
    from netkeeper.crm import apply as mapping

    people = _people(2)
    user_id, _ = _setup(session_factory, people)

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the harvest could not be written")

    monkeypatch.setattr(mapping, "apply_harvest", broken)
    with pytest.raises(RuntimeError, match="harvest could not"):
        await _enrich(session_factory, user_id, FakeBrowser.of(people))

    died = _last_run(session_factory, user_id)
    plan = _read(session_factory, user_id, lambda s, u: enrich_plan.load_plan(s, u, died.id))
    assert (plan.completed, plan.status) == ((), "failed")


# --- P2-10: a run's own cap, and what the run row records --------------------------------------


async def _enrich_capped(
    factory: sessionmaker[Session], user_id: int, browser: FakeBrowser, max_visits: int
) -> EnrichRunReport:
    with session_scope(factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        run_id = runs.create_run(
            session,
            user,
            SyncRunKind.ENRICH,
            trigger=SyncRunTrigger.MANUAL,
            now=NOW,
            max_visits=max_visits,
        ).id
    return await enrich_contacts(
        factory,
        user_id,
        browser.source(),
        settings=SMALL,
        run_id=run_id,
        clock=Clock(),
        sleep=Sleeps(),
        rng=random.Random(SEED),
    )


async def test_max_visits_lowers_the_budget(session_factory: sessionmaker[Session]) -> None:
    """CP4's supervised first enrichment: `--max-visits 5` visits at most five."""
    people = _people(12)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    report = await _enrich_capped(session_factory, user_id, browser, max_visits=3)

    assert report.visit_budget == 3 and report.visits.remaining == 10
    assert browser.visited() == [p.slug for p in people[:3]]
    assert _spent(session_factory, user_id) == 3
    plan = _plan(session_factory, user_id, report)
    assert len(plan.contact_ids) == 3  # the plan itself is cut at the cap


async def test_max_visits_never_raises_the_budget(session_factory: sessionmaker[Session]) -> None:
    people = _people(12)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)

    report = await _enrich_capped(session_factory, user_id, browser, max_visits=50)

    assert report.visit_budget == report.visits.remaining == 10
    assert len(browser.visited()) == 10 == _spent(session_factory, user_id)


async def test_the_run_row_records_how_the_run_ended(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(3)
    user_id, _ = _setup(session_factory, people)

    report = await _enrich(session_factory, user_id, FakeBrowser.of(people))

    run = _read(session_factory, user_id, lambda s, u: runs.get_run(s, u, report.run_id))
    assert (run.status, run.stop_reason, run.trigger) == ("completed", "end_of_plan", "manual")
    assert run.counts_json is not None
    assert run.counts_json["harvests"]["applied"] == 3 and run.counts_json["visits"] == 3
    assert run.progress_json is not None and run.progress_json["visited"] == 3
    assert run.completed_at == NOW


async def test_a_refused_run_is_recorded_failed_with_its_reason(
    session_factory: sessionmaker[Session],
) -> None:
    user_id, _ = _setup(session_factory, _people(2))
    with session_scope(session_factory, write=True) as session:
        user = session.get(User, user_id)
        assert user is not None
        flag_session(session, user, Outcome.CHECKPOINT, url="/checkpoint/challenge")
    browser = FakeBrowser.of(_people(2))

    with pytest.raises(SessionFlagged):
        await _enrich(session_factory, user_id, browser)

    run = _last_run(session_factory, user_id)
    assert (run.status, run.stop_reason) == ("failed", "session_flagged")
    assert run.plan_json is None and browser.visited() == []


# --- #190: the page's own answers, and the one click ----------------------------------------------


async def test_the_runner_hands_the_job_each_contacts_urn(
    session_factory: sessionmaker[Session],
) -> None:
    """A slug that now belongs to somebody else: the job sees another id and does not
    click; the core records the mismatch and writes nothing."""
    people = _people(3)
    user_id, ids = _setup(session_factory, people)
    browser = FakeBrowser.of(people, urns={people[1].slug: "urn:li:fsd_profile:ACoAAFAKE9999999"})
    report = await _enrich(session_factory, user_id, browser)
    assert browser.clicks == [people[0].slug, people[2].slug]
    assert report.harvests.mismatch == 1 and report.harvests.applied == 2
    stranger = _contact(session_factory, user_id, ids[people[1].n])
    assert stranger.last_enriched_at is None and stranger.headline is None
    assert stranger.li_enrich_attempted_at is not None  # the attempt still counts


async def test_one_budgeted_unit_covers_the_page_load_the_scroll_and_the_click(
    session_factory: sessionmaker[Session],
) -> None:
    people = _people(4)
    user_id, _ = _setup(session_factory, people)
    browser = FakeBrowser.of(people)
    report = await _enrich(session_factory, user_id, browser)
    assert _spent(session_factory, user_id) == report.result.visits == 4
    assert len(browser.clicks) == report.result.clicks == 4
    assert "contact_info_fetches" not in report.counts()


async def test_a_visit_read_from_the_page_is_written_and_never_takes_a_value_away(
    session_factory: sessionmaker[Session],
) -> None:
    """End to end through the real source (#190's done-when): the page's profile and
    overlay become the contact's fields, and a later visit that reads less -- no
    location, no email -- takes nothing away."""
    from flagship_pages import Role, Website, contact_info_payload, profile_payload
    from profile_site import ProfilePage, ProfileSite
    from run_fakes import fake_provider
    from voyager_pages import PEOPLE as CAST

    from netkeeper.linkedin.page_profiles import PageProfiles

    person = CAST[0]
    user_id, ids = _setup(
        session_factory,
        [Profile(person.n, person.first, person.last, public_id=person.slug)],
    )
    full = ProfilePage(
        person,
        screen=profile_payload(
            person,
            location="Faketown, State of Example",
            roles=[
                Role("Staff Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present")
            ],
        ),
        overlay=contact_info_payload(
            person,
            emails=["priya.fake@example.test"],
            websites=[Website("https://priya-fake.example.test")],
        ),
    )
    sparse = ProfilePage(
        person,
        screen=profile_payload(person, location=None),
        overlay=contact_info_payload(person),
    )

    async def run_once(page: ProfilePage, at: datetime) -> EnrichRunReport:
        provider, _ = fake_provider(ProfileSite([page]))
        async with provider.run("account-1") as run:
            source = PageProfiles(
                run, sleep=Sleeps(), landing_wait_s=0.05, lazy_wait_s=0.01, overlay_wait_s=0.05
            )
            with session_scope(session_factory, write=True) as session:
                user = session.get(User, user_id)
                assert user is not None
                row = get_scoped(session, user, Contact, ids[person.n])
                assert row is not None
                row.enrich_priority = 1  # something asked: visit again within the stale window
            return await enrich_contacts(
                session_factory,
                user_id,
                source,
                settings=SMALL,
                clock=Clock(at),
                sleep=Sleeps(),
                rng=random.Random(SEED),
            )

    first = await run_once(full, NOW)
    assert first.harvests.applied == 1
    contact = _contact(session_factory, user_id, ids[person.n])
    assert contact.headline == person.headline
    assert contact.location == "Faketown, State of Example"
    assert (contact.current_title, contact.current_company) == (
        "Staff Engineer",
        "Fictional Robotics Co",
    )

    second = await run_once(sparse, NOW + timedelta(days=1))
    assert second.harvests.applied == 1
    again = _contact(session_factory, user_id, ids[person.n])
    assert again.location == "Faketown, State of Example"
    assert again.current_title == "Staff Engineer"

    def emails(session: Session, user: User) -> list[str]:
        row = get_scoped(session, user, Contact, ids[person.n])
        assert row is not None
        return [email.email for email in row.emails]

    assert _read(session_factory, user_id, emails) == ["priya.fake@example.test"]


async def test_a_wall_after_the_contact_info_click_flags_the_session(
    session_factory: sessionmaker[Session],
) -> None:
    """#193 review, M1: the click leads to a checkpoint and no overlay answers. The run
    stops there as a checkpoint -- heat raised, the session flagged -- and visits nobody
    else."""
    from profile_site import CHECKPOINT_URL, ProfilePage, ProfileSite
    from run_fakes import fake_provider
    from voyager_pages import PEOPLE as CAST

    from netkeeper.linkedin.page_profiles import PageProfiles

    cast = CAST[:2]
    user_id, _ = _setup(
        session_factory,
        [Profile(p.n, p.first, p.last, public_id=p.slug) for p in cast],
    )
    site = ProfileSite(
        [ProfilePage(cast[0], tab_after_click=CHECKPOINT_URL, overlay_answers=0)]
        + [ProfilePage(p) for p in cast[1:]]
    )
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        source = PageProfiles(
            run, sleep=Sleeps(), landing_wait_s=0.05, lazy_wait_s=0.01, overlay_wait_s=0.05
        )
        report = await enrich_contacts(
            session_factory,
            user_id,
            source,
            settings=SMALL,
            clock=Clock(),
            sleep=Sleeps(),
            rng=random.Random(SEED),
        )
    assert report.result.outcome is Outcome.CHECKPOINT
    assert report.session_flagged and report.heat_raised
    assert report.result.visits == 1 and len(site.clicks) == 1
    flag = _read(session_factory, user_id, lambda s, u: session_flag(s, u))
    assert flag is not None
