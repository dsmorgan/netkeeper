"""Contacts and their child tables (spec 8.1 and 8.2).

A ``contacts`` row is one person in the user's network. The children hold the
many-valued and historical facts: emails, phones, links, positions, a snapshot
of the headline and job at each change, old public-id slugs (aliases) so a
renamed vanity URL still resolves, and interactions. Every table is user-owned
(ADR 0005). Every child references its contact with a database-level cascade:
:func:`netkeeper.scoping.scoped_delete` is a Core delete, which never runs ORM
cascades, so the database has to do it.

Enums are strings plus a CHECK (:func:`netkeeper.models.base.string_enum`).
Moments are ``UTCDateTime``; calendar dates that LinkedIn reports without a time
(``connected_on``, position spans) are plain ``Date``.

Provenance is per field, not per row (spec 10.5): ``contacts.source`` is the
first source, ``contacts.field_sources`` says which source last wrote each
LinkedIn field, ``contacts.synced_values`` keeps the last value an automated
source reported for each (what a manual override reverts to), and every child
row carries its own ``source``. The rule that decides whether a value may be
overwritten, and the ledger's reads and writes, are :mod:`netkeeper.crm.provenance`.

A child row needs its own ``user_id``: the scope guard rejects a flush of an
owned row without one, and nothing copies it from the parent.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import Mapped, declared_attr, mapped_column, relationship, validates

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum, utcnow

# For CONTACT_CHILDREN at the foot of this module: list_members names a contact
# without being one of its children. netkeeper.models.lists imports only
# netkeeper.models.base, so this direction is the only one there is.
from netkeeper.models.lists import ListMember

if TYPE_CHECKING:
    from netkeeper.models.tags import ContactTag, Tag

LINKEDIN_PROFILE_URL = "https://www.linkedin.com/in/{public_id}/"


class ContactMet(enum.StrEnum):
    """Triage: have you met this person (spec 10.2)?"""

    UNKNOWN = "unknown"
    MET = "met"
    NOT_MET = "not_met"
    SKIP = "skip"


class MetSource(enum.StrEnum):
    """Who decided ``met``: the person, or netkeeper on their behalf (spec 10.2).

    ``manual`` is the resting state and covers a contact nobody has decided yet.
    ``automatic`` is set only by a triage batch the person accepted
    (:func:`netkeeper.crm.triage.apply_suggestion`), so a decision netkeeper made
    never reads as one the person made by hand, and the review queue can serve
    exactly those contacts back for checking. No import writes either: ``met``
    stays a field the person owns.
    """

    MANUAL = "manual"
    AUTOMATIC = "automatic"


class ContactSource(enum.StrEnum):
    """Where a row came from. On ``contacts`` the first source; children carry their own.

    For a LinkedIn field (spec 10.5) a recorded ``manual`` outranks everything
    until the person reverts it; among the automated sources ``sync`` beats
    ``archive`` beats ``csv``. The fields a person owns (``preferred_name``,
    ``notes``, ``met``, tags) take ``manual`` and nothing else.
    """

    SYNC = "sync"
    ARCHIVE = "archive"
    CSV = "csv"
    MANUAL = "manual"


class SyncedValue(TypedDict):
    """One entry of ``contacts.synced_values``: what an automated source last reported.

    ``value`` is the field's text, a date in ISO form, or ``None`` for a field
    the source reported empty. ``source`` is a :class:`ContactSource` value other
    than ``manual``. ``observed_at`` is an ISO datetime in UTC. Stored as plain
    JSON, so the types are the JSON ones; :mod:`netkeeper.crm.provenance`
    converts on the way in and out.
    """

    value: str | None
    source: str
    observed_at: str


class EmailKind(enum.StrEnum):
    PERSONAL = "personal"
    WORK = "work"
    OTHER = "other"


class EmailStatus(enum.StrEnum):
    OK = "ok"
    BOUNCED = "bounced"
    INVALID = "invalid"


class PhoneKind(enum.StrEnum):
    MOBILE = "mobile"
    HOME = "home"
    WORK = "work"
    OTHER = "other"


class LinkKind(enum.StrEnum):
    WEBSITE = "website"
    TWITTER = "twitter"
    GITHUB = "github"
    OTHER = "other"


class InteractionKind(enum.StrEnum):
    """What happened between you and a contact. ``*_out`` is you reaching out."""

    NOTE = "note"
    CALL = "call"
    MEETING = "meeting"
    EMAIL_OUT = "email_out"
    EMAIL_IN = "email_in"
    LI_OUT = "li_out"
    LI_IN = "li_in"
    LI_VIEW = "li_view"


def linkedin_profile_url(public_id: str) -> str:
    """The canonical profile URL for a public id (spec 8.1)."""
    return LINKEDIN_PROFILE_URL.format(public_id=public_id)


def normalize_public_id(value: str | None) -> str | None:
    """Lowercase and strip a ``/in/`` slug; empty becomes None.

    LinkedIn resolves slugs case-insensitively, and identity resolution matches
    them exactly (spec 8.2), so one spelling is stored.
    """
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def normalize_email(value: str) -> str:
    """Lowercase and strip an address; identity resolution matches emails exactly (spec 8.2)."""
    cleaned = value.strip().lower()
    if not cleaned:
        raise ValueError("email is empty")
    return cleaned


def _preferred_name_default(context: DefaultExecutionContext) -> str:
    """``preferred_name`` starts as ``first_name`` (spec 8.1); triage changes it.

    A column default rather than a mapper event so a Core ``insert(Contact)``
    gets it too. ``first_name`` precedes this column, so its own default has
    already been applied when this runs.
    """
    # SQLAlchemy 2.0 leaves this method unannotated.
    parameters: dict[str, Any] = context.get_current_parameters()  # type: ignore[no-untyped-call]
    first_name = parameters.get("first_name")
    return first_name if isinstance(first_name, str) else ""


class Contact(UserOwned, TimestampMixin, Base):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("user_id", "li_urn"),
        UniqueConstraint("user_id", "li_public_id"),
        # The naming convention names an index after its first column alone, so
        # every composite index starting with user_id needs an explicit name.
        Index("ix_contacts_user_id_last_name_first_name", "user_id", "last_name", "first_name"),
        Index("ix_contacts_user_id_current_company", "user_id", "current_company"),
        Index("ix_contacts_user_id_met", "user_id", "met"),
        # The review queue: the contacts a batch decided, waiting to be checked.
        Index("ix_contacts_user_id_met_source", "user_id", "met_source"),
        Index("ix_contacts_user_id_archived_at", "user_id", "archived_at"),
        Index("ix_contacts_user_id_last_contacted_at", "user_id", "last_contacted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    # LinkedIn identity. All nullable: a CSV row may carry none of them.
    li_urn: Mapped[str | None] = mapped_column(String(200))
    li_public_id: Mapped[str | None] = mapped_column(String(200))
    li_url: Mapped[str | None] = mapped_column(String(500))
    # Empty rather than NULL when unknown, so sorting and matching never meet
    # three-valued logic.
    first_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    last_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    preferred_name: Mapped[str] = mapped_column(
        String(200), nullable=False, default=_preferred_name_default
    )
    headline: Mapped[str | None] = mapped_column(String(500))
    current_title: Mapped[str | None] = mapped_column(String(300))
    current_company: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))
    connected_on: Mapped[date | None] = mapped_column(Date)
    degree: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    met: Mapped[ContactMet] = mapped_column(
        string_enum(ContactMet, "contact_met"), nullable=False, default=ContactMet.UNKNOWN
    )
    # Who put that value there (spec 10.2). ``automatic`` means a triage batch
    # the person accepted decided it and it is waiting to be reviewed; the
    # decision log says which batch and when.
    met_source: Mapped[MetSource] = mapped_column(
        string_enum(MetSource, "contact_met_source"), nullable=False, default=MetSource.MANUAL
    )
    triaged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    do_not_contact_reason: Mapped[str | None] = mapped_column(String(500))
    # Debounced removal detection (spec 9.8).
    li_missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    li_disconnected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Enrichment scheduling (spec 9.6).
    last_enriched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    enrich_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The NotFound streak (spec 9.8): consecutive enrichment visits that found no
    # profile, when the streak began, and the latest. Three across at least 14 days
    # marks the profile gone (``netkeeper.crm.apply``); a visit that finds it resets
    # all three. The server default is what migration 0012 filled existing rows with.
    li_not_found_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    li_not_found_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    li_not_found_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The last enrichment visit that finished with this contact, whatever it found.
    # A visit that wrote nothing (a profile under another URN, a slug another contact
    # holds, a shape the parser could not read, no profile at all) waits a week before
    # the next, so one such contact cannot cost a profile visit every day
    # (``netkeeper.services.enrich_plan``).
    li_enrich_attempted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Denormalized: the ``at`` of the newest outbound interaction, so the
    # ``last_contacted`` filter and sort (spec 10.4) never scan ``interactions``.
    # Whoever writes an outbound interaction keeps it current (P1-10, then the
    # campaign engine); nothing here recomputes it.
    last_contacted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    source: Mapped[ContactSource] = mapped_column(
        string_enum(ContactSource, "contact_source"), nullable=False, default=ContactSource.MANUAL
    )
    # Per-field provenance (spec 10.5): a LinkedIn field's column name to the
    # ``ContactSource`` value that last wrote it; the fields a person owns are never
    # in here. ``source`` above stays the first source. A ``MutableDict``, so
    # ``contact.field_sources["headline"] = "sync"`` is flushed like any other change.
    field_sources: Mapped[dict[str, str]] = mapped_column(
        MutableDict.as_mutable(JSON()), nullable=False, default=dict
    )
    # The last value each automated source (``sync``, ``archive``, ``csv``) reported
    # for a LinkedIn field, kept whether or not it reached the column, so a manual
    # override can be reverted (spec 10.5, CP1 #28). Keys are provenance fields,
    # values :class:`SyncedValue`. The database default is what migration 0003
    # filled existing rows with; the ORM sends ``{}`` itself.
    synced_values: Mapped[dict[str, SyncedValue]] = mapped_column(
        MutableDict.as_mutable(JSON()),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    # Set on the loser of a merge (spec 8.2). Losing the winner leaves the loser alone.
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )

    merged_into: Mapped[Contact | None] = relationship(remote_side=lambda: Contact.id)
    emails: Mapped[list[ContactEmail]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (ContactEmail.is_primary.desc(), ContactEmail.id),
    )
    phones: Mapped[list[ContactPhone]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (ContactPhone.is_primary.desc(), ContactPhone.id),
    )
    links: Mapped[list[ContactLink]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ContactLink.id,
    )
    positions: Mapped[list[ContactPosition]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (
            ContactPosition.is_current.desc(),
            ContactPosition.started_on.desc().nulls_last(),
            ContactPosition.id.desc(),
        ),
    )
    snapshots: Mapped[list[ContactSnapshot]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (ContactSnapshot.observed_at.desc(), ContactSnapshot.id.desc()),
    )
    aliases: Mapped[list[ContactAlias]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ContactAlias.id,
    )
    interactions: Mapped[list[Interaction]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (Interaction.at.desc(), Interaction.id.desc()),
    )
    # Tags (spec 8.3). ``tag_assignments`` are the rows with their provenance;
    # ``tags`` is the read-only view through them. Owned to owned, as the scope
    # guard requires. The targets are named as strings because ``models.tags``
    # imports this module.
    tag_assignments: Mapped[list[ContactTag]] = relationship(
        "ContactTag",
        back_populates="contact",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ContactTag.id",
    )
    tags: Mapped[list[Tag]] = relationship(
        "Tag", secondary="contact_tags", viewonly=True, order_by="Tag.name_key"
    )

    @validates("li_public_id")
    def _normalize_li_public_id(self, key: str, value: str | None) -> str | None:
        """Store the slug lowercase, and keep ``li_url`` in step with it.

        ``li_url`` is derived when it is unset, or when it is the canonical URL
        of the slug being replaced; a URL that was set by hand is left alone.
        """
        cleaned = normalize_public_id(value)
        if cleaned is not None:
            previous: str | None = self.li_public_id
            derived_before = previous is not None and self.li_url == linkedin_profile_url(previous)
            if self.li_url is None or derived_before:
                self.li_url = linkedin_profile_url(cleaned)
        return cleaned

    @validates("preferred_name")
    def _empty_preferred_name_means_first_name(self, key: str, value: str | None) -> str | None:
        """An empty ``preferred_name`` means "use ``first_name``".

        Before the row is inserted that is None, so the column default reads
        ``first_name`` from the row being written. After the insert no default
        runs, so the fallback happens here.
        """
        cleaned = value.strip() if value is not None else ""
        if cleaned:
            return cleaned
        state = inspect(self)
        return None if state.transient or state.pending else self.first_name


class ContactChild(UserOwned, TimestampMixin):
    """What every child of ``contacts`` carries (spec 8.1): its parent and its provenance.

    ``contact_id`` cascades at the database level, which is what makes
    :func:`netkeeper.scoping.scoped_delete` on ``Contact`` remove children.
    ``source`` and ``observed_at`` are the per-field provenance spec 10.5 uses to
    decide which value may overwrite which.
    """

    @declared_attr
    def contact_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("contacts.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            sort_order=-80,
        )

    source: Mapped[ContactSource] = mapped_column(
        string_enum(ContactSource, "contact_source"),
        nullable=False,
        default=ContactSource.MANUAL,
        sort_order=90,
    )
    observed_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, sort_order=91
    )


class ContactEmail(ContactChild, Base):
    __tablename__ = "contact_emails"
    __table_args__ = (
        UniqueConstraint("user_id", "contact_id", "email"),
        # Identity resolution step 3 looks an address up across the user's contacts.
        Index("ix_contact_emails_user_id_email", "user_id", "email"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    kind: Mapped[EmailKind] = mapped_column(
        string_enum(EmailKind, "email_kind"), nullable=False, default=EmailKind.OTHER
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[EmailStatus] = mapped_column(
        string_enum(EmailStatus, "email_status"), nullable=False, default=EmailStatus.OK
    )

    contact: Mapped[Contact] = relationship(back_populates="emails")

    @validates("email")
    def _normalize_email(self, key: str, value: str) -> str:
        return normalize_email(value)


class ContactPhone(ContactChild, Base):
    __tablename__ = "contact_phones"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    # E.164 when the raw number parsed; the raw form is always kept.
    number_e164: Mapped[str | None] = mapped_column(String(20))
    raw: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[PhoneKind] = mapped_column(
        string_enum(PhoneKind, "phone_kind"), nullable=False, default=PhoneKind.OTHER
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    contact: Mapped[Contact] = relationship(back_populates="phones")


class ContactLink(ContactChild, Base):
    __tablename__ = "contact_links"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    kind: Mapped[LinkKind] = mapped_column(
        string_enum(LinkKind, "link_kind"), nullable=False, default=LinkKind.OTHER
    )

    contact: Mapped[Contact] = relationship(back_populates="links")


class ContactPosition(ContactChild, Base):
    __tablename__ = "contact_positions"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    title: Mapped[str | None] = mapped_column(String(300))
    company: Mapped[str | None] = mapped_column(String(300))
    company_urn: Mapped[str | None] = mapped_column(String(200))
    started_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    contact: Mapped[Contact] = relationship(back_populates="positions")


class ContactSnapshot(ContactChild, Base):
    """The headline and job as they were, written when any of them changes (spec 9.8).

    ``observed_at`` is when the change was seen, so "changed jobs in the last 30
    days" is a query over ``(user_id, observed_at)``.
    """

    __tablename__ = "contact_snapshots"
    __table_args__ = (Index("ix_contact_snapshots_user_id_observed_at", "user_id", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    headline: Mapped[str | None] = mapped_column(String(500))
    current_title: Mapped[str | None] = mapped_column(String(300))
    current_company: Mapped[str | None] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(300))

    contact: Mapped[Contact] = relationship(back_populates="snapshots")


class ContactAlias(ContactChild, Base):
    """An old ``li_public_id`` (spec 8.2). Unique per user, so a slug resolves to one contact."""

    __tablename__ = "contact_aliases"
    __table_args__ = (UniqueConstraint("user_id", "li_public_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    li_public_id: Mapped[str] = mapped_column(String(200), nullable=False)

    contact: Mapped[Contact] = relationship(back_populates="aliases")

    @validates("li_public_id")
    def _normalize_li_public_id(self, key: str, value: str) -> str:
        cleaned = normalize_public_id(value)
        if cleaned is None:
            raise ValueError("li_public_id is empty")
        return cleaned


class Interaction(ContactChild, Base):
    """One thing that happened between you and a contact: the timeline (spec 8.1)."""

    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    kind: Mapped[InteractionKind] = mapped_column(
        string_enum(InteractionKind, "interaction_kind"), nullable=False
    )
    at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    summary: Mapped[str | None] = mapped_column(Text)
    # The messages table arrives with the campaign engine (P3-04). Until then this
    # is a plain integer with no foreign key, so that migration adds the constraint.
    message_id: Mapped[int | None] = mapped_column(Integer)

    contact: Mapped[Contact] = relationship(back_populates="interactions")


CONTACT_CHILDREN: tuple[type[ContactChild] | type[ListMember], ...] = (
    ContactEmail,
    ContactPhone,
    ContactLink,
    ContactPosition,
    ContactSnapshot,
    ContactAlias,
    Interaction,
    ListMember,
)
"""Every table that names a contact, for tests and tooling that iterate them.

The first seven are :class:`ContactChild` subclasses — the contact's own rows,
with its provenance columns. ``ListMember`` is not one of those: a membership
row belongs to a *list* and names a contact, and carries none of the
provenance. It is here because what this tuple is used for is "every row that
points at a contact", which is what the merge tests walk to prove nothing is
left pointing at the loser (#81). Anything needing only the provenance-bearing
children wants :data:`netkeeper.crm.import_runs.CHILD_MODELS` or its own
tuple, not this one."""
