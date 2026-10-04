"""What a merge would do, by doing it and taking it back (#363).

The preview runs :func:`netkeeper.crm.contacts.merge_contacts`, the function the
merge route runs, inside a savepoint that is always rolled back. So every rule a
merge follows (the Met rule of #331, the card rules of #186, the history rows and
review marks of #65, the campaign rows of #242, and whatever comes after them)
shows in the preview without a second copy of it that could drift. The import
preview resolves rows the same way (``crm/import_runs.py``, ``_dry_run``).

Rolling back needs a writer session, because the merge writes before the
savepoint is undone; the database keeps none of it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy.orm import InstrumentedAttribute, Session

from netkeeper.crm.contacts import NotFound, live_contact, merge_contacts
from netkeeper.db import is_writer
from netkeeper.models import (
    Contact,
    ContactEmail,
    ContactLink,
    ContactList,
    ContactPhone,
    ContactPosition,
    ContactSnapshot,
    ContactTag,
    Enrollment,
    HistoryRecipient,
    Interaction,
    ListMember,
    Message,
    MessageStatus,
    Tag,
    User,
)
from netkeeper.models.base import UserOwned
from netkeeper.scoping import get_scoped, scoped


@dataclass(frozen=True, slots=True)
class Moved:
    """The loser's rows of one kind: how many move to the survivor, how many are dropped
    because the survivor already holds the same one (an address both had)."""

    moved: int = 0
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class MergeMoves:
    """What a merge moves from the loser to the survivor, as the merge itself did it."""

    emails: Moved = field(default_factory=Moved)
    phones: Moved = field(default_factory=Moved)
    links: Moved = field(default_factory=Moved)
    positions: Moved = field(default_factory=Moved)
    snapshots: int = 0
    interactions: int = 0
    tags_added: tuple[str, ...] = ()
    """Tags the survivor carries afterwards and did not before."""
    tags_removed: tuple[str, ...] = ()
    """Tags the survivor loses: an automatic one the loser's suppression overrules."""
    lists_added: tuple[str, ...] = ()
    """Static lists the survivor joins."""
    enrollments_moved: int = 0
    """The loser's enrollments in campaigns the survivor is not in."""
    enrollments_combined: int = 0
    """The loser's enrollments folded into the survivor's in the same campaign (#242)."""
    messages_moved: int = 0
    messages_discarded: int = 0
    """Unsent messages a combined enrollment discards (#242)."""
    history_rows: int = 0
    """Old-campaign history rows that move (#65)."""


@dataclass(frozen=True, slots=True)
class MergePreview[T]:
    """Both contacts before, the survivor after, rendered by the caller, and the moves."""

    survivor: T
    loser: T
    result: T
    moves: MergeMoves


def preview_merge[T](
    session: Session,
    user: User,
    survivor_id: int,
    loser_id: int,
    *,
    render: Callable[[Contact], T],
) -> MergePreview[T]:
    """Merge ``loser_id`` into ``survivor_id`` in a savepoint, read the result, undo it.

    ``render`` turns a contact into what the caller shows (the API's contact
    detail); it runs on both contacts before the merge and on the survivor after
    it, in the same session the merge ran in, which is what the merge route does
    with its answer. Raises what :func:`~netkeeper.crm.contacts.merge_contacts`
    raises, and ``RuntimeError`` when ``session`` is not a writer. Writes nothing.
    """
    if not is_writer(session):
        raise RuntimeError("a merge preview writes inside a savepoint; use a writer session")
    with _rolled_back(session):
        # merge_contacts checks these too; checking first keeps a refused merge
        # from rendering a "before" for a row it would never touch.
        survivor = live_contact(session, user, survivor_id)
        loser = get_scoped(session, user, Contact, loser_id)
        if loser is None:
            raise NotFound("no such contact")
        before_survivor = render(survivor)
        before_loser = render(loser)
        before = _Holdings.of(session, user, survivor.id, loser.id)
        merged = merge_contacts(session, user, survivor_id, loser_id)
        result = render(merged)
        moves = before.moves(_Holdings.of(session, user, merged.id, loser.id))
    return MergePreview(before_survivor, before_loser, result, moves)


@contextmanager
def _rolled_back(session: Session) -> Iterator[None]:
    nested = session.begin_nested()
    try:
        yield
    finally:
        if nested.is_active:
            nested.rollback()
        # Nothing rolled back may be read back from memory afterwards.
        session.expire_all()


_Column = InstrumentedAttribute[int] | InstrumentedAttribute[int | None]

_CHILDREN: dict[str, tuple[type[UserOwned], InstrumentedAttribute[int], _Column]] = {
    "emails": (ContactEmail, ContactEmail.id, ContactEmail.contact_id),
    "phones": (ContactPhone, ContactPhone.id, ContactPhone.contact_id),
    "links": (ContactLink, ContactLink.id, ContactLink.contact_id),
    "positions": (ContactPosition, ContactPosition.id, ContactPosition.contact_id),
    "snapshots": (ContactSnapshot, ContactSnapshot.id, ContactSnapshot.contact_id),
    "interactions": (Interaction, Interaction.id, Interaction.contact_id),
    "enrollments": (Enrollment, Enrollment.id, Enrollment.contact_id),
    "messages": (Message, Message.id, Message.contact_id),
    "history": (HistoryRecipient, HistoryRecipient.id, HistoryRecipient.contact_id),
}
"""Every kind of row a merge moves by ``contact_id``, which the preview counts."""


def _ids(
    session: Session,
    user: User,
    model: type[UserOwned],
    id_column: InstrumentedAttribute[int],
    contact_column: _Column,
    contact_id: int,
) -> set[int]:
    statement = scoped(user, model).with_only_columns(id_column).where(contact_column == contact_id)
    return set(session.scalars(statement))


@dataclass(frozen=True, slots=True)
class _Holdings:
    """Row ids each contact holds, by kind, plus the survivor's tags and lists."""

    survivor: dict[str, set[int]]
    loser: dict[str, set[int]]
    tags: dict[int, str]
    lists: dict[int, str]
    discarded: set[int]

    @classmethod
    def of(cls, session: Session, user: User, survivor_id: int, loser_id: int) -> _Holdings:
        def held(contact_id: int) -> dict[str, set[int]]:
            return {
                kind: _ids(session, user, *columns, contact_id)
                for kind, columns in _CHILDREN.items()
            }

        mine, theirs = held(survivor_id), held(loser_id)
        tags = session.execute(
            scoped(user, Tag)
            .with_only_columns(Tag.id, Tag.name)
            .where(
                Tag.id.in_(
                    scoped(user, ContactTag)
                    .with_only_columns(ContactTag.tag_id)
                    .where(ContactTag.contact_id == survivor_id)
                )
            )
        ).all()
        lists = session.execute(
            scoped(user, ContactList)
            .with_only_columns(ContactList.id, ContactList.name)
            .where(
                ContactList.id.in_(
                    scoped(user, ListMember)
                    .with_only_columns(ListMember.list_id)
                    .where(ListMember.contact_id == survivor_id)
                )
            )
        ).all()
        messages = mine["messages"] | theirs["messages"]
        discarded = set(
            session.scalars(
                scoped(user, Message)
                .with_only_columns(Message.id)
                .where(Message.id.in_(sorted(messages)), Message.status == MessageStatus.DISCARDED)
            )
        )
        return cls(
            survivor=mine,
            loser=theirs,
            tags={row[0]: row[1] for row in tags},
            lists={row[0]: row[1] for row in lists},
            discarded=discarded,
        )

    def moves(self, after: _Holdings) -> MergeMoves:
        """What moved between this, before the merge, and ``after`` it."""

        def moved(kind: str) -> set[int]:
            return self.loser[kind] & after.survivor[kind]

        def child(kind: str) -> Moved:
            went = moved(kind)
            return Moved(moved=len(went), dropped=len(self.loser[kind] - went))

        enrollments = moved("enrollments")
        return MergeMoves(
            emails=child("emails"),
            phones=child("phones"),
            links=child("links"),
            positions=child("positions"),
            snapshots=len(moved("snapshots")),
            interactions=len(moved("interactions")),
            tags_added=_names(after.tags, self.tags),
            tags_removed=_names(self.tags, after.tags),
            lists_added=_names(after.lists, self.lists),
            enrollments_moved=len(enrollments),
            enrollments_combined=len(self.loser["enrollments"] - enrollments),
            messages_moved=len(moved("messages")),
            messages_discarded=len(after.discarded - self.discarded),
            history_rows=len(moved("history")),
        )


def _names(present: dict[int, str], absent: dict[int, str]) -> tuple[str, ...]:
    return tuple(sorted(name for key, name in present.items() if key not in absent))
