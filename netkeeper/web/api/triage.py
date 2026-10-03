"""``/triage``: the keyboard flow of spec 10.2, one request per contact.

Every answer that hands out a contact hands out its evidence with it, and every
answer that advances the queue prefetches the contact after it, so a run of
fifty is fifty requests, not fifty plus two hundred for the evidence panels.
``tests/test_web_triage.py`` counts the requests and the queries a run costs.

The screens in the order they are used after an import: ``GET
/triage/suggestions`` says what netkeeper can decide in bulk and how many people
each batch covers, ``GET /triage/suggestions/{key}/contacts`` shows who, ``POST
/triage/suggestions/{key}/apply`` decides them as one undoable batch, and ``GET
/triage/next?decided_by=automatic`` walks that work back for review.

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
from netkeeper.models import ContactMet, MetSource, TriageDecision
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.errors import ApiError
from netkeeper.web.schemas import (
    InteractionOut,
    OverlapOut,
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
    TriageSuggestionPage,
    TriageUndoIn,
    TriageUndoOut,
    timeline_entry_out,
)

router = APIRouter(tags=["triage"])

Responses = dict[int | str, dict[str, Any]]
NO_SUCH_CONTACT: Responses = {404: {"description": "No such contact"}}
NOTHING_TO_UNDO: Responses = {404: {"description": "No triage decision left to undo"}}
UNDO_CONFLICT: Responses = {
    409: {
        "description": "The contact changed after the decision; retry with `force`. Or "
        'another undo took the same decision back first (`{"detail": <why>, "reason": '
        '"raced", "decision_id": <id>}`) and nothing changed: never retry that with `force`, '
        "which would take back the decision before it"
    }
}
# The queue never serves either, so only a hand-written request meets this (#83).
# A merged-away contact answers the Contacts routes' own 409 body, naming the
# survivor to retry against.
NOT_IN_QUEUE: Responses = {
    409: {
        "description": 'The contact is archived (`{"detail": "archived"}`) or merged into '
        'another (`{"detail": "merged", "merged_into_id": <survivor>}`)'
    }
}
COUNT_CHANGED: Responses = {409: {"description": "The suggestion matches a different count now"}}
NO_SUCH_SUGGESTION: Responses = {404: {"description": "No suggestion by that key"}}
ALREADY_DECIDED: Responses = {
    422: {"description": "A batch cannot be pointed at contacts somebody has answered for"}
}

States = Annotated[
    list[ContactMet] | None,
    Query(
        description=(
            "The met states the queue holds. Defaults to `unknown`; pass `skip` to revisit, "
            "or both to walk the two together. With `decided_by=automatic` and no states, "
            "the queue defaults to everything a batch can have decided."
        )
    ),
]
DecidedBy = Annotated[
    MetSource | None,
    Query(
        description=(
            "Narrow the queue to the contacts whose current `met` was decided this way. "
            "`automatic` is the review pass: what netkeeper decided for you, on the same "
            "cards, so you can check it. Deciding one by hand makes it `manual` and takes "
            "it out of that queue."
        )
    ),
]
Limit = Annotated[int, Query(ge=1, le=200, description="Contacts per page of a preview.")]
Offset = Annotated[int, Query(ge=0, description="How many contacts to skip.")]
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
    except service.NotInQueue as exc:
        if exc.survivor_id is None:
            raise ApiError(409, {"detail": "archived"}) from exc
        raise ApiError(409, {"detail": "merged", "merged_into_id": exc.survivor_id}) from exc
    except service.NothingToUndo as exc:
        raise HTTPException(status_code=404, detail="nothing to undo") from exc
    except service.UndoRaced as exc:
        # Not the conflict's shape: that one is answered with `force`, and forcing
        # past a race takes back the decision before the one that was meant (#222).
        raise ApiError(
            409, {"detail": str(exc), "reason": "raced", "decision_id": exc.decision_id}
        ) from exc
    except (service.UndoConflict, service.CountChanged) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except service.AlreadyDecided as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.InvalidDecision as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/triage/next", operation_id="get_next_triage_contact")
def get_next_triage_contact(
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
    after_id: AfterId = None,
    prefetch: Prefetch = True,
    decided_by: DecidedBy = None,
) -> TriageQueueOut:
    """The next contact to triage with its evidence, the one after it, and the progress.

    `after_id` moves on without deciding (the `→` key). Both cards are `null`
    when the queue is empty. `decided_by=automatic` serves the review pass: the
    contacts a batch decided and nobody has corrected.
    """
    wanted = _states(states, decided_by)
    with translate_errors():
        card = service.next_card(
            session, user, states=wanted, after_id=after_id, decided_by=decided_by
        )
        following = (
            service.next_card(
                session, user, states=wanted, after_id=card.contact.id, decided_by=decided_by
            )
            if prefetch and card is not None
            else None
        )
        counters = service.progress(session, user, states=wanted, decided_by=decided_by)
    return TriageQueueOut(
        card=_card_out(card), next=_card_out(following), progress=_progress_out(counters)
    )


@router.get(
    "/triage/contacts/{contact_id}",
    operation_id="get_triage_contact",
    responses={404: {"description": "No such contact, or not in the queue being served"}},
)
def get_triage_contact(
    contact_id: int,
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
    decided_by: DecidedBy = None,
) -> TriageCardOut:
    """One contact's card, to triage them next without deciding the ones before them.

    The jump. It holds to the queue's own rules: the contact must be live and in
    one of `states` (and `decided_by`, for the review pass), so a contact waiting
    in another queue answers `404`. Nothing is written.
    """
    wanted = _states(states, decided_by)
    with translate_errors():
        card = service.card_for(session, user, contact_id, states=wanted, decided_by=decided_by)
    out = _card_out(card)
    assert out is not None
    return out


@router.post(
    "/triage/decisions",
    operation_id="decide_triage",
    status_code=201,
    responses={**NO_SUCH_CONTACT, **NOT_IN_QUEUE},
)
def decide_triage(
    body: TriageDecisionIn,
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
    decided_by: DecidedBy = None,
) -> TriageDecisionResult:
    """Record `met`, `not_met`, or `skip` on a contact and return the next card with it.

    The response carries the contact after `prefetch_after_id` (or after the one
    just decided), so triaging a run costs one request per contact. Pass the
    `decided_by` the queue is being served with, so a review pass hands back the
    next contact still waiting to be reviewed; the one just decided has left that
    queue, because deciding by hand is what `manual` means.
    """
    wanted = _states(states, decided_by)
    with translate_errors():
        decision = service.decide(session, user, body.contact_id, body.met)
        after = body.prefetch_after_id if body.prefetch_after_id is not None else body.contact_id
        following = service.next_card(
            session, user, states=wanted, after_id=after, decided_by=decided_by
        )
        counters = service.progress(session, user, states=wanted, decided_by=decided_by)
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
    decided_by: DecidedBy = None,
) -> TriageUndoOut:
    """Undo the newest triage action, restoring the previous state exactly.

    A bulk apply is undone as one batch. `409` when the contact changed after the
    decision, so nothing is overwritten silently; `force` in the body restores
    anyway. Pass the `decided_by` the queue is being served with, as the other
    two routes take it: the counters come back describing the queue the caller
    is looking at, and an undo in a review pass puts a contact back into it.
    """
    wanted = _states(states, decided_by)
    with translate_errors():
        undone = service.undo(session, user, force=body.force if body is not None else False)
        card = None if undone.contact is None else service.load_card(session, user, undone.contact)
        counters = service.progress(session, user, states=wanted, decided_by=decided_by)
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
    responses={**NO_SUCH_CONTACT, **NOT_IN_QUEUE},
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

    Strongest evidence first, and the batch that assumes the most last. A
    suggestion that matches nobody is left out, so the banner shows only when
    there is something to accept. Every count is against the queue as it stands,
    so accepting one batch shrinks the rest. Nothing is applied until `apply`.
    """
    with translate_errors():
        found = service.suggestions(session, user, states=_states(states))
    return [TriageSuggestionOut.model_validate(item) for item in found]


@router.get(
    "/triage/suggestions/{key}/contacts",
    operation_id="list_triage_suggestion_contacts",
    responses={**NO_SUCH_SUGGESTION, **ALREADY_DECIDED},
)
def list_triage_suggestion_contacts(
    key: str,
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
    limit: Limit = service.SUGGESTION_PAGE,
    offset: Offset = 0,
) -> TriageSuggestionPage:
    """Who a suggestion covers, a page at a time, in the order the queue holds them.

    The preview a count alone cannot give: a batch that decides hundreds of
    people at once should be readable as a list of names before anyone says yes.
    Writes nothing, and the page is served from the same query the apply uses,
    so what is shown is what would be decided.
    """
    try:
        page, total = service.suggestion_contacts(
            session, user, key, states=_states(states), limit=limit, offset=offset
        )
    except service.AlreadyDecided as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.InvalidDecision as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return TriageSuggestionPage(
        items=[TriageContactOut.model_validate(contact) for contact in page],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/triage/suggestions/{key}/apply",
    operation_id="apply_triage_suggestion",
    responses={**NO_SUCH_SUGGESTION, **COUNT_CHANGED, **ALREADY_DECIDED},
)
def apply_triage_suggestion(
    key: str,
    body: TriageSuggestionApplyIn,
    user: CurrentUser,
    session: SessionDep,
    states: States = None,
) -> TriageSuggestionApplyOut:
    """Apply a bulk suggestion as one batch that a single undo takes back.

    `expected_count` is required and is the count the banner showed: a set that
    has moved on since answers `409` rather than touching more people than the
    banner named, and there is no form of this request that skips the guard.
    Every contact it touches is left marked `automatic`, so `/triage/next` with
    `decided_by=automatic` serves exactly this batch's work back for review.

    A batch only ever reaches contacts nobody has answered for, so `states`
    outside `unknown` and `skip` answers `422` and writes nothing.
    """
    wanted = _states(states)
    try:
        applied = service.apply_suggestion(
            session, user, key, states=wanted, expected_count=body.expected_count
        )
    except service.AlreadyDecided as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.InvalidDecision as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.CountChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    counters = service.progress(session, user, states=wanted)
    return TriageSuggestionApplyOut(
        key=applied.key,
        applied=applied.applied,
        batch_id=applied.batch_id,
        met=applied.met,
        progress=_progress_out(counters),
    )


def _states(
    states: Sequence[ContactMet] | None, decided_by: MetSource | None = None
) -> Sequence[ContactMet]:
    """What the client asked for, or the default for the queue it is asking for.

    The review pass covers every state a batch can leave behind, so asking for
    it without naming states would otherwise serve the untriaged, which a batch
    never decides, and the queue would always be empty.
    """
    if states:
        return list(states)
    if decided_by is MetSource.AUTOMATIC:
        return service.REVIEW_QUEUE_STATES
    return service.DEFAULT_QUEUE_STATES


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
            worked_together=[OverlapOut.model_validate(item) for item in evidence.worked_together],
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
        automatic=counters.automatic,
    )
