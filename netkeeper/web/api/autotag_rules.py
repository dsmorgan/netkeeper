"""``/autotag-rules``: rule CRUD, ordering, running, and the "matches N" preview (spec 10.3).

Runs happen inside the request: a run is regular expressions over a few
columns in Python, well under a second for ten thousand contacts, and it never
touches the browser, so it is not a task (spec 14.1).
"""

from __future__ import annotations

from fastapi import APIRouter

from netkeeper.crm import tags as service
from netkeeper.web.api.tags import INVALID, NOT_FOUND, translate_errors
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.schemas import (
    AutotagRuleCreate,
    AutotagRuleOut,
    AutotagRulePatch,
    AutotagRulePreviewIn,
    AutotagRulePreviewOut,
    AutotagRuleReorder,
    AutotagRuleRunOut,
)

router = APIRouter(prefix="/autotag-rules", tags=["autotag-rules"])


def _run_out(result: service.RuleRun) -> AutotagRuleRunOut:
    return AutotagRuleRunOut(
        contacts=result.contacts,
        added=result.added,
        removed=result.removed,
        updated=result.updated,
        timeouts=result.timeouts,
    )


@router.get("", operation_id="list_autotag_rules")
def list_autotag_rules(user: CurrentUser, session: SessionDep) -> list[AutotagRuleOut]:
    """Every rule in position order."""
    return [AutotagRuleOut.model_validate(rule) for rule in service.list_rules(session, user)]


@router.post(
    "", operation_id="create_autotag_rule", status_code=201, responses={**NOT_FOUND, **INVALID}
)
def create_autotag_rule(
    body: AutotagRuleCreate, user: CurrentUser, session: SessionDep
) -> AutotagRuleOut:
    """Add a rule at the end of the order. The pattern is validated (syntax, no nested unbounded
    repeat), not run."""
    with translate_errors():
        rule = service.create_rule(
            session, user, body.tag_id, body.field, body.pattern, enabled=body.enabled
        )
    return AutotagRuleOut.model_validate(rule)


@router.post("/reorder", operation_id="reorder_autotag_rules", responses={**NOT_FOUND, **INVALID})
def reorder_autotag_rules(
    body: AutotagRuleReorder, user: CurrentUser, session: SessionDep
) -> list[AutotagRuleOut]:
    """Put the given rules first, in that order; the rest keep their order after them."""
    with translate_errors():
        rules = service.reorder_rules(session, user, body.rule_ids)
    return [AutotagRuleOut.model_validate(rule) for rule in rules]


@router.post("/run", operation_id="run_autotag_rules", responses=INVALID)
def run_autotag_rules(user: CurrentUser, session: SessionDep) -> AutotagRuleRunOut:
    """Apply every enabled rule to every live contact (seeding the defaults on first use)."""
    with translate_errors():
        service.ensure_default_rules(session, user)
        result = service.run_rules(session, user)
    return _run_out(result)


@router.post("/preview", operation_id="preview_autotag_rule", responses=INVALID)
def preview_autotag_rule(
    body: AutotagRulePreviewIn, user: CurrentUser, session: SessionDep
) -> AutotagRulePreviewOut:
    """How many live contacts a pattern matches in a field, and the first ten. Writes nothing."""
    with translate_errors():
        preview = service.preview_matches(session, user, body.field, body.pattern)
    return AutotagRulePreviewOut(
        count=preview.count, contact_ids=list(preview.contact_ids), timeouts=preview.timeouts
    )


@router.patch("/{rule_id}", operation_id="update_autotag_rule", responses={**NOT_FOUND, **INVALID})
def update_autotag_rule(
    rule_id: int, body: AutotagRulePatch, user: CurrentUser, session: SessionDep
) -> AutotagRuleOut:
    """Change a rule's tag, field, pattern, or enabled flag. Fields left out are left alone."""
    with translate_errors():
        rule = service.update_rule(
            session,
            user,
            rule_id,
            tag_id=body.tag_id,
            field=body.field,
            pattern=body.pattern,
            enabled=body.enabled,
        )
    return AutotagRuleOut.model_validate(rule)


@router.delete(
    "/{rule_id}", operation_id="delete_autotag_rule", status_code=204, responses=NOT_FOUND
)
def delete_autotag_rule(rule_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a rule. Its assignments stay until the next run finds no rule for their tag."""
    with translate_errors():
        service.delete_rule(session, user, rule_id)


@router.post("/{rule_id}/run", operation_id="run_autotag_rule", responses=NOT_FOUND)
def run_autotag_rule(rule_id: int, user: CurrentUser, session: SessionDep) -> AutotagRuleRunOut:
    """Reconcile the tag this rule feeds, using every enabled rule for that tag."""
    with translate_errors():
        result = service.run_rule(session, user, rule_id)
    return _run_out(result)
