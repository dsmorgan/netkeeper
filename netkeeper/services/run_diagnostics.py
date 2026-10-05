"""Why a LinkedIn run's visits or pages could not be read, from its stored record (#405).

An enrichment run records every visit that counted toward the unreadable limits
(:mod:`netkeeper.services.enrichment`): its number in the run, the contact's id,
and a fixed reason code (:class:`~netkeeper.linkedin.enrich.UnreadableCause`). A
connections run records every answer it lost (#200): the list offset and a fixed
cause. Neither holds anything from the page.

This module reads them back for the run detail view and ``netkeeper linkedin run``:
the final record in ``counts_json`` when the run has ended, else the running record
in ``progress_json`` (a run that ended by exception keeps only that one). It adds the
contact's name from the CRM, which is netkeeper's own data. A record in a shape this
does not know reads as nothing rather than failing the view.

Read-only, and every contact lookup is scoped to the run's user.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.orm import Session

from netkeeper.models import Contact, SyncRun, SyncRunKind, User
from netkeeper.scoping import scoped, scoped_contacts

log = logging.getLogger(__name__)

#: Each reason code (:class:`netkeeper.linkedin.enrich.UnreadableCause`) in the words a
#: person reads. Keyed by the stored strings, not the enum: this module is reached by the
#: app and the CLI, which must not import the extractor's job (``tests/test_posture.py``);
#: a test pins that every cause has words here.
REASON_TEXT: Final[Mapping[str, str]] = {
    "profile_shape_unknown": ("the profile answered in a shape the parser does not know"),
    "contact_info_shape_unknown": ("the Contact info answered in a shape the parser does not know"),
    "landed_off_profile": "the tab landed somewhere that is not a profile",
    "left_profile": "the tab left the profile during the visit",
    "unexpected_profile": "the tab was on a profile no redirect led to",
    "no_profile_screen": "the profile's screen never arrived",
    "profile_screen_status": "the profile's screen request answered 404",
    "too_many_lazy_cards": "more profile cards arrived than a profile loads",
    "contact_info_control_missing": "no Contact info control on the page",
    "contact_info_control_not_alone": "more than one Contact info control",
    "contact_info_control_unreadable": "the Contact info control could not be read",
    "contact_info_control_elsewhere": "the Contact info control opens something else",
    "contact_info_control_unclickable": ("the Contact info control could not be clicked"),
    "contact_info_not_clicked": "Contact info was not clicked",
    "overlay_never_answered": "the Contact info overlay never answered",
    "overlay_other_profile": "the page asked for another profile's Contact info",
    "overlay_redirected": "the Contact info answer redirected elsewhere",
    "overlay_status": "the Contact info answered with an unexpected status",
    "navigation_timed_out": "the profile never finished loading",
    "profile_screen_lost": "the profile's screen arrived with no readable body",
    "contact_info_lost": "the Contact info answer arrived with no readable body",
    "contact_info_deferred": (
        "the Contact info answer arrived with no readable body; the profile was saved,"
        " and Contact info is read on a later visit (not counted as unreadable)"
    ),
    "profile_status": "the profile answered a status that stopped the run at once",
    "id_mismatch": "the profile's id is not the contact's; nothing saved",
    "unknown": "no cause was recorded",
}


@dataclass(frozen=True, slots=True)
class VisitReason:
    """One enrichment visit that counted toward the unreadable limits."""

    visit: int
    contact_id: int
    reason: str
    reason_text: str
    #: The contact's name in the CRM, or ``None`` for one deleted since.
    first_name: str | None
    last_name: str | None
    contact_exists: bool


@dataclass(frozen=True, slots=True)
class LostAnswerReason:
    """One answer a connections run lost (#200): the list offset and a fixed cause."""

    start: int
    cause: str
    ending: str | None


@dataclass(frozen=True, slots=True)
class RunDiagnostics:
    """A run's per-visit and per-answer reasons. Both empty for a run that has none.

    ``stopped_by`` is the visit whose answer stopped the run at once as
    ``route_changed`` (not by the unreadable limits), or ``None``.
    """

    unreadable_visits: tuple[VisitReason, ...]
    lost_answers: tuple[LostAnswerReason, ...]
    stopped_by: VisitReason | None = None


@dataclass(frozen=True, slots=True)
class RecurringReason:
    """One contact and reason code across enrichment runs (``linkedin unreadable``)."""

    contact_id: int
    reason: str
    reason_text: str
    #: How many visits recorded it, and in which runs, oldest first.
    visits: int
    run_ids: tuple[int, ...]
    last_seen: datetime
    first_name: str | None
    last_name: str | None
    contact_exists: bool


def reason_text(code: str) -> str:
    """A reason code in plain words, or the code itself for one with no words yet."""
    return REASON_TEXT.get(code, code)


def _record(run: SyncRun, key: str) -> list[Any]:
    """``key`` from the run's counts, else from its progress; ``[]`` for any other shape."""
    for source in (run.counts_json, run.progress_json):
        if isinstance(source, dict) and key in source:
            value = source[key]
            if isinstance(value, list):
                return value
            log.warning("run %d's %s is not a list; ignoring it", run.id, key)
            return []
    return []


def _visit(item: Any) -> tuple[int, int, str] | None:
    if not isinstance(item, dict):
        return None
    visit, contact_id, reason = item.get("visit"), item.get("contact_id"), item.get("reason")
    if isinstance(visit, int) and isinstance(contact_id, int) and isinstance(reason, str):
        return visit, contact_id, reason
    return None


def _visits(run: SyncRun) -> list[tuple[int, int, str]]:
    return [row for row in map(_visit, _record(run, "unreadable_visits")) if row is not None]


def _stopped_by(run: SyncRun) -> tuple[int, int, str] | None:
    counts = run.counts_json
    return _visit(counts.get("stopped_by")) if isinstance(counts, dict) else None


def _contacts(session: Session, user: User, ids: set[int]) -> dict[int, Contact]:
    if not ids:
        return {}
    return {
        contact.id: contact
        for contact in session.scalars(scoped_contacts(user).where(Contact.id.in_(ids)))
    }


def _lost(run: SyncRun) -> tuple[LostAnswerReason, ...]:
    rows: list[LostAnswerReason] = []
    for item in _record(run, "lost"):
        if not isinstance(item, dict):
            continue
        start, cause, ending = item.get("start"), item.get("cause"), item.get("ending")
        if isinstance(start, int) and isinstance(cause, str):
            rows.append(LostAnswerReason(start, cause, ending if isinstance(ending, str) else None))
    return tuple(rows)


def diagnose(session: Session, user: User, run: SyncRun) -> RunDiagnostics:
    """``run``'s stored reasons, with each contact's name. Read-only.

    ``run`` must already be ``user``'s (``runs.get_run``); contacts are looked up
    through the scoping helper all the same.
    """
    visits = _visits(run)
    stopped = _stopped_by(run)
    rows = [*visits, *([] if stopped is None else [stopped])]
    contacts = _contacts(session, user, {contact_id for _, contact_id, _ in rows})

    def named(visit: int, contact_id: int, reason: str) -> VisitReason:
        contact = contacts.get(contact_id)
        return VisitReason(
            visit=visit,
            contact_id=contact_id,
            reason=reason,
            reason_text=reason_text(reason),
            first_name=None if contact is None else contact.first_name,
            last_name=None if contact is None else contact.last_name,
            contact_exists=contact is not None,
        )

    return RunDiagnostics(
        unreadable_visits=tuple(named(*row) for row in visits),
        lost_answers=_lost(run),
        stopped_by=None if stopped is None else named(*stopped),
    )


def recurring(session: Session, user: User, *, since: datetime) -> list[RecurringReason]:
    """Every recorded reason of ``user``'s enrichment runs started since ``since``, grouped
    by contact and code: "is it the same contacts every morning?" in one read (#405).

    Includes the visit that stopped a run at once. Most visits first, then the most
    recent. Read in Python from each run's record rather than with a JSON table
    function, so it reads the same on SQLite and PostgreSQL.
    """
    statement = (
        scoped(user, SyncRun)
        .where(SyncRun.kind == SyncRunKind.ENRICH, SyncRun.started_at >= since)
        .order_by(SyncRun.started_at, SyncRun.id)
    )
    groups: dict[tuple[int, str], list[SyncRun]] = {}
    for run in session.scalars(statement):
        stopped = _stopped_by(run)
        for _, contact_id, reason in [*_visits(run), *([] if stopped is None else [stopped])]:
            groups.setdefault((contact_id, reason), []).append(run)
    contacts = _contacts(session, user, {contact_id for contact_id, _ in groups})
    found = []
    for (contact_id, reason), seen in groups.items():
        contact = contacts.get(contact_id)
        found.append(
            RecurringReason(
                contact_id=contact_id,
                reason=reason,
                reason_text=reason_text(reason),
                visits=len(seen),
                run_ids=tuple(dict.fromkeys(run.id for run in seen)),
                last_seen=seen[-1].started_at,
                first_name=None if contact is None else contact.first_name,
                last_name=None if contact is None else contact.last_name,
                contact_exists=contact is not None,
            )
        )
    found.sort(key=lambda item: (-item.visits, -item.last_seen.timestamp(), item.contact_id))
    return found
