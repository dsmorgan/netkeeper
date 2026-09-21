"""``/triage``: the keyboard flow of spec 10.2, one request per contact.

Every answer that hands out a contact hands out its evidence with it, and every
answer that advances the queue prefetches the contact after it, so a run of
fifty is fifty requests, not fifty plus two hundred for the evidence panels.
``tests/test_web_triage.py`` counts the requests and the queries a run costs.

The rules live in :mod:`netkeeper.crm.triage`; this module is the shape of the
request and the status codes. A contact that is not the current user's answers
``404``, never ``403``: a ``403`` would confirm that the id exists for someone.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from netkeeper.crm import triage as service
from netkeeper.crm.triage import Card, Progress
from netkeeper.models import ContactMet, TriageDecision
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import (
    InteractionOut,
    PreferredNameIn,
    PreferredNameOut,
    SharedCompanyOut,
    TriageCardOut,
    TriageContactOut,
    TriageDecisionIn,
    TriageDecisionOut,
    TriageDecisionResult,
    TriageEvidenceOut,
    TriageMessagesOut,
    TriageProgressOut,
    TriageQueueOut,
    TriageSuggestionApplyIn,
    TriageSuggestionApplyOut,
    TriageSuggestionOut,
    TriageUndoIn,
    TriageUndoOut,
    timeline_entry_out,
)

router = APIRouter(tags=["triage"])

Responses = dict[int | str, dict[str, Any]]
NO_SUCH_CONTACT: Responses = {404: {"description": "No such contact"}}
NOTHING_TO_UNDO: Responses = {404: {"description": "No triage decision left to undo"}}
UNDO_CONFLICT: Responses = {
    409: {"description": "The contact changed after the decision; retry with `force`"}
}
COUNT_CHANGED: Responses = {409: {"description": "The suggestion matches a different count now"}}
NO_SUCH_SUGGESTION: Responses = {404: {"description": "No suggestion by that key"}}

States = Annotated[
    list[ContactMet] | None,
    Query(
        description="The met states the queue holds. Defaults to `unknown`; pass `skip` to revisit."
    ),
]
AfterId = Annotated[
    int | None,
    Query(
        ge=1, description="Cursor: the first contact past this id, for moving on without deciding."
    ),
]
Prefetch = Annotated[
    bool, Query(description="Also return the contact after this one, so the client never waits.")
]


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions to HTTP statuses: 404, 409, 422."""
    try:
        yield
    except service.NotFound as exc:
        raise HTTPException(status_code=404, detail="no such contact") from exc
    except service.NothingToUndo as exc:
        raise HTTPException(status_code=404, detail="nothing to undo") from exc
    except (service.UndoConflict, service.CountChanged) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.InvalidDecision as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/triage/next", operation_id="get_next_triage_contact")
def get_next_triage_contact(
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
    after_id: AfterId = None,
    prefetch: Prefetch = True,
) -> TriageQueueOut:
    """The next contact to triage with its evidence, the one after it, and the progress.

    `after_id` moves on without deciding (the `→` key). Both cards are `null`
    when the queue is empty.
    """
    wanted = _states(states)
    with translate_errors():
        card = service.next_card(session, user, states=wanted, after_id=after_id)
        following = (
            service.next_card(session, user, states=wanted, after_id=card.contact.id)
            if prefetch and card is not None
            else None
        )
        counters = service.progress(session, user, states=wanted)
    return TriageQueueOut(
        card=_card_out(card), next=_card_out(following), progress=_progress_out(counters)
    )


@router.post(
    "/triage/decisions",
    operation_id="decide_triage",
    status_code=201,
    responses={**NO_SUCH_CONTACT},
)
def decide_triage(
    body: TriageDecisionIn, user: CurrentUser, session: SessionDep, states: States = None
) -> TriageDecisionResult:
    """Record `met`, `not_met`, or `skip` on a contact and return the next card with it.

    The response carries the contact after `prefetch_after_id` (or after the one
    just decided), so triaging a run costs one request per contact.
    """
    wanted = _states(states)
    with translate_errors():
        decision = service.decide(session, user, body.contact_id, body.met)
        after = body.prefetch_after_id if body.prefetch_after_id is not None else body.contact_id
        following = service.next_card(session, user, states=wanted, after_id=after)
        counters = service.progress(session, user, states=wanted)
    return TriageDecisionResult(
        decision=_decision_out(decision),
        next=_card_out(following),
        progress=_progress_out(counters),
    )


@router.post(
    "/triage/undo",
    operation_id="undo_triage",
    responses={**NOTHING_TO_UNDO, **UNDO_CONFLICT},
)
def undo_triage(
    user: CurrentUser,
    session: SessionDep,
    body: TriageUndoIn | None = None,
    states: States = None,
) -> TriageUndoOut:
    """Undo the newest triage action, restoring the previous state exactly.

    A bulk apply is undone as one batch. `409` when the contact changed after the
    decision, so nothing is overwritten silently; `force` in the body restores
    anyway.
    """
    wanted = _states(states)
    with translate_errors():
        undone = service.undo(session, user, force=body.force if body is not None else False)
        card = None if undone.contact is None else service.load_card(session, user, undone.contact)
        counters = service.progress(session, user, states=wanted)
    return TriageUndoOut(
        kind=undone.kind,
        decisions=undone.decisions,
        batch_id=undone.batch_id,
        forced=undone.forced,
        card=_card_out(card),
        progress=_progress_out(counters),
    )


@router.put(
    "/triage/contacts/{contact_id}/preferred-name",
    operation_id="set_preferred_name",
    responses=NO_SUCH_CONTACT,
)
def set_preferred_name(
    contact_id: int, body: PreferredNameIn, user: CurrentUser, session: SessionDep
) -> PreferredNameOut:
    """The `p` key: what you call this person, as a manual edit no later sync overwrites.

    An empty name falls back to the first name. The edit is undoable like a
    decision.
    """
    with translate_errors():
        decision = service.set_preferred_name(session, user, contact_id, body.preferred_name)
    # What the column ended up with, which is the first name when the edit was
    # empty; the decision log recorded it, so no reload is needed.
    stored = decision.after_state.get("preferred_name") or ""
    return PreferredNameOut(
        contact_id=decision.contact_id, preferred_name=stored, decision=_decision_out(decision)
    )


@router.get("/triage/suggestions", operation_id="list_triage_suggestions")
def list_triage_suggestions(
    user: CurrentUser, session: SessionDep, states: States = None
) -> list[TriageSuggestionOut]:
    """The bulk actions worth offering, with the count each would apply to.

    A suggestion that matches nobody is left out, so the banner shows only when
    there is something to accept. Nothing is applied until `apply`.
    """
    with translate_errors():
        found = service.suggestions(session, user, states=_states(states))
    return [TriageSuggestionOut.model_validate(item) for item in found]


@router.post(
    "/triage/suggestions/{key}/apply",
    operation_id="apply_triage_suggestion",
    responses={**NO_SUCH_SUGGESTION, **COUNT_CHANGED},
)
def apply_triage_suggestion(
    key: str,
    user: CurrentUser,
    session: SessionDep,
    body: TriageSuggestionApplyIn | None = None,
    states: States = None,
) -> TriageSuggestionApplyOut:
    """Apply a bulk suggestion as one batch that a single undo takes back.

    Send the `expected_count` the banner showed: a set that has moved on since
    answers `409` rather than touching more people than the banner named.
    """
    wanted = _states(states)
    expected = body.expected_count if body is not None else None
    try:
        applied = service.apply_suggestion(
            session, user, key, states=wanted, expected_count=expected
        )
    except service.InvalidDecision as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CountChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    counters = service.progress(session, user, states=wanted)
    return TriageSuggestionApplyOut(
        key=applied.key,
        applied=applied.applied,
        batch_id=applied.batch_id,
        progress=_progress_out(counters),
    )


def _states(states: Sequence[ContactMet] | None) -> Sequence[ContactMet]:
    return service.DEFAULT_QUEUE_STATES if not states else list(states)


def _card_out(card: Card | None) -> TriageCardOut | None:
    if card is None:
        return None
    evidence = card.evidence
    return TriageCardOut(
        contact=TriageContactOut.model_validate(card.contact),
        evidence=TriageEvidenceOut(
            messages=TriageMessagesOut(
                total=evidence.messages.total,
                inbound=evidence.messages.inbound,
                outbound=evidence.messages.outbound,
                first_at=evidence.messages.first_at,
                last_at=evidence.messages.last_at,
                recent=[InteractionOut.model_validate(row) for row in evidence.messages.recent],
            ),
            timeline=[timeline_entry_out(entry) for entry in evidence.timeline],
            shared_companies=[
                SharedCompanyOut.model_validate(item) for item in evidence.shared_companies
            ],
        ),
    )


def _decision_out(decision: TriageDecision) -> TriageDecisionOut:
    return TriageDecisionOut.model_validate(decision)


def _progress_out(counters: Progress) -> TriageProgressOut:
    return TriageProgressOut(
        total=counters.total,
        triaged=counters.triaged,
        remaining=counters.remaining,
        by_state=counters.by_state,
    )
