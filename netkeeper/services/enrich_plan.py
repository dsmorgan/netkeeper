"""Who enrichment visits, in what order, and the plan a run leaves behind (spec 9.6, 9.9).

The core's half of choosing an enrichment run's targets, kept apart from the
runner (:mod:`netkeeper.services.enrichment`) so that the module that reads
contacts to rank them never touches an extractor result, and the runner that
handles results never names a table (``tests/test_extractor_boundary.py``).
Everything here speaks in contact ids and slugs; the runner turns them into
the job's ``EnrichTarget`` values.

**Prioritization (spec 9.6).** Pinned contacts first, in the order they were
pinned; then, among the contacts enrichment may visit:

1. contacts something has asked to enrich (``enrich_priority`` above 0, highest
   first) -- the hook spec 9.6's first tier, "contacts you are about to enroll
   in a campaign that lack the channel's address", fills when the campaign
   engine (P3) exists. Nothing sets it today;
2. ``met`` contacts never enriched;
3. stale contacts, enriched longer ago than ``enrich_stale_days`` (180);
4. everyone else never enriched.

Within each tier, newest connection first (``connected_on``, then the newest
row). A contact enriched within ``enrich_stale_days`` is not visited again
unless something asks for it. The list is cut at the run's visit budget, so a
pin takes a place *within* the budget, never one on top of it (spec 9.6).

**Who enrichment may visit at all.** A contact that is not merged away, not
archived, not disconnected, not marked do-not-contact, and not waiting for
review (#184), with a URN and a slug. The URN is the point: it is how
:func:`netkeeper.crm.apply.apply_harvest` knows the profile a slug led to is
the contact's, and every connection a sync has seen has one. A contact whose
last visit wrote nothing (no profile, a profile under another URN, a slug
another contact holds, an unreadable shape) waits :data:`ENRICH_RETRY_AFTER`
before the next, so it cannot head every run, and spec 9.8's NotFound streak
("3 across at least 14 days") is spread across two weeks rather than spent on
three consecutive days. A visit whose harvest was applied does not wait (#172):
it wrote something, so ``last_enriched_at`` is as new as
``li_enrich_attempted_at``, and only a fresh ask (``enrich_priority``, which
the harvest cleared) brings the contact back before it goes stale. A pin
overrides the tiers and the wait, never the eligibility.

**Pins (spec 9.6).** At most :data:`MAX_PINS`, stored in ``settings_kv`` per
account. A pin is removed when a run finishes with that contact (harvested or
not found), so it pins the *next* run, as igtracker's pins did.

**The stored plan (spec 9.9).** Every enrichment run stores its ordered
contact ids on its own ``sync_runs`` row (``plan_json``, P2-10) and, as each
visit's harvest is written, the ids it completed -- in the same transaction as
the harvest, so the two never disagree. A resume (:func:`start_resume`) is a
new run whose plan is the old one's remaining contacts in the old order
(``resume_of_id`` names the old run): it skips what completed and never
re-plans, and a plan is resumed at most once. The cancel flag is the run's
(``services.runs.request_cancel``), checked between profiles and inside the
waits between them. Before P2-10 the plan lived in ``settings_kv``; migration
0013 moved every unfinished one onto a run.

Transactions belong to the caller. Every writer here reads first, so it needs a
writer session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import Select, and_, case, or_
from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactMet,
    SyncRun,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    User,
)
from netkeeper.scoping import scoped
from netkeeper.services import runs
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

#: Spec 9.6: "You can pin up to 5 contacts to the front of the next run."
MAX_PINS: Final = 5

#: How long a contact waits after an enrichment visit that wrote nothing before
#: the next one: no profile, a profile under another URN, a slug another contact
#: holds, or a shape the parser could not read (``li_enrich_attempted_at``). Without
#: it one such contact heads every run's queue and costs a profile visit every day.
#: For NotFound it is also the spacing spec 9.8 wants: three NotFound a week apart
#: land across 14 days.
ENRICH_RETRY_AFTER: Final = timedelta(days=7)


class PinError(ValueError):
    """A pin that cannot be taken: the list is full, or the contact cannot be visited."""


class PlanNotFound(LookupError):
    """No enrichment run with that id, or one with no plan."""


class PlanFinished(ValueError):
    """The plan has nothing to resume: completed, empty, still running, or already resumed."""


# --- who may be visited -------------------------------------------------------


def _eligible(user: User) -> Select[tuple[Contact]]:
    return scoped(user, Contact).where(
        Contact.merged_into_id.is_(None),
        Contact.archived_at.is_(None),
        Contact.li_disconnected_at.is_(None),
        Contact.do_not_contact.is_(False),
        Contact.li_urn.is_not(None),
        Contact.li_public_id.is_not(None),
        # #184: a contact read only off a connections-page card is not visited
        # until a person confirms it or a sync gives it a URN. The URN rule above
        # already keeps out every such contact a sync created; this keeps out one
        # that came by a URN some other way (a merge, an edit) while still unconfirmed.
        Contact.needs_review_at.is_(None),
    )


def prioritize(
    session: Session,
    user: User,
    account_id: int,
    *,
    now: datetime,
    limit: int,
    stale_days: int,
) -> list[tuple[int, str]]:
    """The next run's ``(contact id, slug)`` list, pins first, cut at ``limit``. Read-only."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if limit <= 0:
        return []
    chosen: list[tuple[int, str]] = []
    pins = pinned(session, user, account_id)
    by_id = (
        {
            contact.id: contact
            for contact in session.scalars(_eligible(user).where(Contact.id.in_(pins)))
        }
        if pins
        else {}
    )
    for contact_id in pins:
        contact = by_id.get(contact_id)
        if contact is not None and contact.li_public_id is not None:
            chosen.append((contact.id, contact.li_public_id))
    if len(chosen) >= limit:
        return chosen[:limit]

    stale_before = now - timedelta(days=stale_days)
    never = Contact.last_enriched_at.is_(None)
    tier = case(
        (Contact.enrich_priority > 0, 1),
        (and_(Contact.met == ContactMet.MET, never), 2),
        (Contact.last_enriched_at < stale_before, 3),
        (never, 4),
        else_=5,
    )
    statement = (
        _eligible(user)
        .where(
            tier < 5,
            or_(
                Contact.li_enrich_attempted_at.is_(None),
                Contact.li_enrich_attempted_at <= now - ENRICH_RETRY_AFTER,
                # The wait is for a visit that wrote nothing (#172): a visit whose
                # harvest was applied set last_enriched_at to the same instant.
                Contact.last_enriched_at >= Contact.li_enrich_attempted_at,
            ),
        )
        .order_by(
            tier,
            Contact.enrich_priority.desc(),
            Contact.connected_on.desc().nulls_last(),
            Contact.id.desc(),
        )
        .limit(limit - len(chosen) + len(pins))
    )
    taken = {contact_id for contact_id, _ in chosen}
    for contact in session.scalars(statement):
        if len(chosen) >= limit:
            break
        if contact.id in taken or contact.li_public_id is None:
            continue
        chosen.append((contact.id, contact.li_public_id))
    return chosen


# --- pins ---------------------------------------------------------------------


def pins_key(account_id: int) -> str:
    return f"linkedin.enrich.{account_id}.pins"


def pinned(session: Session, user: User, account_id: int) -> list[int]:
    """The account's pinned contact ids, in the order they were pinned. Read-only.

    A stored value this cannot read is no pins, logged, rather than an error:
    pins are a convenience, and a run must not fail over one.
    """
    raw = get_setting(session, user, pins_key(account_id), default=[])
    if not isinstance(raw, list) or not all(
        isinstance(item, int) and not isinstance(item, bool) for item in raw
    ):
        log.warning("enrichment pins for account %d are unreadable; ignoring them", account_id)
        return []
    return [item for item in raw if isinstance(item, int)]


def pin(session: Session, user: User, account_id: int, contact_id: int) -> list[int]:
    """Pin ``contact_id`` to the front of the next run; returns the pins.

    Pinning a contact already pinned changes nothing. :class:`PinError` when
    :data:`MAX_PINS` are already pinned, or the contact is not one enrichment
    may visit (see the module docstring).
    """
    _require_writer(session)
    pins = pinned(session, user, account_id)
    if contact_id in pins:
        return pins
    if len(pins) >= MAX_PINS:
        raise PinError(f"at most {MAX_PINS} contacts can be pinned; unpin one first")
    contact = session.scalars(_eligible(user).where(Contact.id == contact_id)).one_or_none()
    if contact is None:
        raise PinError(
            f"contact {contact_id} cannot be enriched: it needs a LinkedIn URN and profile"
            " url, and must not be archived, merged, disconnected, or do-not-contact"
        )
    pins.append(contact_id)
    set_setting(session, user, pins_key(account_id), pins)
    return pins


def unpin(session: Session, user: User, account_id: int, contact_id: int) -> list[int]:
    """Remove ``contact_id`` from the pins, if it is there; returns the pins."""
    _require_writer(session)
    pins = pinned(session, user, account_id)
    if contact_id in pins:
        pins.remove(contact_id)
        set_setting(session, user, pins_key(account_id), pins)
    return pins


# --- the stored plan ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """One enrichment run's plan, as its ``sync_runs`` row holds it (``plan_json``).

    ``contact_ids`` is in visiting order and ``completed`` what the run finished
    with. ``status`` is the run's. ``resumed_by`` is the run that took the rest
    of this plan over, once one has: a plan is resumed at most once, so two
    resumes of one aborted run cannot visit the same people twice.
    """

    run_id: int
    account_id: int
    status: SyncRunStatus
    contact_ids: tuple[int, ...]
    completed: tuple[int, ...] = ()
    resumed_by: int | None = None

    @property
    def remaining(self) -> tuple[int, ...]:
        """The planned contacts not yet completed, in the planned order."""
        done = set(self.completed)
        return tuple(contact_id for contact_id in self.contact_ids if contact_id not in done)


def store_plan(session: Session, user: User, run_id: int, contact_ids: list[int]) -> StoredPlan:
    """Give enrichment run ``run_id`` its plan: ``contact_ids``, in visiting order."""
    _require_writer(session)
    if len(set(contact_ids)) != len(contact_ids):
        raise ValueError("a contact may appear in an enrichment plan only once")
    run = _enrich_run(session, user, run_id)
    if run.plan_json is not None:
        raise ValueError(f"run {run_id} already has a plan; a plan is never re-planned")
    run.plan_json = {"contact_ids": list(contact_ids), "completed": []}
    return _plan_of(run)


def load_plan(session: Session, user: User, run_id: int) -> StoredPlan:
    """Run ``run_id``'s plan; :class:`PlanNotFound` when it has none. Read-only."""
    try:
        run = _enrich_run(session, user, run_id)
    except runs.RunNotFound as exc:
        raise PlanNotFound(f"no enrichment run {run_id}") from exc
    except ValueError as exc:  # a connections sync: it has no plan to load or resume
        raise PlanNotFound(str(exc)) from exc
    if run.plan_json is None:
        raise PlanNotFound(f"run {run_id} has no enrichment plan")
    return _plan_of(run)


def mark_completed(session: Session, user: User, run_id: int, contact_id: int) -> StoredPlan:
    """Record that the run finished with ``contact_id``, and unpin it.

    Call in the same transaction as the harvest's mapping, so a plan never
    says "done" about a contact whose harvest was not written, or the reverse.
    """
    _require_writer(session)
    run = _enrich_run(session, user, run_id)
    plan = _plan_of(run)
    if contact_id not in plan.contact_ids:
        raise ValueError(f"contact {contact_id} is not in the plan of run {run_id}")
    if contact_id not in plan.completed:
        # A new dict, so the JSON column sees the change.
        run.plan_json = {**(run.plan_json or {}), "completed": [*plan.completed, contact_id]}
    unpin(session, user, run.linkedin_account_id, contact_id)
    return _plan_of(run)


def start_resume(
    session: Session,
    user: User,
    of_run_id: int,
    *,
    now: datetime,
    max_visits: int | None = None,
) -> SyncRun:
    """Record a manual run that takes over what enrichment run ``of_run_id`` left (spec 9.9).

    The new run's plan is the old plan's remaining contacts, in the old order:
    never re-planned. :class:`PlanNotFound` when there is no such plan,
    :class:`PlanFinished` when it completed, has nothing left, is still running,
    or was already resumed. The refusals of
    :func:`netkeeper.services.runs.create_run` apply as well.
    """
    _require_writer(session)
    plan = load_plan(session, user, of_run_id)
    if plan.status is SyncRunStatus.RUNNING:
        raise PlanFinished(f"run {of_run_id} is still running; cancel it before resuming it")
    if plan.status is SyncRunStatus.COMPLETED or not plan.remaining:
        raise PlanFinished(f"run {of_run_id} completed its plan; there is nothing to resume")
    if plan.resumed_by is not None:
        raise PlanFinished(
            f"run {of_run_id} was already resumed by run {plan.resumed_by}; resume that one"
        )
    new = runs.create_run(
        session,
        user,
        SyncRunKind.ENRICH,
        trigger=SyncRunTrigger.MANUAL,
        now=now,
        max_visits=max_visits,
        resume_of_id=of_run_id,
    )
    new.plan_json = {"contact_ids": list(plan.remaining), "completed": []}
    old = _enrich_run(session, user, of_run_id)
    old.plan_json = {**(old.plan_json or {}), "resumed_by": new.id}
    return new


def targets_for(session: Session, user: User, plan: StoredPlan) -> list[tuple[int, str]]:
    """The plan's remaining ``(contact id, slug)`` pairs, in order, as they stand now. Read-only.

    The slug is read now, not stored with the plan: a sync that saw a vanity-url
    change between the abort and the resume has already moved it. A contact
    that can no longer be visited (merged away, archived, disconnected,
    do-not-contact, no URN or slug) is left out; nothing else is re-decided,
    so a resume never re-plans.
    """
    remaining = plan.remaining
    if not remaining:
        return []
    visitable = {
        contact.id: contact.li_public_id
        for contact in session.scalars(_eligible(user).where(Contact.id.in_(remaining)))
    }
    return [
        (contact_id, slug)
        for contact_id in remaining
        if (slug := visitable.get(contact_id)) is not None
    ]


def _enrich_run(session: Session, user: User, run_id: int) -> SyncRun:
    run = runs.get_run(session, user, run_id)
    if run.kind is not SyncRunKind.ENRICH:
        raise ValueError(f"run {run_id} is a {run.kind.value} run, not an enrichment run")
    return run


def _plan_of(run: SyncRun) -> StoredPlan:
    raw = run.plan_json
    if not isinstance(raw, dict):
        raise PlanNotFound(f"run {run.id} has no enrichment plan")
    try:
        resumed_by = raw.get("resumed_by")
        return StoredPlan(
            run_id=run.id,
            account_id=run.linkedin_account_id,
            status=run.status,
            contact_ids=tuple(int(item) for item in raw["contact_ids"]),
            completed=tuple(int(item) for item in raw["completed"]),
            resumed_by=None if resumed_by is None else int(resumed_by),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanNotFound(f"the plan of run {run.id} is unreadable: {exc}") from exc


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError(
            "enrichment plans and pins need a writer session;"
            " use session_scope(factory, write=True)"
        )
