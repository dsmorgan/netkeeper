"""``/imports``: read a CSV, map its columns, review it, commit it, undo it (spec 10.5, 14.1).

The screens this serves, in order: ``POST /imports/inspect`` shows a file's
columns and the mapping a preset gives them without storing anything;
``POST /imports`` reads the file into a draft run and counts the whole file;
``POST /imports/{id}/preview`` resolves the first rows against the database as it
is now and says what each one would change and what provenance would refuse;
``POST /imports/{id}/commit`` applies the run in the request's one transaction
and runs the auto-tag rules over the contacts it wrote, answering with their
counts (#64); ``POST /imports/{id}/rollback`` undoes a committed run.

A draft that is never finished — a ``--dry-run``, or a commit refused for
undecided candidates — is not silently lost: ``GET /imports?status=draft``
finds it and ``DELETE /imports/{id}`` removes it, or a later
``POST /imports/{id}/commit`` on the same id finishes it, which is how a
refusal is resumed rather than orphaned (#90).

A commit is one transaction because the request's session is one transaction: the
handler either returns and the session commits, or it raises and nothing lands.
The rules the service applies are :mod:`netkeeper.crm.import_runs`.

``POST /imports/archive`` (P1-20) is a different shape: the LinkedIn export zip
itself, uploaded and run straight through
:func:`netkeeper.crm.archive.import_archive` in this request's one transaction —
no draft, no preview, no candidate review, because that pipeline has none; a
connection row that resolves to a candidate is counted and left for a later
CSV import to resolve instead (see that module's docstring). It does record a
committed run (#132), whose id it answers with, so the import appears in the
history and ``POST /imports/{id}/rollback`` undoes it like any other.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Annotated, Any, Final

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from netkeeper.crm import import_runs as service
from netkeeper.crm.archive import ArchiveImport
from netkeeper.crm.archive import import_archive as run_archive_import
from netkeeper.crm.archive_check import open_checked_archive
from netkeeper.crm.identity import CreateNew, Decision, MergeInto
from netkeeper.crm.importer import PRESETS
from netkeeper.linkedin.archive import ArchiveFormatError, ArchiveRefusalCode
from netkeeper.models import (
    ContactSource,
    ImportDecisionKind,
    ImportResolution,
    ImportRow,
    ImportRun,
    ImportStatus,
)
from netkeeper.web.deps import CurrentUser, SessionDep
from netkeeper.web.errors import ApiError
from netkeeper.web.schemas import (
    ArchiveConnectionCountsOut,
    ArchiveImportOut,
    ArchiveInvitationCountsOut,
    ArchiveMessageCountsOut,
    ArchiveRefusalOut,
    ArchiveReportOut,
    ImportChangeOut,
    ImportCommitIn,
    ImportDecisionIn,
    ImportDecisionOut,
    ImportInspectIn,
    ImportInspectOut,
    ImportPresetIn,
    ImportPresetOut,
    ImportPresetsOut,
    ImportPreviewRow,
    ImportRefusedOut,
    ImportRollbackOut,
    ImportRowOut,
    ImportRowPage,
    ImportRunCreate,
    ImportRunOut,
    ImportRunPage,
    RollbackAcquiredOut,
    RollbackRefusalOut,
)

router = APIRouter(tags=["imports"])

SAMPLE_ROWS = 5
"""Rows ``POST /imports/inspect`` echoes back, so the mapping screen has something to show."""

Responses = dict[int | str, dict[str, Any]]
NOT_FOUND: Responses = {404: {"description": "No such import run, row, contact, or preset"}}
CONFLICT: Responses = {
    409: {
        "description": "The run's state forbids this, candidate rows are undecided, "
        "or a merge has drawn in a contact the run created"
    }
}
INVALID: Responses = {422: {"description": "A file or a mapping that cannot be used"}}


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map the service's exceptions to HTTP statuses: 404, 409, 422.

    A rollback's own refusals are not here: :func:`rollback_refusal` answers them
    with a ``code`` and the details a client acts on.
    """
    try:
        yield
    except service.UnknownRow as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (service.RunNotFound, service.PresetNotFound, service.ContactNotFound) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        service.RunNotDraft,
        service.RunNotCommitted,
        service.UndecidedCandidates,
        service.DuplicatePreset,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        service.EmptyFile,
        service.MalformedCsv,
        service.InvalidMapping,
        service.UnknownPreset,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ArchiveFormatError as exc:
        raise ApiError(422, {"detail": str(exc), "code": exc.code.value}) from exc


def run_out(run: ImportRun) -> ImportRunOut:
    return ImportRunOut(
        id=run.id,
        source_kind=run.source_kind,
        filename=run.filename,
        preset=run.preset,
        mapping=dict(run.mapping_json),
        status=run.status,
        total_rows=run.total_rows,
        matched_count=run.matched_count,
        created_count=run.created_count,
        candidate_count=run.candidate_count,
        skipped_count=run.skipped_count,
        tagged_contacts=run.tagged_contacts,
        tags_added=run.tags_added,
        tags_removed=run.tags_removed,
        committed_at=run.committed_at,
        rolled_back_at=run.rolled_back_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
        archive=(
            ArchiveReportOut.model_validate(run.report_json)
            if run.report_json is not None
            else None
        ),
    )


def row_out(row: ImportRow) -> ImportRowOut:
    changes = row.changes_json
    return ImportRowOut(
        id=row.id,
        row_number=row.row_number,
        raw=dict(row.raw_json),
        resolution=row.resolution,
        contact_id=row.contact_id,
        matched_by=row.matched_by,
        candidate_ids=list(row.candidate_ids_json or ()),
        decision=(
            ImportDecisionOut(
                kind=ImportDecisionKind(row.decision_json["kind"]),
                contact_id=row.decision_json["contact_id"],
            )
            if row.decision_json is not None
            else None
        ),
        refused=[
            ImportRefusedOut(
                field=entry["field"],
                incoming=entry["incoming"],
                kept=entry["kept"],
                source=ContactSource(entry["source"]),
            )
            for entry in (changes["refused"] if changes is not None else ())
        ],
        error=row.error,
    )


def change_out(change: service.PlannedChange) -> ImportChangeOut:
    return ImportChangeOut(
        field=change.field,
        before=change.before,
        after=change.after,
        refused=change.refused,
        kept_source=ContactSource(change.kept_source) if change.kept_source else None,
    )


def decisions_of(decisions: list[ImportDecisionIn]) -> dict[int, Decision]:
    """The request's decisions keyed by row number. The schema checks the shape."""
    chosen: dict[int, Decision] = {}
    for decision in decisions:
        if decision.kind is ImportDecisionKind.MERGE_INTO and decision.contact_id is not None:
            chosen[decision.row_number] = MergeInto(decision.contact_id)
        else:
            chosen[decision.row_number] = CreateNew()
    return chosen


def preset_out(name: str, mapping: Mapping[str, str], *, builtin: bool) -> ImportPresetOut:
    return ImportPresetOut(name=name, builtin=builtin, mapping=dict(mapping))


# --- presets ----------------------------------------------------------------
# Declared before /imports/{run_id}, which would otherwise try to read "presets"
# as a run id and answer 422.


@router.get("/imports/presets", operation_id="list_import_presets")
def list_import_presets(user: CurrentUser, session: SessionDep) -> ImportPresetsOut:
    """The built-in presets, with the headers each recognizes, and the user's own."""
    return ImportPresetsOut(
        builtin=[
            preset_out(
                preset.name,
                {header: field.value for header, field in preset.aliases.items()},
                builtin=True,
            )
            for preset in PRESETS
        ],
        saved=[
            preset_out(name, mapping, builtin=False)
            for name, mapping in sorted(service.saved_presets(session, user).items())
        ],
    )


@router.put(
    "/imports/presets/{name}",
    operation_id="save_import_preset",
    responses={**CONFLICT, **INVALID},
)
def save_import_preset(
    name: str, body: ImportPresetIn, user: CurrentUser, session: SessionDep
) -> ImportPresetOut:
    """Save a column mapping under a name, replacing any earlier preset by that name."""
    with translate_errors():
        stored = service.save_preset(session, user, name, body.mapping)
    return preset_out(name.strip(), stored, builtin=False)


@router.delete(
    "/imports/presets/{name}",
    operation_id="delete_import_preset",
    status_code=204,
    responses=NOT_FOUND,
)
def delete_import_preset(name: str, user: CurrentUser, session: SessionDep) -> None:
    """Forget one of your own presets. The built-in ones cannot be deleted."""
    with translate_errors():
        service.delete_preset(session, user, name)


# --- reading a file ---------------------------------------------------------


@router.post("/imports/inspect", operation_id="inspect_import_file", responses=INVALID)
def inspect_import_file(body: ImportInspectIn, user: CurrentUser) -> ImportInspectOut:
    """A file's columns and the mapping a preset gives them. Nothing is stored."""
    with translate_errors():
        inspection = service.inspect_csv(
            body.content, preset_name=body.preset, mapping=body.mapping
        )
    return ImportInspectOut(
        headers=list(inspection.headers),
        row_count=inspection.row_count,
        preamble_rows=inspection.parsed.preamble_rows,
        detected_preset=inspection.detected_preset,
        preset=inspection.resolved.preset,
        mapping=dict(inspection.resolved.mapping),
        unmapped=list(inspection.resolved.unmapped),
        sample=[dict(row) for row in inspection.parsed.rows[:SAMPLE_ROWS]],
    )


# --- the LinkedIn archive (spec 10.5, 14.1; P1-20) ---------------------------

ARCHIVE_INVALID: Responses = {
    422: {
        "model": ArchiveRefusalOut,
        "description": "A file or a mapping that cannot be used",
    }
}
"""Every refusal from this endpoint carries a ``code`` (:class:`ArchiveRefusalCode`)
alongside its message — unlike the shared :data:`INVALID`, whose 422 has no
fixed shape because the CSV wizard's refusals have no client depending on one.
"""

ARCHIVE_MAX_UPLOAD_BYTES: Final = 200 * 1024 * 1024
"""``file.size`` above this is refused before the upload is opened as a zip.

Far above any real export — the sample this endpoint was checked against is
under half a megabyte — so this only stops a mistaken, unrelated multi-gigabyte
upload from being processed at all. It is not a request-body limit: Starlette's
multipart parser has already received and spooled the whole upload (to memory,
then to the server's temp directory past 1 MiB) by the time a handler ever
runs, and nothing in this app puts a ceiling on that — see the endpoint
docstring below. What guards an upload that *is* a zip (total uncompressed
size, member count, compression ratio, member paths) lives in
:mod:`netkeeper.linkedin.archive`, so the CLI's ``import archive`` gets the
same protection against a hostile archive that this endpoint does. Both also
refuse a zip whose central directory lost entries, which would otherwise import
partly and silently (:mod:`netkeeper.crm.archive_check`, #138).
"""


def archive_report_out(
    report: ArchiveImport, *, filename: str, ignored_files: list[str]
) -> ArchiveImportOut:
    return ArchiveImportOut(
        filename=filename,
        run_id=report.run_id,
        observed_at=report.observed_at,
        owner_public_id=report.owner_public_id,
        owner_by=report.owner_by,
        connections=ArchiveConnectionCountsOut(
            rows=report.connections.rows,
            created=report.connections.created,
            updated=report.connections.updated,
            needs_review=report.connections.needs_review,
            skipped=report.connections.skipped,
            with_email=report.connections.with_email,
            undated=report.connections.undated,
        ),
        messages=ArchiveMessageCountsOut(
            rows=report.messages.rows,
            conversations=report.messages.conversations,
            attributed=report.messages.attributed,
            no_counterpart=report.messages.no_counterpart,
            group_threads=report.messages.group_threads,
            unknown_contact=report.messages.unknown_contact,
            no_owner=report.messages.no_owner,
            added=report.messages.added,
            already_present=report.messages.already_present,
            undated=report.messages.undated,
            outbound=report.messages.outbound,
            inbound=report.messages.inbound,
        ),
        invitations=ArchiveInvitationCountsOut(
            rows=report.invitations.rows,
            added=report.invitations.added,
            already_present=report.invitations.already_present,
            unknown_contact=report.invitations.unknown_contact,
            no_counterpart=report.invitations.no_counterpart,
            undated=report.invitations.undated,
            undirected=report.invitations.undirected,
        ),
        ignored_files=ignored_files,
        unfamiliar_message_files=list(report.unfamiliar_message_files),
    )


@router.post(
    "/imports/archive",
    operation_id="import_archive",
    status_code=201,
    responses=ARCHIVE_INVALID,
)
def import_archive(
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[
        UploadFile, File(description="The LinkedIn export zip, exactly as downloaded.")
    ],
) -> ArchiveImportOut:
    """Import a LinkedIn export zip: connections, messages, and invitations (P1-20).

    Read straight from the upload FastAPI has already received — ``file.file``
    is the ``SpooledTemporaryFile`` the multipart parser wrote it to, and
    :func:`netkeeper.linkedin.archive.open_archive` reads any seekable binary
    stream, so nothing here copies it into a second in-memory buffer. That
    parsing happens before this handler ever runs, on every upload regardless
    of size, since neither this app nor its FastAPI defaults put a limit on a
    multipart file part; whether that upload is one this importer can even use
    is what is checked here, not whether it was safe to receive. This is a
    plain (not ``async``) handler, like the rest of this router, so FastAPI
    runs it in a worker thread rather than blocking the event loop on it.

    Run through the same :func:`netkeeper.crm.archive.import_archive` that
    ``netkeeper import archive`` uses on a path, so the two report the same
    counts for the same archive. Re-uploading the same export adds nothing
    (see that function's idempotence). A zip that is not a LinkedIn export, or
    one that fails a guard (its total size, member count, compression ratio,
    a member's path, or being password-protected), answers 422 naming what
    was wrong — including one merely damaged in transit, never a 500 — and
    nothing is decompressed before those checks pass. The body
    (:class:`~netkeeper.web.schemas.ArchiveRefusalOut`) carries a ``code``
    (:class:`~netkeeper.linkedin.archive.ArchiveRefusalCode`) alongside the
    message on every refusal, so a client can act on why without parsing it.
    """
    name = file.filename or "upload.zip"
    if file.size is not None and file.size > ARCHIVE_MAX_UPLOAD_BYTES:
        raise ApiError(
            422,
            {
                "detail": (
                    f"{name}: {file.size} bytes, over the {ARCHIVE_MAX_UPLOAD_BYTES} byte limit"
                ),
                "code": ArchiveRefusalCode.TOO_LARGE.value,
            },
        )
    with translate_errors(), open_checked_archive(file.file, filename=name) as archive:
        report = run_archive_import(session, user, archive)
        ignored_files = list(archive.ignored)
    return archive_report_out(report, filename=name, ignored_files=ignored_files)


# --- runs -------------------------------------------------------------------


@router.get("/imports", operation_id="list_import_runs")
def list_import_runs(
    user: CurrentUser,
    session: SessionDep,
    status: ImportStatus | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ImportRunPage:
    """Import runs, newest first. ``status=draft`` is how the orphans left by a
    ``--dry-run`` or a refused commit are found, to finish or delete (#90)."""
    runs, total = service.list_runs(session, user, status=status, limit=limit, offset=offset)
    return ImportRunPage(items=[run_out(run) for run in runs], total=total)


@router.post("/imports", operation_id="create_import_run", status_code=201, responses=INVALID)
def create_import_run(
    body: ImportRunCreate, user: CurrentUser, session: SessionDep
) -> ImportRunOut:
    """Read a file into a draft run: one row per data row, with counts for the whole file.

    A draft writes nothing but the run and its rows. ``preset`` may name a
    built-in preset or one you saved; ``mapping`` overrides it column by column,
    and a column mapped to ``""`` is left out.
    """
    with translate_errors():
        run = service.create_run(
            session,
            user,
            filename=body.filename,
            content=body.content,
            preset_name=body.preset,
            mapping=body.mapping,
            source_kind=body.source_kind,
        )
    return run_out(run)


@router.get("/imports/{run_id}", operation_id="get_import_run", responses=NOT_FOUND)
def get_import_run(run_id: int, user: CurrentUser, session: SessionDep) -> ImportRunOut:
    with translate_errors():
        return run_out(service.get_run(session, user, run_id))


@router.delete(
    "/imports/{run_id}",
    operation_id="delete_import_run",
    status_code=204,
    responses={**NOT_FOUND, **CONFLICT},
)
def delete_import_run(run_id: int, user: CurrentUser, session: SessionDep) -> None:
    """Delete a draft run and its rows. A committed or rolled-back run is refused (#90)."""
    with translate_errors():
        service.delete_run(session, user, run_id)


@router.get("/imports/{run_id}/rows", operation_id="list_import_rows", responses=NOT_FOUND)
def list_import_rows(
    run_id: int,
    user: CurrentUser,
    session: SessionDep,
    resolution: ImportResolution | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ImportRowPage:
    """A run's rows in file order, optionally only those with one resolution."""
    with translate_errors():
        rows, total = service.list_rows(
            session, user, run_id, resolution=resolution, limit=limit, offset=offset
        )
    return ImportRowPage(items=[row_out(row) for row in rows], total=total)


@router.post(
    "/imports/{run_id}/preview",
    operation_id="preview_import_run",
    responses={**NOT_FOUND, **INVALID},
)
def preview_import_run(
    run_id: int,
    user: CurrentUser,
    session: SessionDep,
    limit: int = Query(service.PREVIEW_ROWS, ge=1, le=200),
) -> list[ImportPreviewRow]:
    """Resolve the first rows against the database as it is now, without writing anything.

    Each row says whether it is an existing contact, a candidate waiting for a
    decision, or a new contact, and which fields would change; a change with
    ``refused`` set is one a more authoritative source keeps, a manual edit above
    all (spec 10.5). A ``POST`` because resolving writes inside a savepoint that
    is rolled back, which needs a writer session.
    """
    with translate_errors():
        previewed = service.preview(session, user, run_id, limit=limit)
    return [
        ImportPreviewRow(
            row_number=row.row_number,
            raw=row.raw,
            resolution=row.resolution,
            contact_id=row.contact_id,
            matched_by=row.matched_by,
            candidate_ids=list(row.candidate_ids),
            changes=[change_out(change) for change in row.changes],
            problem=row.problem,
        )
        for row in previewed
    ]


@router.post(
    "/imports/{run_id}/commit",
    operation_id="commit_import_run",
    responses={**NOT_FOUND, **CONFLICT, **INVALID},
)
def commit_import_run(
    run_id: int, body: ImportCommitIn, user: CurrentUser, session: SessionDep
) -> ImportRunOut:
    """Apply every row of a draft run, in this request's one transaction.

    Candidate rows with no decision answer ``409`` unless ``skip_undecided`` is
    set, in which case they are skipped and counted. A row that cannot be applied
    is skipped with its reason on the row; everything else still lands.
    """
    with translate_errors():
        run = service.commit(
            session,
            user,
            run_id,
            decisions=decisions_of(body.decisions),
            undecided=(
                service.UndecidedPolicy.SKIP
                if body.skip_undecided
                else service.UndecidedPolicy.REFUSE
            ),
        )
    return run_out(run)


ROLLBACK_REFUSED: Responses = {
    409: {
        "model": RollbackRefusalOut,
        "description": "The run is not committed, or rolling it back is refused: a contact "
        "it created has a campaign message, a merge drew in a contact it created, a later run "
        "wrote over it, or contacts it created have gained things since (unless force)",
    }
}


def rollback_refusal(exc: service.ImportRunError) -> ApiError:
    """A rollback refusal as a ``409`` carrying a ``code`` a client can act on (#78)."""
    body: RollbackRefusalOut
    if isinstance(exc, service.CreatedContactsMessaged):
        body = RollbackRefusalOut(
            detail=str(exc), code="created_contacts_messaged", contact_ids=list(exc.contact_ids)
        )
    elif isinstance(exc, service.RunMerged):
        body = RollbackRefusalOut(detail=str(exc), code="merged", contact_ids=list(exc.contact_ids))
    elif isinstance(exc, service.RunSuperseded):
        body = RollbackRefusalOut(
            detail=str(exc),
            code="superseded",
            run_ids=list(exc.run_ids),
            contact_ids=list(exc.contact_ids),
        )
    else:
        assert isinstance(exc, service.CreatedContactsChanged)
        acquired = exc.acquired
        body = RollbackRefusalOut(
            detail=str(exc),
            code="created_contacts_changed",
            contact_ids=list(acquired.contact_ids),
            acquired=RollbackAcquiredOut(
                interactions=acquired.interactions,
                tags=acquired.tags,
                list_memberships=acquired.list_memberships,
                triage_decisions=acquired.triage_decisions,
                children=acquired.children,
                edited_contacts=acquired.edited_contacts,
                enriched_contacts=acquired.enriched_contacts,
                later_imports=acquired.later_imports,
                enrollments=acquired.enrollments,
            ),
        )
    return ApiError(409, body.model_dump(mode="json"))


@router.post(
    "/imports/{run_id}/rollback",
    operation_id="rollback_import_run",
    responses={**NOT_FOUND, **ROLLBACK_REFUSED},
)
def rollback_import_run(
    run_id: int,
    user: CurrentUser,
    session: SessionDep,
    force: bool = Query(
        False,
        description="Roll back even though contacts the run created have gained things since; "
        "they are deleted with them. Never overrides a campaign message, a merge or a later run.",
    ),
) -> ImportRollbackOut:
    """Undo a committed run, and only what that run did.

    A contact the run created is deleted; a contact it only enriched keeps the
    values it had before the run, and the child rows the run added are removed. A
    field something changed after the import keeps that later value, and its
    provenance with it.

    Refused with ``409`` and nothing undone, the body's ``code`` saying why
    (#78): ``created_contacts_messaged`` when a contact the run created has a
    campaign message, which is never deleted, so ``force`` does not override it
    (#242); ``merged`` when a merge has since drawn in a contact the run created,
    because deleting it would take rows the run never created; ``superseded``
    when a later run wrote over fields this one wrote, naming the runs to roll
    back first; ``created_contacts_changed`` when contacts the run created have
    gained interactions, tags, lists, campaign enrollments, edits, another
    source's data or later imports, counted in
    ``acquired``, which ``force`` overrides.
    """
    try:
        with translate_errors():
            result = service.rollback(session, user, run_id, force=force)
    except (
        service.CreatedContactsMessaged,
        service.RunMerged,
        service.RunSuperseded,
        service.CreatedContactsChanged,
    ) as exc:
        raise rollback_refusal(exc) from exc
    return ImportRollbackOut(
        run_id=result.run_id,
        contacts_deleted=result.contacts_deleted,
        contacts_restored=result.contacts_restored,
        fields_restored=result.fields_restored,
        children_deleted=result.children_deleted,
    )


__all__ = ["router"]
