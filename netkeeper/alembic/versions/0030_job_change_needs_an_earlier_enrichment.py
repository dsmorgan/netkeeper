"""A job change needs an earlier enrichment: clear the flag on the rest (#323, spec 9.8).

Until #323, ``contact_snapshots.position_changed`` (0028) was set whenever a
non-empty ``current_title`` or ``current_company`` was replaced by a different
value, whatever wrote the old value. A contact's first enrichment replaces the
title and company the LinkedIn archive or a CSV wrote, so it read as a job
change on the dashboard. The rule now is that only an enrichment that finds a
different position than an earlier enrichment recorded is a job change.

This clears ``position_changed`` on every snapshot that the rule does not
support:

- A snapshot whose source is not ``sync``: a person's edit or an import
  notices nothing.
- A ``sync`` snapshot with no earlier enrichment of the position it replaced.
  The evidence for one is a ``contact_positions`` row of the same contact with
  source ``sync``, created before the snapshot was observed, that was open when
  it was recorded (no ``ended_on``, or an ``ended_on`` no earlier than the
  first of the month it was created in), and whose title matches the
  snapshot's non-empty ``current_title`` or whose company matches its
  non-empty ``current_company``. Values match as identity resolution matches
  positions: trimmed and case-folded (a frozen copy of ``identity._fold``).
  Only a profile visit writes a ``sync`` position, and a visit writes its
  positions after it observes the profile, so the positions of the visit that
  wrote the snapshot are never earlier than it. A position that had already
  ended was a past job on that visit, not the current position it recorded.

A snapshot the rule supports keeps its flag, and a false flag is never set.
Running the upgrade twice changes nothing the second time.

The downgrade is lossy and does nothing, like 0010's. The flags this clears
were wrong, no schema changed, and code before this revision reads the
corrected rows as fewer job changes, which is harmless. Restoring them would
need a record of which rows changed, and the data cannot tell those rows from
snapshots that were false all along.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-02 00:00:00 UTC
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_snapshots = sa.table(
    "contact_snapshots",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Integer()),
    sa.column("contact_id", sa.Integer()),
    sa.column("source", sa.String()),
    sa.column("observed_at", sa.DateTime()),
    sa.column("current_title", sa.String()),
    sa.column("current_company", sa.String()),
    sa.column("position_changed", sa.Boolean()),
)

_positions = sa.table(
    "contact_positions",
    sa.column("user_id", sa.Integer()),
    sa.column("contact_id", sa.Integer()),
    sa.column("source", sa.String()),
    sa.column("created_at", sa.DateTime()),
    sa.column("ended_on", sa.Date()),
    sa.column("title", sa.String()),
    sa.column("company", sa.String()),
)


def _fold(value: str | None) -> str | None:
    """Frozen copy of identity resolution's ``_fold``: trimmed, case-folded, empty is None."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned.casefold() if cleaned else None


def _open_when_recorded(created_at: datetime, ended_on: date | None) -> bool:
    """A position ending no earlier than the month it was first recorded in was current."""
    return ended_on is None or ended_on >= date(created_at.year, created_at.month, 1)


def upgrade() -> None:
    connection = op.get_bind()
    flagged = connection.execute(
        sa.select(
            _snapshots.c.id,
            _snapshots.c.user_id,
            _snapshots.c.contact_id,
            _snapshots.c.source,
            _snapshots.c.observed_at,
            _snapshots.c.current_title,
            _snapshots.c.current_company,
        ).where(_snapshots.c.position_changed.is_(sa.true()))
    ).all()
    contacts = {(row.user_id, row.contact_id) for row in flagged if row.source == "sync"}
    enriched = defaultdict(list)
    if contacts:
        positions = connection.execute(
            sa.select(
                _positions.c.user_id,
                _positions.c.contact_id,
                _positions.c.created_at,
                _positions.c.ended_on,
                _positions.c.title,
                _positions.c.company,
            ).where(
                _positions.c.source == "sync",
                _positions.c.contact_id.in_(sorted({contact for _, contact in contacts})),
            )
        ).all()
        for position in positions:
            enriched[(position.user_id, position.contact_id)].append(position)

    def supported(row: sa.Row[tuple[object, ...]]) -> bool:
        if row.source != "sync":
            return False
        title, company = _fold(row.current_title), _fold(row.current_company)
        return any(
            position.created_at < row.observed_at
            and _open_when_recorded(position.created_at, position.ended_on)
            and (
                (title is not None and _fold(position.title) == title)
                or (company is not None and _fold(position.company) == company)
            )
            for position in enriched[(row.user_id, row.contact_id)]
        )

    cleared = sorted(row.id for row in flagged if not supported(row))
    for start in range(0, len(cleared), 500):
        connection.execute(
            sa.update(_snapshots)
            .where(_snapshots.c.id.in_(cleared[start : start + 500]))
            .values(position_changed=False)
        )


def downgrade() -> None:
    # Lossy on purpose: see the module docstring.
    pass
