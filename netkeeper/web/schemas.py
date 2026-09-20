"""Pydantic response models for the API. The TypeScript client is generated from these."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from netkeeper.models import ContactSource, InteractionKind, UserKind
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
