"""Pydantic response models for the API. The TypeScript client is generated from these."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from netkeeper.crm.tags import PATTERN_MAX_LENGTH, InvalidPattern, compile_pattern
from netkeeper.models import (
    TAG_NAME_MAX_LENGTH,
    ContactSource,
    InteractionKind,
    RuleField,
    TagKind,
    TagSource,
    UserKind,
)
from netkeeper.services.tasks import TaskStatus


class HealthOut(BaseModel):
    status: Literal["ok"]
    version: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    kind: UserKind
    display_name: str | None
    email: str | None
    timezone: str


class TaskAccepted(BaseModel):
    """The ``202`` body of every route that enqueues work and returns."""

    task_id: str


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: TaskStatus
    error: str | None
    created_at: datetime
    finished_at: datetime | None


# --- interactions, timeline, notes (P1-10) ----------------------------------


class InteractionIn(BaseModel):
    """A new interaction. ``at`` must carry a timezone; it is stored as UTC."""

    kind: InteractionKind
    at: AwareDatetime
    summary: str | None = None
    message_id: int | None = None


class InteractionPatch(BaseModel):
    """Fields to change on an interaction; a field left out is untouched.

    ``summary`` and ``message_id`` sent as ``null`` are cleared. ``kind`` and
    ``at`` cannot be null, so ``null`` means "leave it".
    """

    kind: InteractionKind | None = None
    at: AwareDatetime | None = None
    summary: str | None = None
    message_id: int | None = None


class InteractionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    kind: InteractionKind
    at: datetime
    summary: str | None
    message_id: int | None
    source: ContactSource
    created_at: datetime
    updated_at: datetime


class InteractionPage(BaseModel):
    """One page of a contact's interactions, newest first, and the total count."""

    items: list[InteractionOut]
    total: int


class SnapshotOut(BaseModel):
    """The headline and job as they were at ``observed_at`` (spec 8.1)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    observed_at: datetime
    headline: str | None
    current_title: str | None
    current_company: str | None
    location: str | None
    source: ContactSource


class TimelineInteraction(BaseModel):
    kind: Literal["interaction"]
    at: datetime
    interaction: InteractionOut


class TimelineSnapshot(BaseModel):
    kind: Literal["snapshot"]
    at: datetime
    snapshot: SnapshotOut


TimelineEntryOut = Annotated[TimelineInteraction | TimelineSnapshot, Field(discriminator="kind")]


class TimelinePage(BaseModel):
    """Interactions and snapshots interleaved, newest first.

    ``next_before`` is the ``before`` for the next page, or ``null`` when this is
    the last one. A page may run past ``limit`` when entries share a timestamp
    at its end, so a cursor never skips one.
    """

    items: list[TimelineEntryOut]
    next_before: datetime | None


class NotesIn(BaseModel):
    """The contact's notes, Markdown, replaced whole; ``null`` clears them."""

    notes: str | None


class NotesOut(BaseModel):
    contact_id: int
    notes: str | None
    updated_at: datetime


# --- tags and auto-tag rules (spec 8.3, 10.3) --------------------------------

TagName = Annotated[str, Field(min_length=1, max_length=TAG_NAME_MAX_LENGTH)]
HexColor = Annotated[str, Field(pattern=r"^#[0-9a-fA-F]{6}$")]


def _valid_pattern(pattern: str) -> str:
    """A rule pattern must compile and be free of nested unbounded repeats (``(a+)+``), which
    can backtrack for minutes inside a writer transaction; the service checks again when it
    stores or previews one, so the two can never disagree."""
    try:
        compile_pattern(pattern)
    except InvalidPattern as exc:
        raise ValueError(str(exc)) from None
    return pattern


RulePattern = Annotated[
    str, Field(min_length=1, max_length=PATTERN_MAX_LENGTH), AfterValidator(_valid_pattern)
]


class TagOut(BaseModel):
    id: int
    name: str
    color: str | None
    kind: TagKind
    contact_count: int
    """Live contacts (not merged away, not archived) carrying the tag."""
    created_at: datetime
    updated_at: datetime


class TagCreate(BaseModel):
    name: TagName
    color: HexColor | None = None
    kind: TagKind = TagKind.MANUAL


class TagPatch(BaseModel):
    """Fields left out are left alone; ``color: null`` clears the color."""

    name: TagName | None = None
    color: HexColor | None = None


class ContactTagCreate(BaseModel):
    tag_id: int


class ContactTagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    tag_id: int
    source: TagSource
    rule_id: int | None
    created_at: datetime


class AutotagRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tag_id: int
    field: RuleField
    pattern: str
    enabled: bool
    position: int
    created_at: datetime
    updated_at: datetime


class AutotagRuleCreate(BaseModel):
    tag_id: int
    field: RuleField
    pattern: RulePattern
    enabled: bool = True


class AutotagRulePatch(BaseModel):
    tag_id: int | None = None
    field: RuleField | None = None
    pattern: RulePattern | None = None
    enabled: bool | None = None


class AutotagRuleReorder(BaseModel):
    """``rule_ids`` go first, in this order; the rules left out keep their order after them."""

    rule_ids: list[int] = Field(min_length=1)


class AutotagRulePreviewIn(BaseModel):
    field: RuleField
    pattern: RulePattern


class AutotagRulePreviewOut(BaseModel):
    count: int
    contact_ids: list[int]
    """The first matching contacts, by id, up to ten."""
    timeouts: int
    """Contacts the pattern timed out on (50 ms), counted as no match."""


class AutotagRuleRunOut(BaseModel):
    contacts: int
    added: int
    removed: int
    updated: int
    timeouts: int
    """Searches that hit the 50 ms timeout, each treated as no match."""
