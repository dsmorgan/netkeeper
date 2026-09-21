"""The user's own job history: source for the you-and-them overlap (spec 8.1, 10.2; #84, #117).

``user_positions`` holds the user's own career, one stint per row. Most of it
arrives from the LinkedIn archive's ``Positions.csv``
(:mod:`netkeeper.linkedin.archive`), imported through :func:`import_positions`
from :mod:`netkeeper.crm.archive`; every row is also readable and editable by
hand through the functions below, because the archive is not the only way
someone has a job history (#117). :mod:`netkeeper.crm.triage` reads the table
to compute the you-and-them overlap on the triage card (#84); the address-book
"shared companies" count there is a separate, older signal and does not use
this table at all.

Re-import matches a row by the natural key contact positions already use
(:func:`netkeeper.crm.identity.position_key`: company and title case-folded,
plus the start date), so importing the same archive twice does not duplicate
a stint. A match is only refreshed when the new observation is at least as new
as the one already recorded -- the same chronological rule the contact child
tables use (see ``_refresh`` in :mod:`netkeeper.crm.identity`), never the full
per-field ranking ``contacts`` has, because a position here is edited whole,
not field by field. A row added or edited by hand keeps its own key and its
own ``manual`` source; a later import only ever touches a row whose key it
matches, and even then only forward in time.

Transactions belong to the caller. Nothing here commits. Every writer reads
before it writes, so it needs a writer session (``session_scope(factory,
write=True)``, or a non-GET request's session).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Final, Literal

from sqlalchemy.orm import Session

from netkeeper.crm.identity import position_key
from netkeeper.db import is_writer
from netkeeper.linkedin.archive import PositionRow
from netkeeper.models import ContactSource, User, UserPosition
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped

log = logging.getLogger(__name__)


class NotFound(LookupError):
    """The position is not one of the user's. Never says which user has it."""


class InvalidPosition(ValueError):
    """A position with neither a title nor a company."""


class _Missing(Enum):
    MISSING = "missing"


MISSING: Final = _Missing.MISSING
"""The default for an :func:`update_position` field that is not being changed."""

type Missing = Literal[_Missing.MISSING]


@dataclass(slots=True)
class PositionCounts:
    """What importing ``Positions.csv`` did. ``rows`` is every non-blank data row read."""

    rows: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    undated: int = 0


# --- manual CRUD -------------------------------------------------------------


def add_position(
    session: Session,
    user: User,
    *,
    title: str | None = None,
    company: str | None = None,
    company_urn: str | None = None,
    started_on: date | None = None,
    ended_on: date | None = None,
    is_current: bool | None = None,
) -> UserPosition:
    """Add one stint to ``user``'s own job history, as a person's own entry.

    ``is_current`` left as ``None`` is inferred: a start with no end is
    current, anything else is not. :class:`InvalidPosition` when neither
    ``title`` nor ``company`` is given; ``RuntimeError`` when ``session`` is
    not a writer.
    """
    _require_writer(session)
    cleaned_title, cleaned_company = _clean(title), _clean(company)
    _require_title_or_company(cleaned_title, cleaned_company)
    row = UserPosition(
        user_id=user.id,
        title=cleaned_title,
        company=cleaned_company,
        company_urn=_clean(company_urn),
        started_on=started_on,
        ended_on=ended_on,
        is_current=_infer_current(started_on, ended_on) if is_current is None else is_current,
        source=ContactSource.MANUAL,
        observed_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def get_position(session: Session, user: User, position_id: int) -> UserPosition:
    """The position with ``position_id`` if it is ``user``'s, else :class:`NotFound`."""
    row = get_scoped(session, user, UserPosition, position_id)
    if row is None:
        raise NotFound(f"position {position_id} is not one of user {user.id}'s")
    return row


def list_positions(session: Session, user: User) -> list[UserPosition]:
    """``user``'s own positions, current first, then most recently started."""
    statement = scoped(user, UserPosition).order_by(
        UserPosition.is_current.desc(),
        UserPosition.started_on.desc().nulls_last(),
        UserPosition.id.desc(),
    )
    return list(session.scalars(statement))


def update_position(
    session: Session,
    user: User,
    position_id: int,
    *,
    title: str | Missing | None = MISSING,
    company: str | Missing | None = MISSING,
    company_urn: str | Missing | None = MISSING,
    started_on: date | Missing | None = MISSING,
    ended_on: date | Missing | None = MISSING,
    is_current: bool | Missing = MISSING,
) -> UserPosition:
    """Change the given fields of one of ``user``'s own positions and return it.

    A field left at :data:`MISSING` is untouched; ``is_current`` is never
    inferred here the way :func:`add_position` infers it on create, because a
    person editing one field of an existing row should not see another one
    move on its own. Editing a position is a person's own act, so it always
    records ``manual`` and a fresh ``observed_at``, whatever source wrote the
    row before. :class:`NotFound`; :class:`InvalidPosition` when the change
    would leave neither a title nor a company; ``RuntimeError`` when
    ``session`` is not a writer.
    """
    _require_writer(session)
    row = get_position(session, user, position_id)
    next_title = _clean(title) if title is not MISSING else row.title
    next_company = _clean(company) if company is not MISSING else row.company
    _require_title_or_company(next_title, next_company)
    row.title = next_title
    row.company = next_company
    if company_urn is not MISSING:
        row.company_urn = _clean(company_urn)
    if started_on is not MISSING:
        row.started_on = started_on
    if ended_on is not MISSING:
        row.ended_on = ended_on
    if is_current is not MISSING:
        row.is_current = is_current
    row.source = ContactSource.MANUAL
    row.observed_at = utcnow()
    session.flush()
    return row


def delete_position(session: Session, user: User, position_id: int) -> None:
    """Delete one of ``user``'s own positions.

    :class:`NotFound`; ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    row = get_position(session, user, position_id)
    session.delete(row)
    session.flush()


# --- archive import -----------------------------------------------------------


def import_positions(
    session: Session,
    user: User,
    rows: Iterable[PositionRow],
    *,
    source: ContactSource,
    observed_at: datetime,
) -> PositionCounts:
    """Upsert ``rows`` (typically ``Positions.csv``) as ``user``'s own positions.

    Matched by :func:`~netkeeper.crm.identity.position_key`. A row that
    matches nothing existing is created; one that matches an existing row is
    refreshed only when ``observed_at`` is at least as new as what that row
    already carries, so re-importing an archive exported before a person's own
    edit never quietly undoes it. A row with neither a title nor a company
    identifies nothing and is only counted, never written.

    ``RuntimeError`` when ``session`` is not a writer; ``ValueError`` for a
    naive ``observed_at``.
    """
    _require_writer(session)
    _require_aware(observed_at)
    counts = PositionCounts()
    existing = {
        position_key(row.company, row.title, row.started_on): row
        for row in session.scalars(scoped(user, UserPosition))
    }
    for row in rows:
        counts.rows += 1
        if row.company is None and row.title is None:
            counts.skipped += 1
            continue
        if row.started_on is None:
            counts.undated += 1
        key = position_key(row.company, row.title, row.started_on)
        current = _infer_current(row.started_on, row.ended_on)
        found = existing.get(key)
        if found is None:
            created = UserPosition(
                user_id=user.id,
                title=row.title,
                company=row.company,
                started_on=row.started_on,
                ended_on=row.ended_on,
                is_current=current,
                source=source,
                observed_at=observed_at,
            )
            session.add(created)
            existing[key] = created
            counts.created += 1
        elif observed_at >= found.observed_at:
            found.source = source
            found.observed_at = observed_at
            if row.ended_on is not None:
                found.ended_on = row.ended_on
            found.is_current = current
            counts.updated += 1
    session.flush()
    return counts


# --- helpers ------------------------------------------------------------------


def _infer_current(started_on: date | None, ended_on: date | None) -> bool:
    """A start with no end is "current"; anything else -- including no dates at all -- is not."""
    return started_on is not None and ended_on is None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _require_title_or_company(title: str | None, company: str | None) -> None:
    if title is None and company is None:
        raise InvalidPosition("a position needs a title or a company")


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError(
            "position writes need a writer session; use session_scope(factory, write=True)"
        )


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
