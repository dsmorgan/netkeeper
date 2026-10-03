"""``/templates``: message templates, their lint, and previews (spec 8.5, 11.1; item P3-03).

A template that is not the current user's answers ``404``, never a status that
would confirm the id exists for someone else. Lint runs on every save and
never blocks one; it blocks activation instead
(:func:`netkeeper.campaigns.templates.activation_errors`). The ``me.<key>``
fields a template may name come from ``[me]`` in the config the app started
with.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from netkeeper.campaigns import templates as service
from netkeeper.campaigns.render import LintIssue, TemplateRenderError, me_fields
from netkeeper.config import Settings
from netkeeper.models import Contact, Template
from netkeeper.scoping import get_scoped
from netkeeper.web.deps import CurrentUser, SessionDep, read_only
from netkeeper.web.schemas import (
    LintIssueOut,
    MergeFieldOut,
    MergeFieldsOut,
    TemplateCreate,
    TemplateLintIn,
    TemplateOut,
    TemplatePatch,
    TemplatePreviewOut,
)

router = APIRouter(tags=["templates"])

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such template or contact for this user"}}
CONFLICT: Responses = {
    409: {
        "description": "A template by that name already exists, the template is an older "
        "version, or a campaign uses it"
    }
}
INVALID: Responses = {422: {"description": "A value that cannot be stored or rendered"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions to HTTP statuses: 404, 409, 422."""
    try:
        yield
    except service.TemplateNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        service.DuplicateTemplateName,
        service.TemplateSuperseded,
        service.TemplateInUse,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (service.InvalidTemplateValue, TemplateRenderError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _me(request: Request) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    return me_fields(settings.me)


def _issue_out(issue: LintIssue) -> LintIssueOut:
    return LintIssueOut(
        rule=issue.rule,
        severity=issue.severity,
        part=issue.part,
        message=issue.message,
        field=issue.field,
        line=issue.line,
    )


def _template_out(row: Template, *, current: bool, in_use: bool) -> TemplateOut:
    return TemplateOut(
        id=row.id,
        name=row.name,
        channel=row.channel,
        subject=row.subject,
        body=row.body,
        version=row.version,
        previous_id=row.previous_id,
        current=current,
        in_use=in_use,
        lint=[_issue_out(LintIssue.from_json(item)) for item in row.lint_json],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/templates", operation_id="list_templates")
def list_templates(user: CurrentUser, session: SessionDep) -> list[TemplateOut]:
    """Every template, the newest version of each, by name."""
    rows = service.list_templates(session, user)
    in_use = service.in_use_ids(session, user, [row.id for row in rows])
    return [_template_out(row, current=True, in_use=row.id in in_use) for row in rows]


@router.post(
    "/templates",
    operation_id="create_template",
    status_code=201,
    responses={**CONFLICT, **INVALID},
)
def create_template(
    body: TemplateCreate, request: Request, user: CurrentUser, session: SessionDep
) -> TemplateOut:
    """Save a new template with its lint. Lint errors are reported, not refused."""
    with translate_errors():
        row = service.create_template(
            session,
            user,
            name=body.name,
            channel=body.channel,
            subject=body.subject,
            body=body.body,
            me_keys=_me(request).keys(),
        )
    return _template_out(row, current=True, in_use=False)


@router.post("/templates/lint", operation_id="lint_template", responses=INVALID)
@read_only
def lint_template(body: TemplateLintIn, request: Request, user: CurrentUser) -> list[LintIssueOut]:
    """Lint a template's text without saving it, as a save of it would.

    The editor calls this as you type, so a lint error shows before you save. It
    stores nothing and reads no rows; ``user`` is there so the route authenticates
    like every other.
    """
    with translate_errors():
        issues = service.lint_draft(body.channel, body.subject, body.body, _me(request).keys())
    return [_issue_out(issue) for issue in issues]


@router.get("/templates/merge-fields", operation_id="list_merge_fields", responses=NOT_FOUND)
def list_merge_fields(
    request: Request,
    user: CurrentUser,
    session: SessionDep,
    contact_id: Annotated[
        int | None,
        Query(
            description="A contact to take the example values from. Without one, every "
            "example is an invented placeholder."
        ),
    ] = None,
) -> MergeFieldsOut:
    """Every merge field a template may name, with a description and an example value.

    The list is the one lint checks names against, so the editor's field list
    follows any change to it. The ``me.<key>`` fields include any extra keys
    under ``[me]`` in the config.
    """
    contact = None
    if contact_id is not None:
        contact = get_scoped(session, user, Contact, contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail=f"no contact {contact_id}")
    examples = service.field_examples(_me(request), contact)
    return MergeFieldsOut(
        contact_id=contact_id,
        fields=[
            MergeFieldOut(
                name=item.field.name,
                group=item.field.group,
                description=item.field.description,
                insert=item.field.insert,
                example=item.example,
                example_source=item.source,
            )
            for item in examples
        ],
    )


@router.get("/templates/{template_id}", operation_id="get_template", responses=NOT_FOUND)
def get_template(template_id: int, user: CurrentUser, session: SessionDep) -> TemplateOut:
    """One template, any version."""
    with translate_errors():
        row = service.get_template(session, user, template_id)
        current = not service.is_superseded(session, user, row)
    return _template_out(row, current=current, in_use=service.is_in_use(session, user, row))


@router.patch(
    "/templates/{template_id}",
    operation_id="update_template",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def update_template(
    template_id: int,
    body: TemplatePatch,
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> TemplateOut:
    """Edit a template. Answers with the row that now holds it: the same one, or a new
    version when a campaign uses the old one (spec 8.5). An older version is read-only."""
    subject = body.subject if "subject" in body.model_fields_set else service.UNSET
    with translate_errors():
        row = service.update_template(
            session,
            user,
            template_id,
            me_keys=_me(request).keys(),
            name=body.name,
            channel=body.channel,
            subject=subject,
            body=body.body,
        )
    return _template_out(row, current=True, in_use=service.is_in_use(session, user, row))


@router.delete(
    "/templates/{template_id}",
    operation_id="delete_template",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
)
def delete_template(template_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a template and its earlier versions. Refused for an older version, and while
    a campaign uses any of them."""
    with translate_errors():
        service.delete_template(session, user, template_id)


@router.get(
    "/templates/{template_id}/preview",
    operation_id="preview_template",
    responses={**NOT_FOUND, **INVALID},
)
def preview_template(
    template_id: int,
    contact_id: Annotated[int, Query(description="The contact to render the template for.")],
    request: Request,
    user: CurrentUser,
    session: SessionDep,
) -> TemplatePreviewOut:
    """Render a template for one contact. Missing contact data is a warning on the result;
    ``422`` only for a template that does not compile or reaches past the sandbox."""
    with translate_errors():
        row = service.get_template(session, user, template_id)
        contact = get_scoped(session, user, Contact, contact_id)
        if contact is None:
            raise HTTPException(status_code=404, detail=f"no contact {contact_id}")
        rendered = service.render_preview(row, contact, me=_me(request))
    return TemplatePreviewOut(
        subject=rendered.subject,
        body=rendered.body,
        issues=[_issue_out(issue) for issue in rendered.issues],
    )
