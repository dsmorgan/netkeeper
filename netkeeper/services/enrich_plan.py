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
archived, not disconnected, and not marked do-not-contact, with a URN and a
slug. The URN is the point: it is how :func:`netkeeper.crm.apply.apply_harvest`
knows the profile a slug led to is the contact's, and every connection a sync
has seen has one. A contact whose last visit wrote nothing (no profile, a
profile under another URN, a slug another contact holds, an unreadable shape)
waits :data:`ENRICH_RETRY_AFTER` before the next, so it cannot head every run,
and spec 9.8's NotFound streak ("3 across at least 14 days") is spread across
two weeks rather than spent on three consecutive days. A pin overrides the
tiers and the wait, never the eligibility.

**Pins (spec 9.6).** At most :data:`MAX_PINS`, stored in ``settings_kv`` per
account. A pin is removed when a run finishes with that contact (harvested or
not found), so it pins the *next* run, as igtracker's pins did.

**The stored plan (spec 9.9).** Every run stores its ordered contact ids and,
as each visit's harvest is written, the ids it completed -- in the same
transaction as the harvest, so the two never disagree. A resumed run reuses
that list, skips what completed, and never re-plans. The cancel flag lives on
the plan too, checked between profiles and inside the waits between them.
``sync_run`` (spec 8.4) does not exist yet; P2-10 adds it, and then the plan
moves onto its row (``progress_json``, ``resume_of_id``) and this record goes.
Until then it is ``settings_kv`` under :func:`plan_key`, and its id is what a
resume names.

Transactions belong to the caller. Every writer here reads first, so it needs a
writer session.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import Select, and_, case, or_
from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import Contact, ContactMet, User
from netkeeper.scoping import scoped
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

PlanStatus = Literal["running", "aborted", "completed"]

_PLAN_VERSION: Final = 1


class PinError(ValueError):
    """A pin that cannot be taken: the list is full, or the contact cannot be visited."""


class PlanNotFound(LookupError):
    """No stored plan with that id for this account."""


class PlanFinished(ValueError):
    """The stored plan already completed; there is nothing to resume."""


# --- who may be visited -------------------------------------------------------


def _eligible(user: User) -> Select[tuple[Contact]]:
    return scoped(user, Contact).where(
        Contact.merged_into_id.is_(None),
        Contact.archived_at.is_(None),
        Contact.li_disconnected_at.is_(None),
        Contact.do_not_contact.is_(False),
        Contact.li_urn.is_not(None),
        Contact.li_public_id.is_not(None),
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
    """One run's plan as ``settings_kv`` holds it. ``contact_ids`` is in visiting order."""

    plan_id: str
    account_id: int
    created_at: datetime
    status: PlanStatus
    contact_ids: tuple[int, ...]
    completed: tuple[int, ...] = ()
    cancel_requested: bool = False
    stopped: str | None = None

    @property
    def remaining(self) -> tuple[int, ...]:
        """The planned contacts not yet completed, in the planned order."""
        done = set(self.completed)
        return tuple(contact_id for contact_id in self.contact_ids if contact_id not in done)


def plan_key(account_id: int, plan_id: str) -> str:
    return f"linkedin.enrich.{account_id}.plan.{plan_id}"


def create_plan(
    session: Session, user: User, account_id: int, contact_ids: list[int], *, now: datetime
) -> StoredPlan:
    """Store a new running plan for ``contact_ids`` and return it."""
    _require_writer(session)
    if len(set(contact_ids)) != len(contact_ids):
        raise ValueError("a contact may appear in an enrichment plan only once")
    plan = StoredPlan(
        plan_id=secrets.token_hex(8),
        account_id=account_id,
        created_at=now,
        status="running",
        contact_ids=tuple(contact_ids),
    )
    _store(session, user, plan)
    return plan


def load_plan(session: Session, user: User, account_id: int, plan_id: str) -> StoredPlan:
    """The stored plan ``plan_id`` of ``account_id``; :class:`PlanNotFound` otherwise. Read-only."""
    raw = get_setting(session, user, plan_key(account_id, plan_id))
    if not isinstance(raw, dict):
        raise PlanNotFound(f"no enrichment plan {plan_id!r} for this account")
    try:
        status = raw["status"]
        if status not in ("running", "aborted", "completed"):
            raise ValueError(f"unknown status {status!r}")
        stopped = raw.get("stopped")
        return StoredPlan(
            plan_id=str(raw["plan_id"]),
            account_id=int(raw["account_id"]),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            status=status,
            contact_ids=tuple(int(item) for item in raw["contact_ids"]),
            completed=tuple(int(item) for item in raw["completed"]),
            cancel_requested=bool(raw["cancel_requested"]),
            stopped=None if stopped is None else str(stopped),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanNotFound(f"enrichment plan {plan_id!r} is unreadable: {exc}") from exc


def mark_completed(
    session: Session, user: User, account_id: int, plan_id: str, contact_id: int
) -> StoredPlan:
    """Record that the run finished with ``contact_id``, and unpin it.

    Call in the same transaction as the harvest's mapping, so a plan never
    says "done" about a contact whose harvest was not written, or the reverse.
    """
    _require_writer(session)
    plan = load_plan(session, user, account_id, plan_id)
    if contact_id not in plan.contact_ids:
        raise ValueError(f"contact {contact_id} is not in enrichment plan {plan_id!r}")
    if contact_id not in plan.completed:
        plan = replace(plan, completed=(*plan.completed, contact_id))
        _store(session, user, plan)
    unpin(session, user, account_id, contact_id)
    return plan


def reopen(session: Session, user: User, account_id: int, plan_id: str) -> StoredPlan:
    """Mark a stored plan running again for a resume, clearing any cancel request.

    :class:`PlanFinished` when it completed: a resume of a finished plan would
    visit nobody, and saying so beats a run that silently does nothing.
    """
    _require_writer(session)
    plan = load_plan(session, user, account_id, plan_id)
    if plan.status == "completed":
        raise PlanFinished(f"enrichment plan {plan_id!r} completed; there is nothing to resume")
    plan = replace(plan, status="running", cancel_requested=False, stopped=None)
    _store(session, user, plan)
    return plan


def finish(
    session: Session,
    user: User,
    account_id: int,
    plan_id: str,
    *,
    status: PlanStatus,
    stopped: str,
) -> StoredPlan:
    """Record how the run on ``plan_id`` ended."""
    _require_writer(session)
    plan = replace(load_plan(session, user, account_id, plan_id), status=status, stopped=stopped)
    _store(session, user, plan)
    return plan


def request_cancel(session: Session, user: User, account_id: int, plan_id: str) -> StoredPlan:
    """Ask the run on ``plan_id`` to stop at its next check (spec 9.9: cooperative)."""
    _require_writer(session)
    plan = replace(load_plan(session, user, account_id, plan_id), cancel_requested=True)
    _store(session, user, plan)
    return plan


def cancel_requested(session: Session, user: User, account_id: int, plan_id: str) -> bool:
    """Whether a person asked the run on ``plan_id`` to stop. Read-only."""
    return load_plan(session, user, account_id, plan_id).cancel_requested


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


def _store(session: Session, user: User, plan: StoredPlan) -> None:
    set_setting(
        session,
        user,
        plan_key(plan.account_id, plan.plan_id),
        {
            "version": _PLAN_VERSION,
            "plan_id": plan.plan_id,
            "account_id": plan.account_id,
            "created_at": plan.created_at.isoformat(),
            "status": plan.status,
            "contact_ids": list(plan.contact_ids),
            "completed": list(plan.completed),
            "cancel_requested": plan.cancel_requested,
            "stopped": plan.stopped,
        },
    )


def _require_writer(session: Session) -> None:
    if not is_writer(session):
        raise RuntimeError(
            "enrichment plans and pins need a writer session;"
            " use session_scope(factory, write=True)"
        )
