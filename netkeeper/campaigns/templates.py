"""Message templates: storage, versions, the activation gate, and previews (spec 8.5, 11.1; P3-03).

What this module decides
------------------------
- A template's lint runs on every save and is stored in ``lint_json`` for the
  editor. It never blocks a save: a draft with an undefined variable is a
  draft. It blocks activation instead, through :func:`activation_errors`,
  which lints again rather than trusting ``lint_json``, because the rules can
  change after the save: a template saved with a ``me.*`` field before #342
  removed them has clean stored lint and an error now.
- Versions (spec 8.5). The newest row of a ``previous_id`` chain is the
  template; :func:`list_templates` shows only those. Editing one that
  :func:`is_in_use` adds a new row pointing back at it, so a campaign keeps the
  text it was approved with until someone upgrades it; editing one that is not
  in use changes it in place. In use means named by a step of a campaign past
  ``draft`` (:data:`IN_USE_STATUSES`). An older version is kept for the
  campaigns that use it and is read-only: editing or deleting it is
  :class:`TemplateSuperseded`.
- Names are unique among the user's current templates. Older versions share
  their template's name, which is why that is not a database constraint.
- Deleting a template deletes its whole chain, and is refused while any
  version is in use or named by any campaign, a draft's included.
- :func:`render_preview` renders one template for one contact. Missing contact
  data is a warning on the result, never an exception.

Every function that writes needs a writer session (CLAUDE.md): each reads
first, and on SQLite an unmarked read-then-write can fail with "database is
locked".
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from sqlalchemy import ColumnElement, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from netkeeper.campaigns.render import (
    FieldGroup,
    LintIssue,
    MergeField,
    MergeValues,
    Rendered,
    has_errors,
    lint,
    merge_fields,
    placeholder_example,
    render,
)
from netkeeper.crm.positions import last_position_change
from netkeeper.db import is_writer
from netkeeper.localtime import local_today
from netkeeper.models import (
    TEMPLATE_NAME_MAX_LENGTH,
    TEMPLATE_SUBJECT_MAX_LENGTH,
    Campaign,
    CampaignStatus,
    CampaignStep,
    Contact,
    Template,
    TemplateChannel,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped

log = logging.getLogger(__name__)

TEMPLATE_BODY_MAX_LENGTH: Final = 20_000


# --- errors -----------------------------------------------------------------


class TemplateServiceError(Exception):
    """Base of everything this module raises on purpose."""


class TemplateNotFound(TemplateServiceError, LookupError):
    """No such template for this user."""


class DuplicateTemplateName(TemplateServiceError, ValueError):
    """The user already has a current template by that name."""


class InvalidTemplateValue(TemplateServiceError, ValueError):
    """A name, subject, or body that cannot be stored."""


class TemplateSuperseded(TemplateServiceError, ValueError):
    """The row is an older version: read-only, kept for the campaigns that use it."""


class TemplateInUse(TemplateServiceError, ValueError):
    """A campaign uses a version of this template, so it cannot be deleted."""


class Unset(enum.Enum):
    """The type of :data:`UNSET`."""

    TOKEN = 0


UNSET: Final = Unset.TOKEN
"""For :func:`update_template`'s ``subject``: "leave it alone", as opposed to ``None``."""


# --- validation ---------------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError("template writes need a writer session; use session_scope(write=True)")


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise InvalidTemplateValue("template name is empty")
    if len(cleaned) > TEMPLATE_NAME_MAX_LENGTH:
        raise InvalidTemplateValue(
            f"template name is longer than {TEMPLATE_NAME_MAX_LENGTH} characters"
        )
    return cleaned


def _clean_subject(subject: str | None) -> str | None:
    """A blank subject is no subject."""
    if subject is None or not subject.strip():
        return None
    if len(subject) > TEMPLATE_SUBJECT_MAX_LENGTH:
        raise InvalidTemplateValue(
            f"subject is longer than {TEMPLATE_SUBJECT_MAX_LENGTH} characters"
        )
    return subject


def _check_body(body: str) -> str:
    if len(body) > TEMPLATE_BODY_MAX_LENGTH:
        raise InvalidTemplateValue(f"body is longer than {TEMPLATE_BODY_MAX_LENGTH} characters")
    return body


def _lint_json(issues: Sequence[LintIssue]) -> list[dict[str, str | int | None]]:
    return [issue.to_json() for issue in issues]


# --- reads ------------------------------------------------------------------


def _current(user: User) -> ColumnElement[bool]:
    """True for a row nothing replaced: the newest version of its template."""
    newer = aliased(Template)
    return ~exists().where(newer.previous_id == Template.id, newer.user_id == user.id)


def list_templates(session: Session, user: User) -> list[Template]:
    """The user's templates, newest version of each, by name."""
    return list(
        session.scalars(scoped(user, Template).where(_current(user)).order_by(Template.name))
    )


def get_template(session: Session, user: User, template_id: int) -> Template:
    """Any version of a template, current or not; a campaign may show an older one."""
    row = get_scoped(session, user, Template, template_id)
    if row is None:
        raise TemplateNotFound(f"no template {template_id}")
    return row


def is_superseded(session: Session, user: User, row: Template) -> bool:
    """True when a newer version replaced ``row``."""
    newer = session.scalars(
        scoped(user, Template).where(Template.previous_id == row.id).limit(1)
    ).first()
    return newer is not None


def versions(session: Session, user: User, row: Template) -> list[Template]:
    """``row`` and every version before it, newest first."""
    chain = [row]
    while chain[-1].previous_id is not None:
        previous = get_scoped(session, user, Template, chain[-1].previous_id)
        if previous is None:
            break
        chain.append(previous)
    return chain


IN_USE_STATUSES: Final[frozenset[CampaignStatus]] = frozenset(CampaignStatus) - {
    CampaignStatus.DRAFT
}
"""The campaign statuses whose templates are frozen: every status past ``draft``.

A campaign in ``reviewing`` is being approved on its rendered previews (spec
11.8), so an edit in place would change what the approval saw; ``paused``
resumes with the same text; ``completed`` and ``archived`` are the record of
what was sent. A ``draft`` campaign is still being written, and sees the
template's edits as they happen.
"""


def in_use_ids(session: Session, user: User, template_ids: Collection[int]) -> set[int]:
    """Which of ``template_ids`` a campaign past ``draft`` sends (:data:`IN_USE_STATUSES`).

    One query for any number of ids, so the template list can say which of its
    rows are in use without a query per row.
    """
    if not template_ids:
        return set()
    statement = (
        scoped(user, CampaignStep)
        .join(Campaign, Campaign.id == CampaignStep.campaign_id)
        .where(
            CampaignStep.template_id.in_(template_ids),
            Campaign.user_id == user.id,
            Campaign.status.in_(IN_USE_STATUSES),
        )
        .with_only_columns(CampaignStep.template_id)
        .distinct()
    )
    return set(session.scalars(statement))


def is_in_use(session: Session, user: User, row: Template) -> bool:
    """Whether a campaign past ``draft`` (:data:`IN_USE_STATUSES`) sends this version (spec 8.5).

    An edit of a version in use makes a new version and leaves this one to the
    campaigns that use it.
    """
    return row.id in in_use_ids(session, user, [row.id])


def is_referenced(session: Session, user: User, row: Template) -> bool:
    """Whether any campaign's step names this version, a draft's included.

    The step's foreign key has no ``ON DELETE`` action, so a version this is
    true of cannot be deleted; :func:`delete_template` asks first so the answer
    is :class:`TemplateInUse` rather than an integrity error.
    """
    statement = scoped(user, CampaignStep).where(CampaignStep.template_id == row.id).limit(1)
    return session.scalars(statement).first() is not None


def _check_name_free(
    session: Session, user: User, name: str, *, except_id: int | None = None
) -> None:
    query = scoped(user, Template).where(_current(user), Template.name == name)
    if except_id is not None:
        query = query.where(Template.id != except_id)
    if session.scalars(query.limit(1)).first() is not None:
        raise DuplicateTemplateName(f"a template named {name!r} already exists")


def lint_draft(channel: TemplateChannel, subject: str | None, body: str) -> list[LintIssue]:
    """Lint text that is not saved yet, exactly as a save of it would (P3-10's editor).

    A blank subject is no subject here too, so the editor shows the lint the saved
    row would carry. Values a save would refuse (too long) raise
    :class:`InvalidTemplateValue`, as the save would.
    """
    cleaned_subject = _clean_subject(subject)
    _check_body(body)
    return lint(channel, cleaned_subject, body)


# --- writes -----------------------------------------------------------------


def create_template(
    session: Session,
    user: User,
    *,
    name: str,
    channel: TemplateChannel,
    subject: str | None,
    body: str,
) -> Template:
    """Store a new template, version 1, with its lint. Lint errors do not block a save."""
    _require_writer(session)
    cleaned_name = _clean_name(name)
    cleaned_subject = _clean_subject(subject)
    _check_body(body)
    _check_name_free(session, user, cleaned_name)
    row = Template(
        user_id=user.id,
        name=cleaned_name,
        channel=channel,
        subject=cleaned_subject,
        body=body,
        lint_json=_lint_json(lint(channel, cleaned_subject, body)),
        version=1,
    )
    session.add(row)
    session.flush()
    return row


def update_template(
    session: Session,
    user: User,
    template_id: int,
    *,
    name: str | None = None,
    channel: TemplateChannel | None = None,
    subject: str | Unset | None = UNSET,
    body: str | None = None,
) -> Template:
    """Edit a template. Returns the row that now holds it.

    That is the same row, changed in place, unless the template :func:`is_in_use`:
    then it is a new row, one version up, pointing back at the old one, which is
    left exactly as it was. ``None`` (or :data:`UNSET` for ``subject``) leaves a
    field alone; ``subject=None`` clears it.
    """
    _require_writer(session)
    row = get_template(session, user, template_id)
    if is_superseded(session, user, row):
        raise TemplateSuperseded(
            f"template {template_id} is an older version; edit the newest one instead"
        )
    new_name = row.name if name is None else _clean_name(name)
    new_channel = row.channel if channel is None else channel
    new_subject = row.subject if isinstance(subject, Unset) else _clean_subject(subject)
    new_body = row.body if body is None else _check_body(body)
    if (new_name, new_channel, new_subject, new_body) == (
        row.name,
        row.channel,
        row.subject,
        row.body,
    ):
        return row
    if new_name != row.name:
        _check_name_free(session, user, new_name, except_id=row.id)
    lint_json = _lint_json(lint(new_channel, new_subject, new_body))

    if is_in_use(session, user, row):
        replacement = Template(
            user_id=user.id,
            name=new_name,
            channel=new_channel,
            subject=new_subject,
            body=new_body,
            lint_json=lint_json,
            version=row.version + 1,
            previous_id=row.id,
        )
        # Two edits of the same in-use version can both pass is_superseded() before either
        # inserts; UNIQUE(user_id, previous_id) lets one win. The savepoint keeps the
        # caller's session usable for the loser, which gets a 409 like any other stale edit.
        try:
            with session.begin_nested():
                session.add(replacement)
                session.flush()
        except IntegrityError as exc:
            raise TemplateSuperseded(
                f"template {template_id} was replaced by another edit just now; "
                "edit the newest version instead"
            ) from exc
        log.info("template %d is in use; saved the edit as version %d", row.id, replacement.version)
        return replacement

    row.name = new_name
    row.channel = new_channel
    row.subject = new_subject
    row.body = new_body
    row.lint_json = lint_json
    session.flush()
    return row


def delete_template(session: Session, user: User, template_id: int) -> None:
    """Delete a template and every earlier version of it.

    Refused for an older version (delete the template through its newest one)
    and while any version is in use or named by a campaign, a draft's included.
    """
    _require_writer(session)
    row = get_template(session, user, template_id)
    if is_superseded(session, user, row):
        raise TemplateSuperseded(
            f"template {template_id} is an older version; delete the newest one instead"
        )
    chain = versions(session, user, row)
    if any(
        is_in_use(session, user, version) or is_referenced(session, user, version)
        for version in chain
    ):
        raise TemplateInUse(f"a campaign uses template {template_id}")
    for version in chain:  # newest first, so nothing is left pointing at a deleted row
        session.delete(version)
        session.flush()


# --- the gate and the preview -------------------------------------------------------


def activation_errors(row: Template) -> list[LintIssue]:
    """The lint errors that keep this template out of an active campaign; empty when none.

    Lints the text again rather than reading ``lint_json``: the rules may have
    changed since the save, as they did when #342 removed the ``me.*`` fields.
    Campaign activation (P3-06 and later) calls this.
    """
    issues = lint(row.channel, row.subject, row.body)
    return issues if has_errors(issues) else []


def _whole_years(since: date, today: date) -> int:
    years = today.year - since.year
    if (today.month, today.day) < (since.month, since.day):
        years -= 1
    return max(years, 0)


def contact_fields(contact: Contact, today: date) -> dict[str, object]:
    """A contact's merge values (spec 11.1), keyed by
    :data:`~netkeeper.campaigns.render.CONTACT_FIELDS`.

    ``first_name`` is the preferred name, falling back to the first name.
    ``last_position_change`` is the latest start or end date on or before
    ``today`` among the contact's positions: leaving a job is a change just as
    starting one is (#232), and neither an announced departure nor an announced
    new job has happened yet (#255). ``years_since_connected`` counts whole
    years to ``today``.
    """
    values: dict[str, object] = {
        "first_name": contact.preferred_name or contact.first_name,
        "last_name": contact.last_name,
        "company": contact.current_company,
        "title": contact.current_title,
        "location": contact.location,
        "connected_year": None if contact.connected_on is None else contact.connected_on.year,
        "years_since_connected": (
            None if contact.connected_on is None else _whole_years(contact.connected_on, today)
        ),
        "last_position_change": last_position_change(contact, today),
    }
    return values


def render_preview(
    row: Template,
    contact: Contact,
    *,
    today: date | None = None,
    timezone: str = "UTC",
    personal_line: str | None = None,
) -> Rendered:
    """Render ``row`` for ``contact``, outside any campaign.

    The campaign fields (``campaign.name``, ``step.number``,
    ``previous_send_date``) have no value in a preview, so a template that
    names them gets a missing-value warning for each, as it does for missing
    contact data. Raises :class:`~netkeeper.campaigns.render.TemplateRenderError`
    only for a template that does not compile or reaches past the sandbox.

    ``today`` defaults to the day it is in ``timezone`` (the user's), as the engine
    counts it, so the date fields match what a send would render.
    """
    if row.user_id != contact.user_id:
        raise ValueError("a template can only be previewed against its own user's contact")
    day = local_today(timezone, utcnow()) if today is None else today
    values = MergeValues(contact=contact_fields(contact, day), personal_line=personal_line)
    return render(row.channel, row.subject, row.body, values, today=day)


# --- the editor's field list --------------------------------------------------------


class ExampleSource(enum.StrEnum):
    """Where a field's example value came from."""

    CONTACT = "contact"
    PLACEHOLDER = "placeholder"


@dataclass(frozen=True, slots=True)
class FieldExample:
    """A merge field with an example value. ``example`` is ``None`` when the picked contact
    has no value for it: the field would render empty."""

    field: MergeField
    example: str | None
    source: ExampleSource


def field_examples(
    contact: Contact | None = None,
    *,
    today: date | None = None,
    timezone: str = "UTC",
) -> list[FieldExample]:
    """Every merge field (:func:`~netkeeper.campaigns.render.merge_fields`) with an example.

    With no ``contact``, every example is an invented placeholder, never anyone's
    data. With one, the contact fields show that contact's values, as a preview
    for that contact would render them. ``personal_line`` and the campaign fields
    have no value outside a campaign, so they keep their placeholders either way.
    """
    day = local_today(timezone, utcnow()) if today is None else today
    values = None if contact is None else contact_fields(contact, day)
    out: list[FieldExample] = []
    for item in merge_fields():
        if values is not None and item.group is FieldGroup.CONTACT:
            out.append(FieldExample(item, _shown(values.get(item.name)), ExampleSource.CONTACT))
        else:
            out.append(
                FieldExample(item, placeholder_example(item.name), ExampleSource.PLACEHOLDER)
            )
    return out


def _shown(value: object) -> str | None:
    """A value as the render prints it, or ``None`` when it would render empty."""
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
