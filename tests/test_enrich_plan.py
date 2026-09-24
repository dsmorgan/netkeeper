"""netkeeper.services.enrich_plan: spec 9.6's order, the pins, and the stored plan (spec 9.9).

Contacts here are built to land in exactly one tier each, with connection dates
chosen so that the tier, not the date, has to decide the order: a stale
contact connected yesterday must still come after a met one connected years ago.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.db import session_scope
from netkeeper.models import (
    Contact,
    ContactMet,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.services import enrich_plan, runs
from netkeeper.services.enrich_plan import (
    ENRICH_RETRY_AFTER,
    MAX_PINS,
    PinError,
    PlanFinished,
    PlanNotFound,
)

NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)
ACCOUNT = 1
STALE_DAYS = 180


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


def _contact(
    session: Session, user: User, connected: date | None = date(2024, 1, 1), **overrides: Any
) -> Contact:
    return factories.make_contact(session, user, connected_on=connected, **overrides)


def _order(session: Session, user: User, limit: int = 100) -> list[int]:
    chosen = enrich_plan.prioritize(
        session, user, ACCOUNT, now=NOW, limit=limit, stale_days=STALE_DAYS
    )
    return [contact_id for contact_id, _ in chosen]


def test_the_planning_constants_are_the_specs() -> None:
    assert MAX_PINS == 5
    assert timedelta(days=7) == ENRICH_RETRY_AFTER


# --- spec 9.6's order ---------------------------------------------------------------


def test_tiers_decide_before_connection_dates(writer: Session, user: User) -> None:
    """Asked-for, then met never enriched, then stale, then everyone else never enriched."""
    other = _contact(writer, user, connected=date(2026, 9, 1))
    stale = _contact(
        writer, user, connected=date(2026, 8, 1), last_enriched_at=NOW - timedelta(days=200)
    )
    met = _contact(writer, user, connected=date(2015, 1, 1), met=ContactMet.MET)
    asked = _contact(
        writer, user, connected=date(2010, 1, 1), last_enriched_at=NOW, enrich_priority=1
    )

    assert _order(writer, user) == [asked.id, met.id, stale.id, other.id]


def test_newest_connection_first_within_a_tier(writer: Session, user: User) -> None:
    old = _contact(writer, user, connected=date(2018, 5, 1))
    undated = _contact(writer, user, connected=None)
    new = _contact(writer, user, connected=date(2026, 9, 1))
    middle = _contact(writer, user, connected=date(2022, 3, 1))

    assert _order(writer, user) == [new.id, middle.id, old.id, undated.id]


def test_a_higher_enrich_priority_goes_first(writer: Session, user: User) -> None:
    low = _contact(writer, user, enrich_priority=1, connected=date(2026, 1, 1))
    high = _contact(writer, user, enrich_priority=9, connected=date(2020, 1, 1))
    assert _order(writer, user) == [high.id, low.id]


def test_a_met_contact_already_enriched_is_not_in_the_met_tier(writer: Session, user: User) -> None:
    fresh_met = _contact(writer, user, met=ContactMet.MET, last_enriched_at=NOW - timedelta(1))
    never = _contact(writer, user, connected=date(2000, 1, 1))
    assert _order(writer, user) == [never.id]
    assert fresh_met.id not in _order(writer, user)


def test_who_is_never_visited(writer: Session, user: User) -> None:
    visitable = _contact(writer, user)
    survivor = _contact(writer, user, connected=date(2000, 1, 1))
    _contact(writer, user, li_urn=None)
    _contact(writer, user, li_public_id=None)
    _contact(writer, user, archived_at=NOW)
    _contact(writer, user, li_disconnected_at=NOW)
    _contact(writer, user, do_not_contact=True)
    _contact(writer, user, merged_into_id=survivor.id, li_urn=None)
    _contact(writer, user, last_enriched_at=NOW - timedelta(days=STALE_DAYS - 1))
    _contact(writer, user, li_enrich_attempted_at=NOW - ENRICH_RETRY_AFTER + timedelta(hours=1))

    assert _order(writer, user) == [visitable.id, survivor.id]


def test_a_contact_whose_visit_wrote_nothing_is_visited_again_after_a_week(
    writer: Session, user: User
) -> None:
    waited = _contact(writer, user, li_enrich_attempted_at=NOW - ENRICH_RETRY_AFTER)
    assert _order(writer, user) == [waited.id]


def test_a_visit_that_wrote_something_does_not_wait_when_asked_for_again(
    writer: Session, user: User
) -> None:
    """#172: the week's wait is for a visit that wrote nothing, as spec 9.6 says.

    A tier-1 contact (``enrich_priority``, set by the campaign engine) whose last
    visit was applied yesterday is visited again when asked; one whose last visit
    wrote nothing still waits, priority or not.
    """
    yesterday = NOW - timedelta(days=1)
    applied = _contact(
        writer,
        user,
        enrich_priority=5,
        last_enriched_at=yesterday,
        li_enrich_attempted_at=yesterday,
    )
    _contact(writer, user, enrich_priority=5, li_enrich_attempted_at=yesterday)
    _contact(
        writer,
        user,
        enrich_priority=5,
        last_enriched_at=yesterday - timedelta(days=30),
        li_enrich_attempted_at=yesterday,
    )
    assert _order(writer, user) == [applied.id]


def test_the_limit_cuts_the_list(writer: Session, user: User) -> None:
    for year in range(2020, 2026):
        _contact(writer, user, connected=date(year, 1, 1))
    assert len(_order(writer, user, limit=4)) == 4
    assert _order(writer, user, limit=0) == []


def test_another_users_contacts_are_never_planned(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    _contact(writer, other)
    mine = _contact(writer, user)
    assert _order(writer, user) == [mine.id]


def test_the_plan_carries_the_current_slug(writer: Session, user: User) -> None:
    contact = _contact(writer, user, li_public_id="Casey-Fake-Poe")
    chosen = enrich_plan.prioritize(writer, user, ACCOUNT, now=NOW, limit=5, stale_days=180)
    assert chosen == [(contact.id, "casey-fake-poe")]


# --- pins -----------------------------------------------------------------------------


def test_pins_go_first_in_the_order_they_were_pinned(writer: Session, user: User) -> None:
    first = _contact(writer, user, connected=date(2026, 9, 1))
    recent = _contact(writer, user, last_enriched_at=NOW - timedelta(days=1))
    old = _contact(writer, user, connected=date(2001, 1, 1))
    enrich_plan.pin(writer, user, ACCOUNT, old.id)
    enrich_plan.pin(writer, user, ACCOUNT, recent.id)  # a pin overrides "enriched recently"

    assert _order(writer, user) == [old.id, recent.id, first.id]


def test_pins_take_places_within_the_budget_not_on_top_of_it(writer: Session, user: User) -> None:
    """Spec 9.6: "selected within the budget, not on top of it"."""
    unpinned = _contact(writer, user, connected=date(2026, 9, 1))
    pins = [_contact(writer, user, connected=date(2000, 1, 1)) for _ in range(3)]
    for contact in pins:
        enrich_plan.pin(writer, user, ACCOUNT, contact.id)

    assert _order(writer, user, limit=2) == [pins[0].id, pins[1].id]
    assert _order(writer, user, limit=4) == [*(c.id for c in pins), unpinned.id]


def test_at_most_five_pins(writer: Session, user: User) -> None:
    contacts = [_contact(writer, user) for _ in range(6)]
    for contact in contacts[:5]:
        enrich_plan.pin(writer, user, ACCOUNT, contact.id)
    with pytest.raises(PinError, match="at most 5"):
        enrich_plan.pin(writer, user, ACCOUNT, contacts[5].id)
    assert enrich_plan.pin(writer, user, ACCOUNT, contacts[0].id) == [c.id for c in contacts[:5]]


def test_a_contact_enrichment_may_not_visit_cannot_be_pinned(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    for contact in (
        _contact(writer, user, li_urn=None),
        _contact(writer, user, do_not_contact=True),
        _contact(writer, other),
    ):
        with pytest.raises(PinError, match="cannot be enriched"):
            enrich_plan.pin(writer, user, ACCOUNT, contact.id)
    assert enrich_plan.pinned(writer, user, ACCOUNT) == []


def test_a_pin_that_can_no_longer_be_visited_is_skipped(writer: Session, user: User) -> None:
    pinned = _contact(writer, user)
    other = _contact(writer, user)
    enrich_plan.pin(writer, user, ACCOUNT, pinned.id)
    pinned.archived_at = NOW
    writer.flush()
    assert _order(writer, user) == [other.id]


def test_unpin_and_pins_per_account(writer: Session, user: User) -> None:
    contact = _contact(writer, user)
    enrich_plan.pin(writer, user, ACCOUNT, contact.id)
    assert enrich_plan.pinned(writer, user, ACCOUNT + 1) == []
    assert enrich_plan.unpin(writer, user, ACCOUNT, contact.id) == []
    assert enrich_plan.unpin(writer, user, ACCOUNT, contact.id) == []


def test_unreadable_pins_are_no_pins(writer: Session, user: User) -> None:
    from netkeeper.services.settings_kv import set_setting

    set_setting(writer, user, enrich_plan.pins_key(ACCOUNT), {"not": "a list"})
    assert enrich_plan.pinned(writer, user, ACCOUNT) == []


# --- the stored plan (on the run's row since P2-10) -------------------------------------


def _run(writer: Session, user: User) -> SyncRun:
    return runs.create_run(writer, user, SyncRunKind.ENRICH, trigger=SyncRunTrigger.MANUAL, now=NOW)


def test_a_plan_round_trips_and_records_what_completed(writer: Session, user: User) -> None:
    contacts = [_contact(writer, user) for _ in range(3)]
    ids = [c.id for c in contacts]
    run = _run(writer, user)
    enrich_plan.pin(writer, user, run.linkedin_account_id, ids[1])
    enrich_plan.store_plan(writer, user, run.id, ids)

    enrich_plan.mark_completed(writer, user, run.id, ids[1])
    stored = enrich_plan.load_plan(writer, user, run.id)

    assert stored.contact_ids == tuple(ids) and stored.completed == (ids[1],)
    assert stored.remaining == (ids[0], ids[2])
    assert stored.status is SyncRunStatus.RUNNING
    assert run.plan_json == {"contact_ids": ids, "completed": [ids[1]]}
    assert enrich_plan.pinned(writer, user, run.linkedin_account_id) == []  # done with


def test_a_plan_is_never_replanned(writer: Session, user: User) -> None:
    run = _run(writer, user)
    enrich_plan.store_plan(writer, user, run.id, [1])
    with pytest.raises(ValueError, match="never re-planned"):
        enrich_plan.store_plan(writer, user, run.id, [2])


def test_completing_a_contact_not_in_the_plan_is_refused(writer: Session, user: User) -> None:
    run = _run(writer, user)
    enrich_plan.store_plan(writer, user, run.id, [1, 2])
    with pytest.raises(ValueError, match="not in the plan"):
        enrich_plan.mark_completed(writer, user, run.id, 3)


def test_a_plan_is_another_users_business(writer: Session, user: User) -> None:
    other = factories.make_user(writer)
    run = _run(writer, user)
    enrich_plan.store_plan(writer, user, run.id, [1])
    with pytest.raises(PlanNotFound):
        enrich_plan.load_plan(writer, other, run.id)
    with pytest.raises(PlanNotFound):
        enrich_plan.load_plan(writer, user, run.id + 1)


def test_a_run_with_no_plan_has_none_to_load(writer: Session, user: User) -> None:
    run = _run(writer, user)
    with pytest.raises(PlanNotFound):
        enrich_plan.load_plan(writer, user, run.id)


def test_a_sync_run_holds_no_plan(writer: Session, user: User) -> None:
    run = runs.create_run(
        writer, user, SyncRunKind.CONNECTIONS_FULL, trigger=SyncRunTrigger.MANUAL, now=NOW
    )
    with pytest.raises(ValueError, match="not an enrichment run"):
        enrich_plan.store_plan(writer, user, run.id, [1])


def _aborted_with_plan(writer: Session, user: User, ids: list[int], done: list[int]) -> SyncRun:
    run = _run(writer, user)
    enrich_plan.store_plan(writer, user, run.id, ids)
    for contact_id in done:
        enrich_plan.mark_completed(writer, user, run.id, contact_id)
    runs.finish_run(writer, user, run.id, status=SyncRunStatus.ABORTED, now=NOW)
    return run


def test_a_resume_takes_over_what_is_left_in_order_once(writer: Session, user: User) -> None:
    old = _aborted_with_plan(writer, user, [4, 2, 9, 7], done=[2])

    new = enrich_plan.start_resume(writer, user, old.id, now=NOW, max_visits=3)

    assert (new.resume_of_id, new.max_visits, new.trigger) == (old.id, 3, SyncRunTrigger.MANUAL)
    assert enrich_plan.load_plan(writer, user, new.id).contact_ids == (4, 9, 7)
    assert enrich_plan.load_plan(writer, user, old.id).resumed_by == new.id
    runs.finish_run(writer, user, new.id, status=SyncRunStatus.ABORTED, now=NOW)
    with pytest.raises(PlanFinished, match="already resumed by run"):
        enrich_plan.start_resume(writer, user, old.id, now=NOW)
    # The resume itself can be resumed.
    assert enrich_plan.start_resume(writer, user, new.id, now=NOW).resume_of_id == new.id


def test_a_completed_or_running_plan_is_not_resumed(writer: Session, user: User) -> None:
    done = _aborted_with_plan(writer, user, [1, 2], done=[1, 2])
    with pytest.raises(PlanFinished, match="nothing to resume"):
        enrich_plan.start_resume(writer, user, done.id, now=NOW)
    running = _run(writer, user)
    enrich_plan.store_plan(writer, user, running.id, [3])
    with pytest.raises(PlanFinished, match="still running"):
        enrich_plan.start_resume(writer, user, running.id, now=NOW)
    with pytest.raises(PlanNotFound):
        enrich_plan.start_resume(writer, user, running.id + 99, now=NOW)


def test_a_plan_names_each_contact_once(writer: Session, user: User) -> None:
    run = _run(writer, user)
    with pytest.raises(ValueError, match="only once"):
        enrich_plan.store_plan(writer, user, run.id, [1, 1])


def test_the_remaining_targets_keep_the_order_and_read_slugs_now(
    writer: Session, user: User
) -> None:
    a, b, c, d = (_contact(writer, user) for _ in range(4))
    run = _run(writer, user)
    enrich_plan.store_plan(writer, user, run.id, [d.id, b.id, a.id, c.id])
    enrich_plan.mark_completed(writer, user, run.id, b.id)
    a.li_public_id = "renamed-since"
    c.archived_at = NOW
    writer.flush()

    plan = enrich_plan.load_plan(writer, user, run.id)
    targets = enrich_plan.targets_for(writer, user, plan)

    assert targets == [(d.id, d.li_public_id), (a.id, "renamed-since")]
