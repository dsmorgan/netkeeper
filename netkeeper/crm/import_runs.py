"""Import runs: read a file, review it, commit it in one transaction, undo it (spec 10.5).

The flow this module implements, and what each step promises:

1. :func:`inspect_csv` reads a file's header and works out a mapping from a
   preset, a saved preset, or explicit column choices. It touches no database.
2. :func:`create_run` stores an ``import_runs`` row in state ``draft`` and one
   ``import_rows`` row per data row, with the cells as they were read. Every row
   is resolved (:func:`netkeeper.crm.identity.resolve`) so the run carries counts
   for the whole file, and rows that resolve cleanly are applied *inside a
   savepoint that is rolled back*, so a second row for the same person resolves
   to the first one's contact and the draft still writes nothing.
3. :func:`preview` re-resolves the first :data:`PREVIEW_ROWS` rows the same way
   and says, per row, what it would change and what provenance would refuse.
4. :func:`commit` applies every row in the caller's transaction, so the whole
   file lands or none of it does, and records what each row did.
5. :func:`rollback` undoes one run by id.

What a rollback undoes, exactly
-------------------------------
A contact the run created is deleted, and its children go with it. A contact the
run only enriched is kept and put back the way it was: every provenance field
the run wrote goes back to the value recorded in ``changes_json["fields"]``, its
``field_sources`` and ``synced_values`` entries go back to what they were, and
the child rows the run created (addresses, phones, links, positions, the
snapshot a job change wrote, a retired slug's alias) are deleted by id. A field
that something changed after the import is left alone: the rollback only puts
back what still holds the value the run wrote. A field the import was refused
was never written, so a rollback never touches it either.

Two things a rollback deliberately does not undo: the ``source`` and
``observed_at`` stamps refreshed on child rows that already existed (the row was
not created by the run, and its value is unchanged), and a merge a person
performed afterwards.

Every function that writes needs a writer session (CLAUDE.md): each reads before
it writes, and on SQLite an unmarked read-then-write fails at once with
"database is locked". An importer is a writer. Transactions belong to the
caller; nothing here commits.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime
from typing import Any, Final, assert_never, cast

from sqlalchemy import CursorResult, Date, Delete, inspect
from sqlalchemy.orm import Session, class_mapper

from netkeeper.crm import identity
from netkeeper.crm.identity import Candidate, CreateNew, Decision, Matched, MergeInto, New
from netkeeper.crm.importer import (
    CsvImportError,
    EmptyFile,
    ImportField,
    InvalidMapping,
    MappedRow,
    ParsedCsv,
    Preset,
    ResolvedMapping,
    UnknownPreset,
    detect_preset,
    get_preset,
    map_row,
    mapping_as_json,
    mapping_from_json,
    parse_csv,
    resolve_mapping,
)
from netkeeper.crm.provenance import PROVENANCE_ORDER, may_overwrite
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactAlias,
    ContactChild,
    ContactEmail,
    ContactLink,
    ContactPhone,
    ContactPosition,
    ContactSnapshot,
    ContactSource,
    FieldChange,
    ImportDecisionKind,
    ImportResolution,
    ImportRow,
    ImportRun,
    ImportSourceKind,
    ImportStatus,
    RefusedField,
    RowChanges,
    RowDecision,
    SyncedValue,
    User,
)
from netkeeper.models.base import utcnow
from netkeeper.models.imports import FILENAME_MAX_LENGTH, PRESET_NAME_MAX_LENGTH
from netkeeper.scoping import get_scoped, scoped, scoped_count, scoped_delete
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

PREVIEW_ROWS: Final[int] = 20
"""Rows the preview resolves (spec 10.5 step 3)."""

SAVED_PRESETS_KEY: Final[str] = "imports.presets"
"""``settings_kv`` key holding the user's own presets: ``{name: {header: field}}``."""

# The child tables ``apply`` may add rows to, by table name, so a rollback can
# delete the rows one run created without touching the ones it only refreshed.
CHILD_MODELS: Final[dict[str, type[ContactChild]]] = {
    ContactEmail.__tablename__: ContactEmail,
    ContactPhone.__tablename__: ContactPhone,
    ContactLink.__tablename__: ContactLink,
    ContactPosition.__tablename__: ContactPosition,
    ContactSnapshot.__tablename__: ContactSnapshot,
    ContactAlias.__tablename__: ContactAlias,
}
_CHILD_RELATIONSHIPS: Final[dict[str, str]] = {
    ContactEmail.__tablename__: "emails",
    ContactPhone.__tablename__: "phones",
    ContactLink.__tablename__: "links",
    ContactPosition.__tablename__: "positions",
    ContactSnapshot.__tablename__: "snapshots",
    ContactAlias.__tablename__: "aliases",
}


# --- errors -----------------------------------------------------------------


class ImportRunError(Exception):
    """Base of everything this module raises on purpose."""


class RunNotFound(ImportRunError, LookupError):
    """No such import run for this user."""


class RunNotDraft(ImportRunError, ValueError):
    """The run was already committed or rolled back; it cannot be committed again."""


class RunNotCommitted(ImportRunError, ValueError):
    """Only a committed run can be rolled back."""


class ContactNotFound(ImportRunError, LookupError):
    """A decision, or a row, naming a contact that is not this user's."""


class UnknownRow(ImportRunError, LookupError):
    """A decision naming a row number the run does not have."""


class UndecidedCandidates(ImportRunError, ValueError):
    """Candidate rows are waiting for a decision and the commit was not told to skip them."""

    def __init__(self, row_numbers: Sequence[int]) -> None:
        self.row_numbers = tuple(row_numbers)
        shown = ", ".join(str(number) for number in self.row_numbers[:10])
        more = "" if len(self.row_numbers) <= 10 else f" and {len(self.row_numbers) - 10} more"
        super().__init__(
            f"{len(self.row_numbers)} row(s) resolve to a candidate and have no decision "
            f"(rows {shown}{more}); decide each one, or commit with skip_undecided"
        )


class DuplicatePreset(ImportRunError, ValueError):
    """A built-in preset already has that name."""


class PresetNotFound(ImportRunError, LookupError):
    """No saved preset by that name."""


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "import runs need a writer session; use session_scope(factory, write=True)"
        )


# --- inspecting a file ------------------------------------------------------


@dataclass(frozen=True)
class Inspection:
    """A file's header, the mapping chosen for it, and a first look at its rows."""

    parsed: ParsedCsv
    resolved: ResolvedMapping
    detected_preset: str | None
    """The built-in preset that best fits the header, whether or not it was used."""

    @property
    def headers(self) -> tuple[str, ...]:
        return self.parsed.headers

    @property
    def row_count(self) -> int:
        return len(self.parsed.rows)


def inspect_csv(
    content: str | bytes,
    *,
    preset_name: str | None = None,
    saved_mapping: Mapping[str, str] | None = None,
    mapping: Mapping[str, str] | None = None,
) -> Inspection:
    """Read ``content``'s header and settle its mapping, without touching the database.

    The mapping comes from the named preset (or, when none is named, the one
    :func:`~netkeeper.crm.importer.detect_preset` finds), then a saved preset's
    stored mapping, then ``mapping``'s explicit choices, each overriding the one
    before. ``EmptyFile``, ``UnknownPreset``, and ``InvalidMapping`` say why a
    file or a mapping cannot be used.
    """
    parsed = parse_csv(content)
    detected = detect_preset(parsed.headers)
    preset: Preset | None
    if preset_name is not None:
        preset = get_preset(preset_name) if saved_mapping is None else None
    else:
        preset = detected
    resolved = resolve_mapping(
        parsed.headers, preset=preset, saved=saved_mapping, overrides=mapping
    )
    if preset is None and preset_name is not None:
        resolved = ResolvedMapping(resolved.mapping, preset_name, resolved.unmapped)
    return Inspection(parsed, resolved, detected.name if detected is not None else None)


# --- what one row would do, and what it did ---------------------------------


@dataclass(frozen=True)
class PlannedChange:
    """One field of one row: what is there, what the row carries, and whether it may land."""

    field: str
    before: str | None
    after: str | None
    refused: bool
    """True when provenance keeps the incoming value out (spec 10.5)."""
    kept_source: str | None
    """The source recorded on the field that refused the row; ``manual`` is a person's edit."""


@dataclass(frozen=True)
class RowPreview:
    """One row as the review screen shows it."""

    row_number: int
    raw: dict[str, str]
    resolution: ImportResolution
    contact_id: int | None
    matched_by: str | None
    candidate_ids: tuple[int, ...]
    changes: tuple[PlannedChange, ...]
    problem: str | None

    @property
    def refused(self) -> tuple[PlannedChange, ...]:
        return tuple(change for change in self.changes if change.refused)


@dataclass
class _Outcome:
    """What processing one row produced, for the draft pass and for the commit."""

    row_number: int
    raw: dict[str, str]
    resolution: ImportResolution
    contact_id: int | None = None
    matched_by: str | None = None
    candidate_ids: tuple[int, ...] = ()
    changes: tuple[PlannedChange, ...] = ()
    effect: RowChanges | None = None
    problem: str | None = None
    decision: RowDecision | None = None


@dataclass(frozen=True)
class _Before:
    """A contact's state before a row was applied: everything a rollback needs."""

    fields: dict[str, str | date | None]
    sources: dict[str, str]
    synced: dict[str, SyncedValue]
    children: dict[str, set[int]] = dataclass_field(default_factory=dict)


def _snapshot(contact: Contact) -> _Before:
    return _Before(
        fields={name: getattr(contact, name) for name in PROVENANCE_ORDER},
        sources=dict(contact.field_sources or {}),
        synced={name: dict(entry) for name, entry in (contact.synced_values or {}).items()},  # type: ignore[misc]
        children={
            table: {row.id for row in getattr(contact, attribute)}
            for table, attribute in _CHILD_RELATIONSHIPS.items()
        },
    )


def _effect(before: _Before, contact: Contact) -> RowChanges:
    """The difference the row made, in the shape :func:`rollback` reads."""
    fields: dict[str, FieldChange] = {}
    sources: dict[str, str | None] = {}
    synced: dict[str, SyncedValue | None] = {}
    for name in PROVENANCE_ORDER:
        was, now = before.fields[name], getattr(contact, name)
        if was != now:
            fields[name] = {"before": _json_value(was), "after": _json_value(now)}
        source_now = (contact.field_sources or {}).get(name)
        if before.sources.get(name) != source_now:
            sources[name] = before.sources.get(name)
        synced_now = (contact.synced_values or {}).get(name)
        if before.synced.get(name) != synced_now:
            synced[name] = before.synced.get(name)
    children = {
        table: sorted(
            {row.id for row in getattr(contact, attribute)} - before.children.get(table, set())
        )
        for table, attribute in _CHILD_RELATIONSHIPS.items()
    }
    return {
        "created_contact": False,
        "fields": fields,
        "sources": sources,
        "synced": synced,
        "children": {table: ids for table, ids in children.items() if ids},
        "refused": [],
    }


def _plan(contact: Contact | None, mapped: MappedRow) -> tuple[PlannedChange, ...]:
    """What the row would write to ``contact`` (or to a new contact when None).

    Every provenance field the row carries whose value differs from what is
    there, with ``refused`` set on the ones
    :func:`~netkeeper.crm.provenance.may_overwrite` keeps out.
    """
    if mapped.incoming is None:
        return ()
    source = mapped.incoming.source.value
    changes: list[PlannedChange] = []
    for name, value in mapped.incoming.provided_fields().items():
        before = getattr(contact, name) if contact is not None else None
        if before == value:
            continue
        allowed = contact is None or may_overwrite(name, source, contact)
        changes.append(
            PlannedChange(
                field=name,
                before=_json_value(before),
                after=_json_value(value),
                refused=not allowed,
                kept_source=None
                if allowed
                else (contact.field_sources or {}).get(name)
                if contact is not None
                else None,
            )
        )
    return tuple(changes)


def _refused_json(changes: Sequence[PlannedChange]) -> list[RefusedField]:
    return [
        {
            "field": change.field,
            "incoming": change.after,
            "kept": change.before,
            "source": change.kept_source or ContactSource.MANUAL.value,
        }
        for change in changes
        if change.refused
    ]


def _process_row(
    session: Session,
    user: User,
    row_number: int,
    raw: Mapping[str, str],
    mapping: Mapping[str, ImportField],
    *,
    observed_at: datetime,
    decision: Decision | None,
    apply_row: bool,
) -> _Outcome:
    """Resolve one row, and apply it when ``apply_row`` and it needs no decision.

    The single path the draft pass, the preview, and the commit all take, so what
    a preview promises is what a commit does.
    """
    cells = dict(raw)
    mapped = map_row(raw, mapping, observed_at=observed_at)
    if mapped.incoming is None:
        return _Outcome(row_number, cells, ImportResolution.SKIPPED, problem=mapped.problem_text)
    resolution = identity.resolve(session, user, mapped.incoming)
    outcome = _Outcome(row_number, cells, ImportResolution.SKIPPED, problem=mapped.problem_text)

    match resolution:
        case Matched(contact_id=contact_id, by=by):
            target = identity.resolve_survivor(session, user, contact_id)
            outcome.resolution = ImportResolution.MATCHED
            outcome.contact_id = target.id
            outcome.matched_by = by
            outcome.changes = _plan(target, mapped)
        case New():
            outcome.resolution = ImportResolution.CREATED
            outcome.changes = _plan(None, mapped)
        case Candidate(contact_ids=contact_ids):
            outcome.candidate_ids = contact_ids
            match decision:
                case None:
                    outcome.resolution = ImportResolution.CANDIDATE
                    first = identity.resolve_survivor(session, user, contact_ids[0])
                    outcome.contact_id = None
                    outcome.changes = _plan(first, mapped)
                    return outcome
                case MergeInto(contact_id=into):
                    target = identity.resolve_survivor(session, user, into)
                    outcome.resolution = ImportResolution.MATCHED
                    outcome.contact_id = target.id
                    outcome.changes = _plan(target, mapped)
                    outcome.decision = {
                        "kind": ImportDecisionKind.MERGE_INTO.value,
                        "contact_id": target.id,
                    }
                case CreateNew():
                    outcome.resolution = ImportResolution.CREATED
                    outcome.changes = _plan(None, mapped)
                    outcome.decision = {
                        "kind": ImportDecisionKind.CREATE_NEW.value,
                        "contact_id": None,
                    }
                case _:  # pragma: no cover - Decision is a closed union
                    assert_never(decision)
        case _:  # pragma: no cover - Resolution is a closed union
            assert_never(resolution)

    if not apply_row:
        return outcome
    created = outcome.resolution is ImportResolution.CREATED
    before = (
        _Before({}, {}, {})
        if created
        else _snapshot(_owned(session, user, cast(int, outcome.contact_id)))
    )
    try:
        contact = identity.apply(session, user, mapped.incoming, resolution, decision=decision)
    except ValueError as exc:
        # apply() checks everything before its first write, so the contact is
        # untouched: this row lands nowhere and the rest of the file still commits.
        log.warning("import row %d could not be applied: %s", row_number, exc)
        return _Outcome(
            row_number,
            cells,
            ImportResolution.SKIPPED,
            candidate_ids=outcome.candidate_ids,
            problem=_join(mapped.problem_text, str(exc)),
            decision=outcome.decision,
        )
    outcome.contact_id = contact.id
    effect: RowChanges = (
        {
            "created_contact": True,
            "fields": {},
            "sources": {},
            "synced": {},
            "children": {},
            "refused": [],
        }
        if created
        else _effect(before, contact)
    )
    effect["refused"] = _refused_json(outcome.changes)
    outcome.effect = effect
    return outcome


def _join(*parts: str | None) -> str | None:
    kept = [part for part in parts if part]
    return "; ".join(kept) if kept else None


def _owned(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise ContactNotFound(f"contact {contact_id} is not one of this user's contacts")
    return contact


@contextmanager
def _dry_run(session: Session) -> Iterator[None]:
    """Run the block against the database and undo every write it makes.

    A savepoint, so resolution inside sees the rows the block itself wrote (a
    second row for the same person resolves to the first one's contact) and the
    database keeps none of it. The session's objects are expired afterwards, so
    nothing rolled back is read back from memory.
    """
    nested = session.begin_nested()
    try:
        yield
    finally:
        if nested.is_active:
            nested.rollback()
        session.expire_all()


# --- runs -------------------------------------------------------------------


def create_run(
    session: Session,
    user: User,
    *,
    filename: str,
    content: str | bytes,
    preset_name: str | None = None,
    mapping: Mapping[str, str] | None = None,
    source_kind: ImportSourceKind = ImportSourceKind.CSV,
) -> ImportRun:
    """Read ``content`` into a ``draft`` run with one row per data row.

    Every row is resolved so the run carries counts for the whole file, and rows
    that need no decision are applied inside a rolled-back savepoint, so a row
    repeated in the file resolves to the contact its first occurrence would
    create rather than looking like a second new person. Nothing is written
    outside the run and its rows. ``preset_name`` may name a built-in preset or
    one the user saved. ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    inspection = inspect_csv(
        content,
        preset_name=preset_name,
        saved_mapping=_saved_mapping(session, user, preset_name),
        mapping=mapping,
    )
    observed_at = utcnow()
    outcomes: list[_Outcome] = []
    with _dry_run(session):
        for number, raw in enumerate(inspection.parsed.rows, start=1):
            outcomes.append(
                _process_row(
                    session,
                    user,
                    number,
                    raw,
                    inspection.resolved.mapping,
                    observed_at=observed_at,
                    decision=None,
                    apply_row=True,
                )
            )
    run = ImportRun(
        user_id=user.id,
        source_kind=source_kind,
        filename=filename[:FILENAME_MAX_LENGTH],
        preset=inspection.resolved.preset,
        mapping_json=mapping_as_json(inspection.resolved.mapping),
        status=ImportStatus.DRAFT,
    )
    session.add(run)
    for outcome in outcomes:
        run.rows.append(
            ImportRow(
                user_id=user.id,
                row_number=outcome.row_number,
                raw_json=outcome.raw,
                resolution=outcome.resolution,
                # The draft pass wrote nothing, so no row points at a contact yet;
                # a candidate's possibilities are what the review screen needs.
                candidate_ids_json=list(outcome.candidate_ids) or None,
                error=outcome.problem,
            )
        )
    _recount(run, outcomes)
    session.flush()
    log.info(
        "import run %d for user %d: %d rows from %r (%s)",
        run.id,
        user.id,
        run.total_rows,
        run.filename,
        inspection.resolved.preset or "custom mapping",
    )
    return run


def _recount(run: ImportRun, outcomes: Sequence[_Outcome]) -> None:
    run.total_rows = len(outcomes)
    run.matched_count = sum(1 for o in outcomes if o.resolution is ImportResolution.MATCHED)
    run.created_count = sum(1 for o in outcomes if o.resolution is ImportResolution.CREATED)
    run.candidate_count = sum(1 for o in outcomes if o.resolution is ImportResolution.CANDIDATE)
    run.skipped_count = sum(1 for o in outcomes if o.resolution is ImportResolution.SKIPPED)


def get_run(session: Session, user: User, run_id: int) -> ImportRun:
    """``user``'s run by id. ``RunNotFound`` for anyone else's, and for one that is gone."""
    run = get_scoped(session, user, ImportRun, run_id)
    if run is None:
        raise RunNotFound(f"no import run {run_id} for this user")
    return run


def list_runs(
    session: Session, user: User, *, limit: int = 50, offset: int = 0
) -> tuple[list[ImportRun], int]:
    """``user``'s runs, newest first, and how many there are in all."""
    total = session.scalar(scoped_count(user, ImportRun)) or 0
    statement = scoped(user, ImportRun).order_by(ImportRun.id.desc()).limit(limit).offset(offset)
    return list(session.scalars(statement)), total


def list_rows(
    session: Session,
    user: User,
    run_id: int,
    *,
    resolution: ImportResolution | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ImportRow], int]:
    """One run's rows in file order, and how many match. ``RunNotFound`` for another user's."""
    run = get_run(session, user, run_id)
    counted = scoped_count(user, ImportRow).where(ImportRow.run_id == run.id)
    statement = scoped(user, ImportRow).where(ImportRow.run_id == run.id)
    if resolution is not None:
        counted = counted.where(ImportRow.resolution == resolution)
        statement = statement.where(ImportRow.resolution == resolution)
    total = session.scalar(counted) or 0
    statement = statement.order_by(ImportRow.row_number).limit(limit).offset(offset)
    return list(session.scalars(statement)), total


def preview(
    session: Session, user: User, run_id: int, *, limit: int = PREVIEW_ROWS
) -> list[RowPreview]:
    """The first ``limit`` rows resolved against the database as it is now.

    Each row says whether it is an existing contact, a candidate waiting for a
    decision, or a new contact, and for a match which fields would change and
    which provenance would refuse (spec 10.5). Nothing is written: the rows are
    applied inside a savepoint that is rolled back, so a row repeated in the file
    previews as a match on its first occurrence, not as a second new contact.
    ``RuntimeError`` when ``session`` is not a writer: resolution itself writes.
    """
    _require_writer(session)
    run = get_run(session, user, run_id)
    rows, _ = list_rows(session, user, run.id, limit=limit)
    mapping = mapping_from_json(run.mapping_json, _mapping_headers(rows, run))
    decisions = {row.row_number: _decision_of(row) for row in rows}
    observed_at = utcnow()
    previews: list[RowPreview] = []
    with _dry_run(session):
        for row in rows:
            outcome = _process_row(
                session,
                user,
                row.row_number,
                row.raw_json,
                mapping,
                observed_at=observed_at,
                decision=decisions[row.row_number],
                apply_row=True,
            )
            previews.append(
                RowPreview(
                    row_number=outcome.row_number,
                    raw=outcome.raw,
                    resolution=outcome.resolution,
                    contact_id=outcome.contact_id
                    if outcome.resolution is ImportResolution.MATCHED
                    else None,
                    matched_by=outcome.matched_by,
                    candidate_ids=outcome.candidate_ids,
                    changes=outcome.changes,
                    problem=outcome.problem,
                )
            )
    return previews


def _mapping_headers(rows: Sequence[ImportRow], run: ImportRun) -> list[str]:
    """The header names the stored rows carry; the mapping is filtered to these."""
    return list(rows[0].raw_json) if rows else list(run.mapping_json)


def _decision_of(row: ImportRow) -> Decision | None:
    stored = row.decision_json
    if stored is None:
        return None
    if stored["kind"] == ImportDecisionKind.MERGE_INTO.value:
        contact_id = stored["contact_id"]
        return MergeInto(contact_id) if contact_id is not None else None
    return CreateNew()


def set_decisions(
    session: Session, user: User, run_id: int, decisions: Mapping[int, Decision]
) -> ImportRun:
    """Record a person's decision for each named row of a draft run.

    ``UnknownRow`` when a row number is not in the run, ``RunNotDraft`` once the
    run is committed. The decision is applied at :func:`commit`.
    """
    _require_writer(session)
    run = get_run(session, user, run_id)
    if run.status is not ImportStatus.DRAFT:
        raise RunNotDraft(f"import run {run.id} is {run.status.value}; its rows cannot be changed")
    by_number = {row.row_number: row for row in run.rows}
    for number, decision in decisions.items():
        row = by_number.get(number)
        if row is None:
            raise UnknownRow(f"import run {run.id} has no row {number}")
        match decision:
            case MergeInto(contact_id=contact_id):
                _owned(session, user, contact_id)  # ValueError for another user's contact
                row.decision_json = {
                    "kind": ImportDecisionKind.MERGE_INTO.value,
                    "contact_id": contact_id,
                }
            case CreateNew():
                row.decision_json = {
                    "kind": ImportDecisionKind.CREATE_NEW.value,
                    "contact_id": None,
                }
            case _:  # pragma: no cover - Decision is a closed union
                assert_never(decision)
    session.flush()
    return run


def commit(
    session: Session,
    user: User,
    run_id: int,
    *,
    decisions: Mapping[int, Decision] | None = None,
    skip_undecided: bool = False,
) -> ImportRun:
    """Apply every row of a draft run, in the caller's transaction.

    ``decisions`` are recorded first, so one call can decide and commit. Each row
    is resolved again against the database as it is now, because it may have
    moved since the draft was read, and then applied; what each row changed is
    written to its ``changes_json``, with the values that were there before, so
    :func:`rollback` can put them back. A row that resolves to a candidate and
    has no decision stops the commit (``UndecidedCandidates``) unless
    ``skip_undecided``, in which case it is skipped and counted. Nothing is
    committed here: the caller's transaction makes the whole file land or none of
    it. ``RunNotDraft`` for a run that was already committed or rolled back.
    """
    _require_writer(session)
    run = get_run(session, user, run_id)
    if run.status is not ImportStatus.DRAFT:
        raise RunNotDraft(
            f"import run {run.id} is {run.status.value}; only a draft run can be committed"
        )
    if decisions:
        set_decisions(session, user, run.id, decisions)
    rows = list(run.rows)
    mapping = mapping_from_json(run.mapping_json, _mapping_headers(rows, run))
    observed_at = utcnow()
    outcomes: list[_Outcome] = []
    undecided: list[int] = []
    for row in rows:
        outcome = _process_row(
            session,
            user,
            row.row_number,
            row.raw_json,
            mapping,
            observed_at=observed_at,
            decision=_decision_of(row),
            apply_row=True,
        )
        if outcome.resolution is ImportResolution.CANDIDATE:
            undecided.append(row.row_number)
            outcome.resolution = ImportResolution.SKIPPED
            outcome.problem = _join(outcome.problem, "waiting for a decision")
        row.resolution = outcome.resolution
        row.contact_id = outcome.contact_id
        row.matched_by = outcome.matched_by
        row.candidate_ids_json = list(outcome.candidate_ids) or None
        row.changes_json = outcome.effect
        row.error = outcome.problem
        outcomes.append(outcome)
    if undecided and not skip_undecided:
        raise UndecidedCandidates(undecided)
    _recount(run, outcomes)
    run.status = ImportStatus.COMMITTED
    run.committed_at = utcnow()
    session.flush()
    log.info(
        "import run %d committed for user %d: %d matched, %d created, %d skipped",
        run.id,
        user.id,
        run.matched_count,
        run.created_count,
        run.skipped_count,
    )
    return run


# --- rollback ---------------------------------------------------------------


@dataclass(frozen=True)
class RollbackResult:
    """What undoing a run removed and put back."""

    run_id: int
    contacts_deleted: int
    contacts_restored: int
    fields_restored: int
    children_deleted: int


def rollback(session: Session, user: User, run_id: int) -> RollbackResult:
    """Undo one committed run, and only what that run did (spec 10.5).

    A contact the run created is deleted with its children. A contact the run
    only enriched stays, with every field the run wrote put back to the value
    recorded before it, its provenance and synced-value entries put back too, and
    the child rows the run added deleted. A field something changed after the
    import keeps that later value: the rollback only reverses what still holds
    what the run wrote. ``RunNotCommitted`` for a draft or an already rolled-back
    run. ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    run = get_run(session, user, run_id)
    if run.status is not ImportStatus.COMMITTED:
        raise RunNotCommitted(
            f"import run {run.id} is {run.status.value}; only a committed run can be rolled back"
        )
    rows = list(run.rows)
    created_ids = {
        row.contact_id
        for row in rows
        if row.contact_id is not None
        and row.changes_json is not None
        and row.changes_json["created_contact"]
    }
    restored = fields = children = 0
    for row in rows:
        changes = row.changes_json
        if changes is None or changes["created_contact"] or row.contact_id is None:
            continue
        if row.contact_id in created_ids:
            continue  # the same run created this contact; it is about to be deleted
        undone = _restore(session, user, row.contact_id, changes)
        if undone is None:
            continue
        restored += 1
        fields += undone[0]
        children += undone[1]
    session.flush()
    deleted = _delete_contacts(session, user, created_ids)
    session.expire_all()  # the delete cascaded in the database, behind the ORM's back
    run.status = ImportStatus.ROLLED_BACK
    run.rolled_back_at = utcnow()
    session.flush()
    log.info(
        "import run %d rolled back for user %d: %d contacts deleted, %d restored",
        run.id,
        user.id,
        deleted,
        restored,
    )
    return RollbackResult(run.id, deleted, restored, fields, children)


def _restore(
    session: Session, user: User, contact_id: int, changes: RowChanges
) -> tuple[int, int] | None:
    """Put one enriched contact back. ``(fields restored, child rows deleted)``, or None."""
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        return None  # deleted since the import; there is nothing to put back
    restored = 0
    # In PROVENANCE_ORDER, because assigning li_public_id derives li_url, which is
    # restored on its own right after (netkeeper.crm.provenance.PROVENANCE_ORDER).
    for name in PROVENANCE_ORDER:
        change = changes["fields"].get(name)
        if change is None:
            continue
        if _json_value(getattr(contact, name)) != change["after"]:
            continue  # something wrote this field after the import; leave it alone
        setattr(contact, name, _column_value(name, change["before"]))
        restored += 1
    for name, source in changes["sources"].items():
        if source is None:
            contact.field_sources.pop(name, None)
        else:
            contact.field_sources[name] = source
    for name, entry in changes["synced"].items():
        if entry is None:
            contact.synced_values.pop(name, None)
        else:
            contact.synced_values[name] = entry
    deleted = 0
    for table, ids in changes["children"].items():
        model = CHILD_MODELS.get(table)
        if model is None or not ids:  # pragma: no cover - a table this version dropped
            continue
        # ``ContactChild`` declares no primary key of its own; each table does.
        (key,) = class_mapper(model).primary_key
        statement = scoped_delete(user, model).where(model.contact_id == contact.id, key.in_(ids))
        result = cast("CursorResult[Any]", session.execute(_unsynchronized(statement)))
        deleted += result.rowcount
    return restored, deleted


def _delete_contacts(session: Session, user: User, contact_ids: Iterable[int]) -> int:
    """Delete the contacts a run created. Children go with them, in the database."""
    ids = sorted(contact_ids)
    if not ids:
        return 0
    statement = scoped_delete(user, Contact).where(Contact.id.in_(ids))
    result = cast("CursorResult[Any]", session.execute(_unsynchronized(statement)))
    return result.rowcount


def _unsynchronized(statement: Delete) -> Delete:
    """A delete the ORM does not try to mirror into the session first.

    Its default strategy is a SELECT of the rows about to go, which the scope
    guard rejects because the ORM builds it itself and it carries no scope mark.
    :func:`rollback` expires the session once the deletes are done instead.
    """
    return statement.execution_options(synchronize_session=False)


def _json_value(value: str | date | None) -> str | None:
    """A column's value as ``changes_json`` stores it: a date in ISO form."""
    return value.isoformat() if isinstance(value, date) else value


def _column_value(field: str, raw: str | None) -> str | date | None:
    """A stored value back as the column holds it: a date parsed, an empty name ``''``.

    The counterpart of :func:`_json_value`, and the same conversion
    :mod:`netkeeper.crm.provenance` makes for its own ledger.
    """
    column = inspect(Contact).columns[field]
    if raw is None:
        return None if column.nullable else ""  # names are '' when unknown, never NULL
    if isinstance(column.type, Date):
        return date.fromisoformat(raw)
    return raw


# --- saved presets ----------------------------------------------------------


def saved_presets(session: Session, user: User) -> dict[str, dict[str, str]]:
    """The user's own presets: ``{name: {header: field}}``, empty when they have none."""
    stored = get_setting(session, user, SAVED_PRESETS_KEY, {})
    if not isinstance(stored, dict):  # pragma: no cover - a value nothing here writes
        log.warning("%s for user %d is not an object; ignoring it", SAVED_PRESETS_KEY, user.id)
        return {}
    return {
        name: {str(header): str(field) for header, field in mapping.items()}
        for name, mapping in stored.items()
        if isinstance(mapping, dict)
    }


def _saved_mapping(session: Session, user: User, name: str | None) -> dict[str, str] | None:
    """A saved preset's stored mapping, or None when ``name`` is a built-in or absent."""
    if name is None or name in {preset.name for preset in _builtin_names()}:
        return None
    return saved_presets(session, user).get(name)


def _builtin_names() -> tuple[Preset, ...]:
    from netkeeper.crm.importer import PRESETS

    return PRESETS


def save_preset(
    session: Session, user: User, name: str, mapping: Mapping[str, str]
) -> dict[str, str]:
    """Save ``mapping`` under ``name`` for this user, replacing any preset by that name.

    ``DuplicatePreset`` when a built-in preset has that name: those are the
    shared vocabulary and are never shadowed. ``InvalidMapping`` for an empty
    mapping or a field an import cannot write.
    """
    _require_writer(session)
    cleaned = name.strip()
    if not cleaned or len(cleaned) > PRESET_NAME_MAX_LENGTH:
        raise InvalidMapping(f"a preset name is 1 to {PRESET_NAME_MAX_LENGTH} characters")
    if cleaned in {preset.name for preset in _builtin_names()}:
        raise DuplicatePreset(f"{cleaned!r} is a built-in preset; choose another name")
    if not mapping:
        raise InvalidMapping("a preset maps at least one column to a field")
    stored: dict[str, str] = {}
    for header, field in mapping.items():
        if not header.strip():
            continue
        try:
            stored[header.strip()] = ImportField(field).value
        except ValueError as exc:
            raise InvalidMapping(
                f"{field!r} is not a field an import can write; "
                f"the fields are {', '.join(sorted(ImportField))}"
            ) from exc
    if not stored:
        raise InvalidMapping("a preset maps at least one column to a field")
    presets = saved_presets(session, user)
    presets[cleaned] = stored
    set_setting(session, user, SAVED_PRESETS_KEY, presets)
    log.info("user %d saved import preset %r with %d columns", user.id, cleaned, len(stored))
    return stored


def delete_preset(session: Session, user: User, name: str) -> None:
    """Forget one of the user's own presets. ``PresetNotFound`` when there is none."""
    _require_writer(session)
    presets = saved_presets(session, user)
    if name not in presets:
        raise PresetNotFound(f"no saved preset called {name!r}")
    del presets[name]
    set_setting(session, user, SAVED_PRESETS_KEY, presets)


__all__ = [
    "PREVIEW_ROWS",
    "SAVED_PRESETS_KEY",
    "Candidate",
    "ContactNotFound",
    "CreateNew",
    "CsvImportError",
    "Decision",
    "DuplicatePreset",
    "EmptyFile",
    "ImportRunError",
    "Inspection",
    "InvalidMapping",
    "MergeInto",
    "PlannedChange",
    "PresetNotFound",
    "RollbackResult",
    "RowPreview",
    "RunNotCommitted",
    "RunNotDraft",
    "RunNotFound",
    "UndecidedCandidates",
    "UnknownPreset",
    "UnknownRow",
    "commit",
    "create_run",
    "delete_preset",
    "get_run",
    "inspect_csv",
    "list_rows",
    "list_runs",
    "preview",
    "rollback",
    "save_preset",
    "saved_presets",
    "set_decisions",
]
