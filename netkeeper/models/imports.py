"""Import runs and their rows (spec 8.4): what an import did, row by row, so it can be undone.

An ``import_runs`` row is one file a person brought in. An ``import_rows`` row is
one data row of that file: the cells as they were read (``raw_json``), what the
row resolved to (``resolution``), the contact it landed on, the decision a person
made about it when it was a candidate (``decision_json``), and what the commit
actually changed (``changes_json``).

``changes_json`` is what makes a rollback by run id possible (spec 10.5). It does
not hold the new value alone: for every provenance field the run wrote it keeps
the value that was there **before**, the source that was recorded before, and the
``synced_values`` entry that was there before, plus the ids of the child rows the
run created and the fields the run was refused. A rollback puts the "before" side
back on a contact the run only enriched and deletes a contact the run created, so
nothing the run did not do is touched.

Both tables are user-owned (ADR 0005) and every foreign key cascades in the
database, because :func:`netkeeper.scoping.scoped_delete` is a Core delete and
runs no ORM cascade. ``import_rows.contact_id`` is the exception: it is
``SET NULL``, so deleting a contact (a rollback does) leaves the audit row and
its raw cells in place.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum

if TYPE_CHECKING:
    from netkeeper.models.contacts import SyncedValue

FILENAME_MAX_LENGTH = 500
PRESET_NAME_MAX_LENGTH = 100


class ImportSourceKind(enum.StrEnum):
    """Where the rows came from. ``archive`` is the LinkedIn export (P1-03)."""

    ARCHIVE = "archive"
    CSV = "csv"


class ImportStatus(enum.StrEnum):
    """A run is read and resolved (``draft``), then applied, then possibly undone."""

    DRAFT = "draft"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


class ImportResolution(enum.StrEnum):
    """What one row is (spec 8.2, 8.4).

    On a draft run this is the plan; on a committed run it is what happened.
    ``created`` is "this row is a new contact"; ``candidate`` waits for a
    person's decision; ``skipped`` is a row that carried nothing usable, or one
    whose candidacy nobody resolved.
    """

    MATCHED = "matched"
    CREATED = "created"
    CANDIDATE = "candidate"
    SKIPPED = "skipped"


class ImportDecisionKind(enum.StrEnum):
    """What a person decided about a candidate row (:mod:`netkeeper.crm.identity`)."""

    MERGE_INTO = "merge_into"
    CREATE_NEW = "create_new"


class FieldChange(TypedDict):
    """One provenance field the commit wrote: what was there, and what it wrote.

    Both sides are the column's value in JSON form: text, a date in ISO form, or
    ``None``. ``before`` is the half a rollback needs.
    """

    before: str | None
    after: str | None


class RefusedField(TypedDict):
    """A field the row carried that provenance would not let it write (spec 10.5).

    ``source`` is what ``field_sources`` records for it, which outranks the
    incoming source; ``manual`` is a person's own edit and outranks every import.
    """

    field: str
    incoming: str | None
    kept: str | None
    source: str


class RowDecision(TypedDict):
    """``decision_json``: what a person decided about a candidate row."""

    kind: str
    contact_id: int | None


class RowChanges(TypedDict):
    """``changes_json``: everything a rollback of this row needs, and why it did less.

    ``fields`` is per provenance field the "before" and "after" values,
    ``sources`` the ``field_sources`` entry as it was (``None`` when the field
    had none), ``synced`` the ``synced_values`` entry as it was (``None`` when
    there was none), and ``children`` the ids of the child rows the row created,
    keyed by table name. ``refused`` is the audit trail of what provenance kept
    out; a rollback never touches those fields, because the run never wrote them.
    """

    created_contact: bool
    fields: dict[str, FieldChange]
    sources: dict[str, str | None]
    synced: dict[str, SyncedValue | None]
    children: dict[str, list[int]]
    refused: list[RefusedField]


class ImportRun(UserOwned, TimestampMixin, Base):
    """One file, its mapping, its counts, and its state (spec 8.4)."""

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    source_kind: Mapped[ImportSourceKind] = mapped_column(
        string_enum(ImportSourceKind, "import_source_kind"),
        nullable=False,
        default=ImportSourceKind.CSV,
    )
    filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)
    # The preset the mapping came from, when it came from one; the mapping itself
    # is always stored, so a preset that changes later cannot rewrite history.
    preset: Mapped[str | None] = mapped_column(String(PRESET_NAME_MAX_LENGTH))
    # Header name to :class:`netkeeper.crm.importer.ImportField` value.
    mapping_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[ImportStatus] = mapped_column(
        string_enum(ImportStatus, "import_status"), nullable=False, default=ImportStatus.DRAFT
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    rolled_back_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    rows: Mapped[list[ImportRow]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ImportRow.row_number,
    )


class ImportRow(UserOwned, TimestampMixin, Base):
    """One data row of a run: the cells, the resolution, the decision, the effect."""

    __tablename__ = "import_rows"
    __table_args__ = (UniqueConstraint("user_id", "run_id", "row_number"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("import_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1-based, counting data rows only: the header and any preamble are not rows.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Header name to cell, both trimmed, exactly as the file had them.
    raw_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    resolution: Mapped[ImportResolution] = mapped_column(
        string_enum(ImportResolution, "import_resolution"), nullable=False
    )
    # NULL until the row is applied, and again when a rollback deletes the contact.
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    # Which resolution step decided a match (``urn``, ``public_id``, ``alias``, ``email``).
    matched_by: Mapped[str | None] = mapped_column(String(20))
    # The contacts a candidate row might be, for the review screen.
    candidate_ids_json: Mapped[list[int] | None] = mapped_column(JSON)
    decision_json: Mapped[RowDecision | None] = mapped_column(JSON)
    changes_json: Mapped[RowChanges | None] = mapped_column(JSON)
    # Why a row was skipped, or what was dropped from it (a malformed address).
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ImportRun] = relationship(back_populates="rows")


IMPORT_TABLES: tuple[type[UserOwned], ...] = (ImportRun, ImportRow)
"""Every table this module adds, in creation order, for tests and tooling."""
