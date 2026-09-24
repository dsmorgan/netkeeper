"""Pydantic response models for the API. The TypeScript client is generated from these."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self, get_args

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from netkeeper.crm.confirmation import InvalidReason
from netkeeper.crm.filters import FilterTree, SortKey
from netkeeper.crm.importer import ImportField
from netkeeper.crm.interactions import TimelineEntry
from netkeeper.crm.lists import MAX_COLUMNS
from netkeeper.crm.tags import PATTERN_MAX_LENGTH, InvalidPattern, compile_pattern
from netkeeper.linkedin.archive import ArchiveRefusalCode
from netkeeper.models import (
    LIST_NAME_MAX_LENGTH,
    TAG_NAME_MAX_LENGTH,
    VIEW_NAME_MAX_LENGTH,
    ContactMet,
    ContactSnapshot,
    ContactSource,
    EmailKind,
    EmailStatus,
    ImportDecisionKind,
    ImportResolution,
    ImportSourceKind,
    ImportStatus,
    Interaction,
    InteractionKind,
    LinkKind,
    ListKind,
    MetSource,
    PhoneKind,
    RuleField,
    SyncRunKind,
    SyncRunStatus,
    SyncRunTrigger,
    TagKind,
    TagMetSignal,
    TagSource,
    TriageDecisionKind,
    UserKind,
)
from netkeeper.models.imports import FILENAME_MAX_LENGTH, PRESET_NAME_MAX_LENGTH
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
    """Verbatim, exactly as stored, and for a message that is someone else's words.

    The archive importer stores LinkedIn message bodies here as plain text
    (#75): InMail arrives as HTML, and the import path unescapes entities and
    strips tags before anything is written, rather than trusting whatever
    renders this to clean it. The API itself still escapes nothing and strips
    nothing further on the way out — a person's own hand-entered interaction
    can carry any text they typed, angle brackets included — so
    **whatever renders this must still escape it**. Never `dangerouslySetInnerHTML`,
    and no Markdown renderer with HTML passthrough.
    ``tests/test_web_triage.py::test_message_summaries_and_notes_come_back_verbatim``
    pins the contract, so no renderer can assume a value here is free of markup.
    """
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


def timeline_entry_out(entry: TimelineEntry) -> TimelineEntryOut:
    """One :class:`~netkeeper.crm.interactions.TimelineEntry` as the tagged union above.

    The timeline endpoint, the contact detail, and the triage card all render
    entries with it, so they can never drift.
    """
    if isinstance(entry.row, Interaction):
        return TimelineInteraction(
            kind="interaction", at=entry.at, interaction=InteractionOut.model_validate(entry.row)
        )
    if isinstance(entry.row, ContactSnapshot):
        return TimelineSnapshot(
            kind="snapshot", at=entry.at, snapshot=SnapshotOut.model_validate(entry.row)
        )
    raise TypeError(f"timeline entry of unexpected type {type(entry.row).__name__}")


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
    met_signal: TagMetSignal | None
    """What carrying this tag says about having met the person (spec 10.2).

    The user's own reading of their own label, and ``null`` until they give one.
    A tag with a signal is offered as a triage batch, previewed and accepted like
    any other; it decides nobody on its own.
    """
    contact_count: int
    """Live contacts (not merged away, not archived) carrying the tag."""
    created_at: datetime
    updated_at: datetime


class TagCreate(BaseModel):
    name: TagName
    color: HexColor | None = None
    kind: TagKind = TagKind.MANUAL
    met_signal: TagMetSignal | None = None


class TagPatch(BaseModel):
    """Fields left out are left alone; ``color: null`` and ``met_signal: null`` clear them."""

    name: TagName | None = None
    color: HexColor | None = None
    met_signal: TagMetSignal | None = None


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
    """Searches that hit the 50 ms timeout; each left its tag as it was."""


# --- contacts (P1-05) --------------------------------------------------------

ContactColumn = Literal[
    "li_urn",
    "li_public_id",
    "li_url",
    "first_name",
    "last_name",
    "preferred_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "connected_on",
    "degree",
    "met",
    "met_source",
    "triaged_at",
    "do_not_contact",
    "do_not_contact_reason",
    "li_missing_count",
    "li_disconnected_at",
    "last_enriched_at",
    "enrich_priority",
    "last_contacted_at",
    "notes",
    "archived_at",
    "source",
    "created_at",
    "updated_at",
]
"""The scalar columns of ``contacts`` a table row may carry, and ``columns`` may name."""

CONTACT_COLUMNS: tuple[str, ...] = get_args(ContactColumn)

ProvenanceField = Literal[
    "li_urn",
    "li_public_id",
    "li_url",
    "first_name",
    "last_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "connected_on",
]
"""The LinkedIn fields with per-field provenance; the same set as ``PROVENANCE_ORDER``."""


class ContactQuery(BaseModel):
    """The body of ``POST /contacts/query``: a filter, a sort, a page, and the columns wanted.

    ``columns`` names the scalar fields each row carries (``id`` always does);
    ``null`` means every one of them. ``primary_email`` and ``primary_phone`` are
    always present.
    """

    model_config = ConfigDict(extra="forbid")

    filter: FilterTree | None = None
    sort: list[SortKey] = Field(default_factory=list)
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)
    columns: list[ContactColumn] | None = None


class ContactRow(BaseModel):
    """One row of the Contacts table (spec 10.1), compact.

    The scalar fields are present when the query asked for them, and every one
    of them when it named no columns; a field that was not asked for is absent
    from the JSON, not ``null``. ``primary_email`` and ``primary_phone`` are the
    primary child, or the first one when none is marked primary.

    Tags are not on a row yet. Carrying them means a third preloaded collection
    per page, and the Contacts table (P1-12) decides whether it wants them as
    names or as ids; until it does, a row that needs them asks
    ``GET /contacts/{id}`` or the tags API.
    """

    id: int
    li_urn: str | None = None
    li_public_id: str | None = None
    li_url: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    headline: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = None
    connected_on: date | None = None
    degree: int | None = None
    met: ContactMet | None = None
    met_source: MetSource | None = None
    """Who decided ``met``: the person, or a triage batch they accepted (spec 10.2)."""
    triaged_at: datetime | None = None
    do_not_contact: bool | None = None
    do_not_contact_reason: str | None = None
    li_missing_count: int | None = None
    li_disconnected_at: datetime | None = None
    last_enriched_at: datetime | None = None
    enrich_priority: int | None = None
    last_contacted_at: datetime | None = None
    notes: str | None = None
    archived_at: datetime | None = None
    source: ContactSource | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    primary_email: str | None
    primary_phone: str | None


class ContactPage(BaseModel):
    """One page of contacts, the count of every match, and a reading of the selection."""

    items: list[ContactRow]
    total: int
    describe: str


class ContactStatsOut(BaseModel):
    """Counts and triage progress over the user's live contacts (spec 10.1).

    ``netkeeper.crm.contacts.contact_stats()`` backs this, ``netkeeper contacts
    stats``, and ``netkeeper.crm.triage.progress()`` all at once, so the CLI,
    this endpoint, and the triage queue's progress bar cannot quietly disagree
    on what "how many contacts" means (#90; they once did).
    """

    model_config = ConfigDict(from_attributes=True)

    total: int
    met: int
    not_met: int
    skipped: int
    untriaged: int
    archived: int
    merged_away: int
    with_email: int
    with_phone: int
    tagged: int
    tagged_by_rule: int
    """A contact with at least one auto-tag-rule-assigned tag; a subset of ``tagged``."""


class ContactEmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    kind: EmailKind
    is_primary: bool
    status: EmailStatus
    source: ContactSource
    observed_at: datetime


class ContactPhoneOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    number_e164: str | None
    raw: str
    kind: PhoneKind
    is_primary: bool
    source: ContactSource
    observed_at: datetime


class ContactLinkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    url: str
    kind: LinkKind
    source: ContactSource
    observed_at: datetime


class ContactPositionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    company: str | None
    company_urn: str | None
    started_on: date | None
    ended_on: date | None
    is_current: bool
    source: ContactSource
    observed_at: datetime


# --- the user's own positions (spec 8.1, P1-26; #84) -------------------------


def _title_or_company(title: str | None, company: str | None) -> None:
    if not (title or "").strip() and not (company or "").strip():
        raise ValueError("a position needs a title or a company")


class UserPositionIn(BaseModel):
    """One stint to add to the user's own job history. Needs a title or a company.

    ``is_current`` left out is inferred: a start with no end is current,
    anything else is not.
    """

    title: str | None = None
    company: str | None = None
    company_urn: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    is_current: bool | None = None

    @model_validator(mode="after")
    def _needs_title_or_company(self) -> Self:
        _title_or_company(self.title, self.company)
        return self


class UserPositionPatch(BaseModel):
    """Change given fields of one of the user's own positions; a field left out is untouched."""

    title: str | None = None
    company: str | None = None
    company_urn: str | None = None
    started_on: date | None = None
    ended_on: date | None = None
    is_current: bool | None = None


class UserPositionOut(BaseModel):
    """One stint of the user's own job history.

    ``source`` is ``manual`` for a row added or edited here, or whatever
    source last wrote it (``archive`` today). A **manual edit wins
    permanently**: once ``source`` is ``manual``, re-importing the LinkedIn
    archive never overwrites this row again, at any date, until it is edited
    or deleted by hand. There is no revert-to-imported-value ledger for this
    table the way `contacts` has for its LinkedIn fields (`synced_values`) --
    the edit is a one-way trip today.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    company: str | None
    company_urn: str | None
    started_on: date | None
    ended_on: date | None
    is_current: bool
    source: ContactSource
    observed_at: datetime
    created_at: datetime
    updated_at: datetime


class SyncedValueOut(BaseModel):
    """What an automated source last reported for one field: what a revert restores."""

    value: str | None
    source: ContactSource
    observed_at: datetime


class ContactDetail(BaseModel):
    """A contact in full: every scalar, its children, and its provenance (spec 8.1, 10.5).

    ``timeline`` is the newest page of interactions and snapshots interleaved,
    the same shape ``GET /contacts/{id}/timeline`` pages through, so the detail
    screen renders in one request; ask that endpoint for older entries.

    ``field_sources`` says which source last wrote each LinkedIn field;
    ``synced_values`` what the automated sources last reported;
    ``overridden_fields`` the ones a manual edit hides a different synced value
    on, each with a revert available. ``resolved_from`` is set when the id asked
    for belongs to a merged-away contact and this is its survivor.
    """

    id: int
    li_urn: str | None
    li_public_id: str | None
    li_url: str | None
    first_name: str
    last_name: str
    preferred_name: str
    headline: str | None
    current_title: str | None
    current_company: str | None
    location: str | None
    connected_on: date | None
    degree: int
    met: ContactMet
    met_source: MetSource
    """Who decided ``met``: the person, or a triage batch they accepted (spec 10.2).

    ``automatic`` is a decision waiting to be reviewed, which a screen showing
    ``met`` should say out loud; the triage review queue serves exactly these.
    """
    triaged_at: datetime | None
    do_not_contact: bool
    do_not_contact_reason: str | None
    li_missing_count: int
    li_disconnected_at: datetime | None
    last_enriched_at: datetime | None
    enrich_priority: int
    last_contacted_at: datetime | None
    notes: str | None
    archived_at: datetime | None
    source: ContactSource
    created_at: datetime
    updated_at: datetime
    merged_into_id: int | None
    emails: list[ContactEmailOut]
    phones: list[ContactPhoneOut]
    links: list[ContactLinkOut]
    positions: list[ContactPositionOut]
    snapshots: list[SnapshotOut]
    timeline: list[TimelineEntryOut]
    field_sources: dict[str, ContactSource]
    synced_values: dict[str, SyncedValueOut]
    overridden_fields: list[str]
    resolved_from: int | None


class ContactPatch(BaseModel):
    """Fields to change on a contact; a field left out is untouched.

    A LinkedIn field (``li_public_id`` through ``connected_on``) sent here is a
    manual override that sticks until reverted; ``null`` clears it and sticks
    the same way. ``li_public_id`` also sets ``li_url`` to the canonical profile
    URL, and is refused when another contact holds the slug. ``preferred_name``
    sent empty or ``null`` falls back to ``first_name``. ``met`` stamps
    ``triaged_at``. ``met`` and ``do_not_contact`` cannot be null, so ``null``
    means "leave it".
    """

    model_config = ConfigDict(extra="forbid")

    li_public_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = None
    connected_on: date | None = None
    preferred_name: str | None = None
    notes: str | None = None
    met: ContactMet | None = None
    do_not_contact: bool | None = None
    do_not_contact_reason: str | None = None


class RevertFieldIn(BaseModel):
    """The field to put back to its last synced value (spec 10.5, CP1 #28)."""

    field: ProvenanceField


class MergeIn(BaseModel):
    """Fold ``loser_id`` into the contact in the path (spec 8.2)."""

    loser_id: int


class MergedConflict(BaseModel):
    """The ``409`` body of a write to a merged-away contact: where it went."""

    detail: Literal["merged"]
    merged_into_id: int


BulkAction = Literal["set_met", "archive", "unarchive", "set_do_not_contact"]


class BulkSelection(BaseModel):
    """Which contacts a bulk action applies to: a filter, or explicit ids (one of the two).

    A filter selects what the Contacts table shows for it: live contacts, and
    archived ones only with ``include_archived``. Ids select those contacts,
    archived or not; a merged-away id is never selected.
    """

    model_config = ConfigDict(extra="forbid")

    filter: FilterTree | None = None
    ids: list[int] | None = Field(None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.filter is None) == (self.ids is None):
            raise ValueError("give exactly one of filter, ids")
        return self


class BulkConfirmable(BaseModel):
    """Everything a confirmation token binds: the selection and exactly what would be written.

    ``BulkCountIn`` and ``BulkIn`` are this plus nothing and this plus the
    token, so the two cannot drift: a field the count does not see is a field
    the action could change after the person confirmed it.
    """

    model_config = ConfigDict(extra="forbid")

    selection: BulkSelection
    action: BulkAction
    value: ContactMet | bool | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def _value_fits_action(self) -> Self:
        match self.action:
            case "set_met":
                if not isinstance(self.value, ContactMet):
                    raise ValueError("set_met needs value: one of unknown, met, not_met, skip")
            case "set_do_not_contact":
                if not isinstance(self.value, bool):
                    raise ValueError("set_do_not_contact needs value: true or false")
            case "archive" | "unarchive":
                if self.value is not None or self.reason is not None:
                    raise ValueError(f"{self.action} takes no value or reason")
        if self.reason is not None and self.action != "set_do_not_contact":
            raise ValueError("reason goes with set_do_not_contact only")
        return self


class BulkCountIn(BulkConfirmable):
    """Ask how many contacts an action would touch, and for the token that confirms it.

    It carries the ``value`` and ``reason`` the action would write, not only the
    selection, because those are part of what the person confirms: "mark 214
    people do-not-contact" and "clear do-not-contact on 214 people" are two
    different sentences at the same count.
    """


class BulkCountOut(BaseModel):
    """The count to put in the confirmation dialog, and the token that makes it binding.

    Send ``token`` back with the action. It is bound to this user, this action,
    this selection, and this count, and it expires at ``expires_at`` (five
    minutes). ``describe`` reads the selection back in words for the dialog.
    """

    count: int
    describe: str
    token: str
    expires_at: datetime


class BulkIn(BulkConfirmable):
    """A bulk action and the confirmation token that carries the count (spec 10.1).

    ``token`` comes from ``POST /contacts/bulk/count`` and must have been issued
    for this same selection, action, value and reason. The server re-counts the
    selection inside the writer transaction and refuses with ``409`` when the
    count has moved, so an action never lands on rows the person did not see.
    ``value`` is the ``met`` value for ``set_met`` and a boolean for
    ``set_do_not_contact`` (with ``reason``); ``archive`` and ``unarchive`` take
    none.
    """

    token: str = Field(min_length=1, description="The count confirmation token.")


class BulkOut(BaseModel):
    affected: int


class CountMismatch(BaseModel):
    """The ``409`` body of a bulk action whose selection no longer counts what the UI showed."""

    detail: Literal["count mismatch"]
    expected_count: int
    actual_count: int


class ConfirmationRejected(BaseModel):
    """The body of a bulk action whose confirmation token does not hold up.

    ``409`` when the token expired (ask for the count again); ``422`` when it is
    unreadable, was issued to someone else, or is for another action or another
    selection.
    """

    detail: str
    reason: InvalidReason


class ContactEmailIn(BaseModel):
    """A new address. The first address on a contact becomes primary whether or not asked."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1)
    kind: EmailKind = EmailKind.OTHER
    is_primary: bool = False
    status: EmailStatus = EmailStatus.OK


class ContactEmailPatch(BaseModel):
    """Fields to change on an address; ``is_primary: true`` demotes the others."""

    model_config = ConfigDict(extra="forbid")

    email: str | None = Field(None, min_length=1)
    kind: EmailKind | None = None
    is_primary: bool | None = None
    status: EmailStatus | None = None


class ContactPhoneIn(BaseModel):
    """A new number. ``number_e164`` is derived from a ``+``-prefixed ``raw`` when not given."""

    model_config = ConfigDict(extra="forbid")

    raw: str = Field(min_length=1)
    number_e164: str | None = None
    kind: PhoneKind = PhoneKind.OTHER
    is_primary: bool = False


class ContactPhonePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: str | None = Field(None, min_length=1)
    number_e164: str | None = None
    kind: PhoneKind | None = None
    is_primary: bool | None = None


class ContactLinkIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    kind: LinkKind = LinkKind.OTHER


class ContactLinkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(None, min_length=1)
    kind: LinkKind | None = None


def given_fields(body: BaseModel) -> dict[str, Any]:
    """The fields a patch body carried, by name, ``null`` included: what to change."""
    return {name: getattr(body, name) for name in body.model_fields_set}


# --- imports: mapping, review, commit, rollback (spec 10.5) ------------------

# A CSV is sent as text rather than as a multipart upload: the API is local and
# single-user, and this keeps the request one JSON body the generated client
# already knows how to make. The cap is far above a LinkedIn export of a large
# network (a few megabytes) and stops a mistyped request from being read at all.
MAX_IMPORT_CHARS = 32_000_000

ImportPresetName = Annotated[str, Field(min_length=1, max_length=PRESET_NAME_MAX_LENGTH)]
ImportFilename = Annotated[str, Field(min_length=1, max_length=FILENAME_MAX_LENGTH)]
ImportContent = Annotated[str, Field(max_length=MAX_IMPORT_CHARS)]
ColumnMapping = dict[str, str]
"""Column header to :class:`~netkeeper.crm.importer.ImportField` value; ``""`` unmaps it."""


class ImportInspectIn(BaseModel):
    """A file to read the header of, with the mapping to try on it. Nothing is stored."""

    content: ImportContent
    preset: ImportPresetName | None = None
    mapping: ColumnMapping | None = None


class ImportInspectOut(BaseModel):
    """A file's columns and the mapping chosen for them, for the mapping screen."""

    headers: list[str]
    row_count: int
    preamble_rows: int
    """Lines above the header, as the LinkedIn archive's "Notes:" block."""
    detected_preset: str | None
    """The built-in preset that best fits the header, whether or not it was used."""
    preset: str | None
    mapping: dict[str, ImportField]
    unmapped: list[str]
    sample: list[dict[str, str]]
    """The first rows as they were read, so the screen can show the mapping's effect."""


class ImportRunCreate(ImportInspectIn):
    """A file to read into a draft run."""

    filename: ImportFilename
    source_kind: ImportSourceKind = ImportSourceKind.CSV


class ImportRunOut(BaseModel):
    id: int
    source_kind: ImportSourceKind
    filename: str
    preset: str | None
    mapping: dict[str, str]
    status: ImportStatus
    total_rows: int
    matched_count: int
    created_count: int
    candidate_count: int
    skipped_count: int
    tagged_contacts: int
    """Contacts the auto-tag rules looked at when this run committed (#64)."""
    tags_added: int
    tags_removed: int
    committed_at: datetime | None
    rolled_back_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ImportRunPage(BaseModel):
    items: list[ImportRunOut]
    total: int


class ImportDecisionOut(BaseModel):
    kind: ImportDecisionKind
    contact_id: int | None


class ImportRefusedOut(BaseModel):
    """A value an import was not allowed to write, and what outranked it (spec 10.5)."""

    field: str
    incoming: str | None
    kept: str | None
    source: ContactSource


class ImportRowOut(BaseModel):
    id: int
    row_number: int
    raw: dict[str, str]
    resolution: ImportResolution
    contact_id: int | None
    matched_by: str | None
    candidate_ids: list[int]
    decision: ImportDecisionOut | None
    refused: list[ImportRefusedOut]
    """Fields the row carried that provenance kept out; empty until the run is committed."""
    error: str | None


class ImportRowPage(BaseModel):
    items: list[ImportRowOut]
    total: int


class ImportChangeOut(BaseModel):
    """One field a row would write, with the value that is there now."""

    field: str
    before: str | None
    after: str | None
    refused: bool
    kept_source: ContactSource | None
    """What refused the row; ``manual`` is a person's own edit, which no import overwrites."""


class ImportPreviewRow(BaseModel):
    """One row of the review screen (spec 10.5 step 3)."""

    row_number: int
    raw: dict[str, str]
    resolution: ImportResolution
    contact_id: int | None
    """The contact a matched row lands on; null for a candidate or a new contact."""
    matched_by: str | None
    candidate_ids: list[int]
    changes: list[ImportChangeOut]
    """For a candidate these are measured against the first of ``candidate_ids``."""
    problem: str | None


class ImportDecisionIn(BaseModel):
    """What to do with one candidate row. ``merge_into`` needs the contact to merge into."""

    row_number: int
    kind: ImportDecisionKind
    contact_id: int | None = None

    @model_validator(mode="after")
    def _merge_needs_a_contact(self) -> ImportDecisionIn:
        if self.kind is ImportDecisionKind.MERGE_INTO and self.contact_id is None:
            raise ValueError("a merge_into decision needs contact_id")
        return self


class ImportCommitIn(BaseModel):
    """Decisions to record, then apply the whole run in one transaction."""

    decisions: list[ImportDecisionIn] = Field(default_factory=list)
    skip_undecided: bool = False
    """Skip candidate rows nobody decided instead of refusing the commit."""


class ImportRollbackOut(BaseModel):
    """What undoing a run removed and put back."""

    run_id: int
    contacts_deleted: int
    contacts_restored: int
    fields_restored: int
    children_deleted: int


class ImportPresetOut(BaseModel):
    name: str
    builtin: bool
    mapping: dict[str, str]
    """For a built-in preset the headers it recognizes; for a saved one its own columns."""


class ImportPresetsOut(BaseModel):
    """The presets on offer. Built-in ones are the same for everyone; saved ones are yours."""

    builtin: list[ImportPresetOut]
    saved: list[ImportPresetOut]


class ImportPresetIn(BaseModel):
    mapping: ColumnMapping = Field(min_length=1)


# --- archive import through the API (spec 10.5, 14.1; P1-20) ----------------


class ArchiveConnectionCountsOut(BaseModel):
    """What ``Connections.csv`` did (:class:`netkeeper.crm.archive.ConnectionCounts`)."""

    rows: int
    created: int
    updated: int
    needs_review: int
    skipped: int
    with_email: int
    undated: int


class ArchiveMessageCountsOut(BaseModel):
    """What ``messages.csv`` did (:class:`netkeeper.crm.archive.MessageCounts`)."""

    rows: int
    conversations: int
    attributed: int
    no_counterpart: int
    group_threads: int
    unknown_contact: int
    no_owner: int
    added: int
    already_present: int
    undated: int
    outbound: int
    inbound: int


class ArchiveInvitationCountsOut(BaseModel):
    """What ``Invitations.csv`` did (:class:`netkeeper.crm.archive.InvitationCounts`)."""

    rows: int
    added: int
    already_present: int
    unknown_contact: int
    no_counterpart: int
    undated: int
    undirected: int


class ArchiveImportOut(BaseModel):
    """What importing one uploaded archive did (P1-20).

    There is no ``positions`` field yet: #117 adds ``Positions.csv`` support in
    parallel and will add its own counts field alongside these. That is an
    additive change for any client generated from the OpenAPI schema, never a
    breaking one to the fields already here, which is what lets it land without
    a version bump on this endpoint. Until then, ``Positions.csv`` (and any
    other table this importer does not act on) shows up in ``ignored_files``.
    """

    filename: str
    observed_at: datetime
    owner_public_id: str | None
    """Whose archive this was taken to be; ``None`` when the message traffic could not say."""
    owner_by: str | None
    """What identified the owner (see :mod:`netkeeper.linkedin.conversations`), or ``None``."""
    connections: ArchiveConnectionCountsOut
    messages: ArchiveMessageCountsOut
    invitations: ArchiveInvitationCountsOut
    ignored_files: list[str]
    """Tables the archive carried that no importer reads yet, such as ``Positions.csv``."""


class ArchiveRefusalOut(BaseModel):
    """The ``422`` body of a refused archive upload.

    ``detail`` is the human-readable reason, free to reword; ``code`` is what
    a client should actually switch on (:class:`~netkeeper.linkedin.archive.ArchiveRefusalCode`),
    present on every refusal this endpoint can produce, and stable across a
    reword of ``detail`` — the wizard this feeds (P1-21) keys off it rather
    than matching words in the message.
    """

    detail: str
    code: ArchiveRefusalCode


# --- lists and saved views (spec 10.1, 10.4; P1-08) --------------------------

ListName = Annotated[str, Field(min_length=1, max_length=LIST_NAME_MAX_LENGTH)]
ViewName = Annotated[str, Field(min_length=1, max_length=VIEW_NAME_MAX_LENGTH)]


class ListOut(BaseModel):
    id: int
    name: str
    kind: ListKind
    filter: FilterTree | None
    member_count: int
    broken: str | None = None
    """Why this list's filter does not compile, for the rare list where it does not.

    ``null`` for every healthy list, which is every list under ordinary use.
    When it is set, ``member_count`` is 0 because there is no number to give,
    and opening the list answers 422 with this same reason. It is here so that
    one list that cannot be counted does not cost the person the page that
    would let them fix it.
    """

    created_at: datetime
    updated_at: datetime


class ListCreate(BaseModel):
    name: ListName
    kind: ListKind
    filter: FilterTree | None = None
    """Required for a smart list, refused for a static one; the service checks both ways."""


class ListPatch(BaseModel):
    """Fields left out (or ``null``) are left alone; a smart list's filter cannot be cleared,
    only replaced."""

    name: ListName | None = None
    filter: FilterTree | None = None


class ListMembersIn(BaseModel):
    contact_ids: list[int] = Field(min_length=1)


class ListMembersAddedOut(BaseModel):
    added: int
    """Contacts newly added; ids already members, or repeated, are not recounted."""


class ContactSummaryOut(BaseModel):
    """A contact as a list or saved view shows it: enough to identify and open them."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    first_name: str
    last_name: str
    preferred_name: str
    headline: str | None
    current_title: str | None
    current_company: str | None
    met: ContactMet
    li_url: str | None


class ListMembersPage(BaseModel):
    items: list[ContactSummaryOut]
    total: int


class SavedViewOut(BaseModel):
    id: int
    name: str
    columns: list[str]
    sort: list[SortKey]
    filter: FilterTree | None
    created_at: datetime
    updated_at: datetime


class SavedViewCreate(BaseModel):
    name: ViewName
    columns: list[Annotated[str, Field(min_length=1)]] = Field(min_length=1, max_length=MAX_COLUMNS)
    sort: list[SortKey] = Field(default_factory=list)
    filter: FilterTree | None = None


class SavedViewPatch(BaseModel):
    """Fields left out are left alone; ``columns`` and ``sort`` replace the whole list when
    given; ``filter: null`` clears it (a view with no filter shows every contact)."""

    name: ViewName | None = None
    columns: list[Annotated[str, Field(min_length=1)]] | None = Field(
        default=None, min_length=1, max_length=MAX_COLUMNS
    )
    sort: list[SortKey] | None = None
    filter: FilterTree | None = None


# --- triage (spec 10.2) ------------------------------------------------------


class TriageTagOut(BaseModel):
    """A tag as the triage card shows it; the tags resource carries the counts."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    color: str | None
    kind: TagKind


class TriageContactOut(BaseModel):
    """The contact under triage: the fields the screen shows (spec 10.2)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    li_public_id: str | None
    li_url: str | None
    first_name: str
    last_name: str
    preferred_name: str
    headline: str | None
    current_title: str | None
    current_company: str | None
    location: str | None
    connected_on: date | None
    met: ContactMet
    met_source: MetSource
    """Who decided ``met``: the person, or a batch they accepted (spec 10.2).

    ``automatic`` is netkeeper's own answer waiting to be checked, which is what
    the review queue serves; deciding the contact by hand makes it ``manual``.
    """
    triaged_at: datetime | None
    do_not_contact: bool
    notes: str | None
    """Markdown, part of the evidence panel, stored and returned exactly as written.

    Rendered with HTML passthrough it would run whatever is in it, as
    `InteractionOut.summary` would.
    """
    tags: list[TriageTagOut]
    updated_at: datetime
    archived_at: datetime | None
    """When this contact was archived, or ``null`` while they are live (#91).

    Liveness, said plainly, because a client had no other way to ask it. The
    triage queue is ``met IN states AND archived_at IS NULL AND merged_into_id
    IS NULL``, so these two fields are the whole difference between a card that
    is in the queue and one that is not. Undo hands back the contact it
    restored whatever state that left it in, and before this the only signal a
    client had was ``TriageUndoOut.forced`` -- which does not mean that, because
    the service appends to it for *any* overridden divergence, an ordinary field
    edit included.
    """
    merged_into_id: int | None
    """The contact this one was merged into, or ``null``. See ``archived_at``.

    A merged-away contact is out of the queue exactly as an archived one is: the
    survivor carries the decision now.
    """


class SharedCompanyOut(BaseModel):
    """A company this contact is at or was at, and the overlap with the address book.

    Overlap with **the rest of your contacts**, not with you: ``contact_count``
    is how many *other* live contacts are at that company now and ``met_count``
    how many of those you have already marked met, so the reading is "you know
    four people there, three of whom you have met". This is **not** "you both
    worked at X" -- that claim is :class:`OverlapOut`, a separate field with a
    separate name, and a panel must never confuse the two (spec 10.2, #84).

    Every company the contact has is returned, including one where nobody else
    is, as ``contact_count: 0``; a client that only wants overlap filters those.
    """

    model_config = ConfigDict(from_attributes=True)

    company: str
    contact_count: int
    met_count: int


class OverlapOut(BaseModel):
    """Genuine you-and-them overlap: the same company, and the years it is known to overlap.

    The actual LinkedIn "you both worked at X" signal (#84), computed from the
    user's own job history (``/me/positions``) against this contact's. Unlike
    :class:`SharedCompanyOut`, a match here means the two sides are not
    provably disjoint -- a stint known to have ended before the other started
    is excluded.

    ``confirmed`` says whether ``started_on``/``ended_on`` are real: they are
    only ever filled in from a pairing where **both** sides carry an actual
    date. Most contacts today carry no dated position at all, only a current
    company with no known start, which is why most matches come back
    unconfirmed: real evidence that the company matches, but no evidence of
    *when*, so `started_on` and `ended_on` are both `null` rather than
    borrowed from whichever side happens to have a date -- a client must not
    render years next to an unconfirmed match, or show one as fact.
    """

    model_config = ConfigDict(from_attributes=True)

    company: str
    started_on: date | None
    ended_on: date | None
    confirmed: bool


class TriageMessagesOut(BaseModel):
    """The message history with this contact: its shape, and the newest few."""

    model_config = ConfigDict(from_attributes=True)

    total: int
    inbound: int
    outbound: int
    first_at: datetime | None
    last_at: datetime | None
    recent: list[InteractionOut]
    """Newest first. Their `summary` is a message body, returned verbatim: escape it."""
    invitations: int = 0
    """Invitations on file, which are not messages and are not counted as any.

    An invitation is stored as the same kind of row as a message, so a card that
    counted both said "1 message" over a contact who had only ever clicked
    Connect. The batches never counted them; neither does this now."""


class TriageEvidenceOut(BaseModel):
    """Everything the panel shows beside the contact, in the same response as the contact.

    ``shared_companies`` and ``worked_together`` are two different signals with
    two different names (see :class:`SharedCompanyOut` and :class:`OverlapOut`);
    neither implies the other.
    """

    messages: TriageMessagesOut
    timeline: list[TimelineEntryOut]
    shared_companies: list[SharedCompanyOut]
    worked_together: list[OverlapOut]


class TriageCardOut(BaseModel):
    """One contact with its evidence: one request is enough to triage it."""

    contact: TriageContactOut
    evidence: TriageEvidenceOut


class TriageProgressOut(BaseModel):
    """Triaged against total, and how many are left in the queue that was asked for."""

    total: int
    triaged: int
    remaining: int
    by_state: dict[ContactMet, int]
    automatic: int
    """Live contacts a batch decided and nobody has corrected: the review pass."""


class TriageQueueOut(BaseModel):
    """The contact to show now, the one after it, and the progress counters.

    ``next`` is the prefetch: hold it, and deciding ``card`` costs no wait. Both
    are ``null`` when the queue is empty.
    """

    card: TriageCardOut | None
    next: TriageCardOut | None
    progress: TriageProgressOut


class TriageDecisionIn(BaseModel):
    """The `m`, `n`, or `s` key on one contact.

    ``prefetch_after_id`` is the id of the last card the client already holds;
    the response prefetches the one after it, so a client that keeps two cards in
    hand never waits. Left out, the prefetch follows the contact just decided.
    """

    contact_id: int
    met: ContactMet
    prefetch_after_id: int | None = None


class TriageDecisionOut(BaseModel):
    """One row of the triage log: what changed, as it was and as the decision left it."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    contact_id: int
    kind: TriageDecisionKind
    before_state: dict[str, str | None]
    after_state: dict[str, str | None]
    batch_id: str | None
    reason: str | None
    """The key of the suggestion a batch came from; ``null`` for a decision by hand."""
    decided_at: datetime
    undone_at: datetime | None


class TriageDecisionResult(BaseModel):
    """The decision, the next contact with its evidence, and the progress counters."""

    decision: TriageDecisionOut
    next: TriageCardOut | None
    progress: TriageProgressOut


class TriageUndoIn(BaseModel):
    """``force`` restores the previous state even where the contact has moved on since.

    Without it a contact that changed after the decision answers `409` and
    nothing is written.
    """

    force: bool = False


class TriageUndoOut(BaseModel):
    """What the undo put back.

    ``card`` is the restored contact, ready to show, when one decision was
    undone; a bulk batch has no single contact and ``decisions`` counts the rows.
    ``forced`` lists the contacts whose newer state was overwritten.
    """

    kind: TriageDecisionKind
    decisions: int
    batch_id: str | None
    forced: list[int]
    card: TriageCardOut | None
    progress: TriageProgressOut


class PreferredNameIn(BaseModel):
    """What you call this person. Empty means "use the first name"."""

    preferred_name: Annotated[str, Field(max_length=200)]


class PreferredNameOut(BaseModel):
    """The stored name (the first name when the edit was empty) and the undoable decision."""

    contact_id: int
    preferred_name: str
    decision: TriageDecisionOut


class TriageSuggestionOut(BaseModel):
    """A bulk action worth offering, with the number of contacts it would touch.

    Offered, never applied on its own: the count is a preview and the apply is a
    separate call.
    """

    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    description: str
    count: int
    met: ContactMet
    """What the batch would write: ``met`` or ``not_met``."""
    tag_id: int | None
    """The tag a tag batch is built on; ``null`` for the other batches."""


class TriageSuggestionPage(BaseModel):
    """One page of the contacts a suggestion covers, so it can be read before it is taken."""

    items: list[TriageContactOut]
    total: int
    limit: int
    offset: int


class TriageSuggestionApplyIn(BaseModel):
    """``expected_count`` is the count the banner showed; a different one answers `409`.

    Required, and the guard is the point: a batch decides for people nobody has
    looked at, so "apply whatever matches right now" is not a request this API
    takes. The count comes back from ``GET /triage/suggestions`` and from this
    batch's own ``contacts`` page.
    """

    expected_count: int = Field(ge=0)


class TriageSuggestionApplyOut(BaseModel):
    """How many contacts the suggestion touched, and the batch one undo takes back."""

    key: str
    applied: int
    batch_id: str
    met: ContactMet
    progress: TriageProgressOut


# --- LinkedIn runs, budget, heat, pins, schedule (P2-10) -------------------------


class RunStartIn(BaseModel):
    """Start a run by hand. ``max_visits`` (enrichment only) only ever lowers today's budget."""

    model_config = ConfigDict(extra="forbid")

    kind: SyncRunKind
    max_visits: int | None = Field(default=None, ge=1)


class RunResumeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_visits: int | None = Field(default=None, ge=1)


class RunAccepted(BaseModel):
    """The ``202`` of a start or a resume: the recorded run, and the task running it."""

    run_id: int
    task_id: str


class RunOut(BaseModel):
    """One run: what it is, how far it got, and how it ended. Counts only, never names.

    ``planned``/``completed`` are an enrichment plan's size and progress.
    ``aging_refused`` is why a complete full sync aged nobody (#169 E), or null.
    ``resumed_by`` is the id of the run that already took over this one's
    remaining plan, or null when it has not been (and so still may be, spec 9.9).
    """

    id: int
    kind: SyncRunKind
    status: SyncRunStatus
    trigger: SyncRunTrigger
    started_at: datetime
    completed_at: datetime | None
    stop_reason: str | None
    cancel_requested_at: datetime | None
    max_visits: int | None
    resume_of_id: int | None
    browser_mode: str
    progress: dict[str, Any] | None
    counts: dict[str, Any] | None
    notes: str | None
    error: str | None
    planned: int | None
    completed: int | None
    aging_refused: str | None
    resumed_by: int | None


class RunPage(BaseModel):
    items: list[RunOut]
    total: int


class PeriodBudgetOut(BaseModel):
    count: int
    limit: int
    remaining: int


class BudgetOut(BaseModel):
    """One action class's counters against its limits (spec 9.6)."""

    action: str
    day: PeriodBudgetOut
    week: PeriodBudgetOut | None


class TodaysVisitsOut(BaseModel):
    """Today's profile visits, step by step: warm-up, weekend, heat; then what is left."""

    ramp: int
    after_weekend: int
    after_heat: int
    spent_today: int
    week_left: int | None
    remaining: int


class BudgetStatusOut(BaseModel):
    budgets: list[BudgetOut]
    profile_visits_today: TodaysVisitsOut


class HeatOut(BaseModel):
    """Heat as spec 9.7 says the page shows it: level, last raised, when runs resume."""

    score: float
    threshold: float
    multiplier: float
    tripped: bool
    last_raised_at: datetime | None
    cleared_at: datetime | None
    resumes_at: datetime | None


class PinIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int


class PinOut(BaseModel):
    """A pinned contact (spec 9.6: at most 5, to the front of the next run)."""

    contact_id: int
    first_name: str | None
    last_name: str | None


class ScheduledJobOut(BaseModel):
    kind: str
    interval_hours: float
    next_due: datetime | None


class ScheduleOut(BaseModel):
    """Whether scheduled runs may fire, and when each kind is next due.

    ``armed`` is false on every install until a person arms it; while false the
    scheduler still keeps due times, and no scheduled LinkedIn job fires.
    ``scheduler_running`` is whether this process runs a scheduler at all.
    """

    armed: bool
    armed_at: datetime | None
    scheduler_running: bool
    jobs: list[ScheduledJobOut]


class ScheduleArmIn(BaseModel):
    """Arming needs ``confirm: true``: it lets netkeeper visit LinkedIn on its own."""

    model_config = ConfigDict(extra="forbid")

    confirm: bool


class LinkedInStatusOut(BaseModel):
    """The LinkedIn page's banner: the session flag, heat, arming, and any running run."""

    session_flag: str | None
    session_flagged_at: datetime | None
    heat_tripped: bool
    armed: bool
    running_run_id: int | None
    can_start_runs: bool


class ProtectionOut(BaseModel):
    """One row of the posture report (spec section 9): what it is, whether it is in
    force, and anything wrong with it. ``status`` is `netkeeper.services.posture.Status`'s
    value (``on``, ``off``, ``unknown``); ``off``/``unknown`` always carry a warning."""

    name: str
    status: str
    value: str
    warnings: list[str]


class PostureOut(BaseModel):
    """Every protection the LinkedIn extractor has, read-only (P2-11, P2-12, CP3).

    Built from `netkeeper.services.posture.posture()` with no browser probe: a
    live attach-and-read-the-session check has to await browser work, which may
    not happen inside a request handler (CLAUDE.md), so the session protection
    here is always reported unknown rather than checked live — `netkeeper
    preflight` is the live check, still a terminal command only.

    ``gaps`` are known limits of what this report can see, not warnings; they
    never affect ``ok``. ``verdict`` is `netkeeper.services.posture.verdict()`'s
    own sentence, exactly as `netkeeper posture` prints it -- "nothing is
    misconfigured" on a clean report, never "you are safe" (that module's own
    docstring says why: this reads configuration and counters, not whether the
    code that would enforce them actually runs).
    """

    checked_at: datetime
    timezone: str
    local_time: datetime
    protections: list[ProtectionOut]
    warnings: list[str]
    gaps: list[str]
    ok: bool
    verdict: str


class BrowserLaunchOut(BaseModel):
    """``netkeeper browser launch``'s instructions, for the page (spec 9.1, ADR 0002).

    Read-only and built from config alone -- no attach, no CDP connection, nothing
    awaited. Whether Chrome is actually reachable, and whether the session it holds
    is healthy, is what ``netkeeper preflight`` checks; that attaches, so it cannot
    run inside a request handler and has no API endpoint yet (a follow-up).
    """

    cdp_url: str
    profile_dir: str
    launch_command: list[str]
    remote_host_note: str | None
    check_command: str
