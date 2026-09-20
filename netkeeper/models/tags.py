"""Tags, tag assignments, suppressions, and auto-tag rules (spec 8.3 and 10.3).

A ``tags`` row is a label the user owns; its name is unique per user without
regard to case, which ``name_key`` (the lowercased name) enforces. A
``contact_tags`` row assigns a tag to a contact and says where the assignment
came from: ``manual`` is the user's own, ``rule`` came from an auto-tag rule
(``rule_id`` names it until the rule is deleted), ``llm`` from the optional LLM
module (spec 12). A ``contact_tag_suppressions`` row records that the user
removed a ``rule`` or ``llm`` assignment, so nothing automatic re-adds that tag
to that contact. An ``autotag_rules`` row is a case-insensitive regular
expression over one contact field that feeds one tag; ``position`` orders the
rules for display and for which rule an assignment is credited to.

Every table is user-owned (ADR 0005), every unique constraint includes
``user_id``, and every foreign key cascades at the database level, because
:func:`netkeeper.scoping.scoped_delete` is a Core delete that runs no ORM
cascade. The rules that decide what a run adds, keeps, and removes live in
:mod:`netkeeper.crm.tags`.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from netkeeper.models.base import Base, TimestampMixin, UserOwned, string_enum

if TYPE_CHECKING:
    from netkeeper.models.contacts import Contact

TAG_NAME_MAX_LENGTH = 100


class TagKind(enum.StrEnum):
    """How a tag came to exist: made by hand, by the default rule set, or by the LLM module."""

    MANUAL = "manual"
    AUTO = "auto"
    LLM = "llm"


class TagSource(enum.StrEnum):
    """Who assigned a tag to a contact. Rules never touch a ``manual`` assignment."""

    MANUAL = "manual"
    RULE = "rule"
    LLM = "llm"


class RuleField(enum.StrEnum):
    """The contact field a rule's pattern is searched in (spec 8.3)."""

    TITLE = "title"
    HEADLINE = "headline"
    COMPANY = "company"


def tag_name_key(name: str) -> str:
    """The stored key of a tag name: stripped and lowercased, so ``VP`` and ``vp`` are one tag."""
    return name.strip().lower()


class Tag(UserOwned, TimestampMixin, Base):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(TAG_NAME_MAX_LENGTH), nullable=False)
    # ``name`` lowercased, kept in step by the validator below; the unique is on this.
    name_key: Mapped[str] = mapped_column(String(TAG_NAME_MAX_LENGTH), nullable=False)
    # ``#rrggbb`` or NULL for the UI's default.
    color: Mapped[str | None] = mapped_column(String(7))
    kind: Mapped[TagKind] = mapped_column(
        string_enum(TagKind, "tag_kind"), nullable=False, default=TagKind.MANUAL
    )

    rules: Mapped[list[AutotagRule]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (AutotagRule.position, AutotagRule.id),
    )
    assignments: Mapped[list[ContactTag]] = relationship(
        back_populates="tag",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: ContactTag.id,
    )

    @validates("name")
    def _keep_name_key(self, key: str, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("tag name is empty")
        self.name_key = tag_name_key(cleaned)
        return cleaned


class ContactTag(UserOwned, TimestampMixin, Base):
    """One tag on one contact, with its provenance."""

    __tablename__ = "contact_tags"
    __table_args__ = (
        UniqueConstraint("user_id", "contact_id", "tag_id"),
        # Counting a tag's contacts and the tag filters (spec 10.4) look up by tag.
        Index("ix_contact_tags_user_id_tag_id", "user_id", "tag_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source: Mapped[TagSource] = mapped_column(
        string_enum(TagSource, "tag_source"), nullable=False, default=TagSource.MANUAL
    )
    # The rule credited with a ``rule`` assignment; NULL once that rule is deleted
    # (the assignment stays until the next run finds no rule for its tag).
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("autotag_rules.id", ondelete="SET NULL"), index=True
    )

    contact: Mapped[Contact] = relationship(back_populates="tag_assignments")
    tag: Mapped[Tag] = relationship(back_populates="assignments")


class ContactTagSuppression(UserOwned, TimestampMixin, Base):
    """The user removed an automatic ``tag_id`` from ``contact_id``: rules never re-add it."""

    __tablename__ = "contact_tag_suppressions"
    __table_args__ = (UniqueConstraint("user_id", "contact_id", "tag_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )


class AutotagRule(UserOwned, TimestampMixin, Base):
    """A regular expression over one contact field that assigns one tag (spec 10.3).

    ``pattern`` is a Python regular expression, searched (not matched) with
    ``re.IGNORECASE``; :func:`netkeeper.crm.tags.compile_pattern` validates it
    before it is stored. Evaluation happens in Python so SQLite and PostgreSQL
    behave the same.
    """

    __tablename__ = "autotag_rules"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    field: Mapped[RuleField] = mapped_column(string_enum(RuleField, "rule_field"), nullable=False)
    pattern: Mapped[str] = mapped_column(String(500), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    tag: Mapped[Tag] = relationship(back_populates="rules")


TAG_TABLES: tuple[type[UserOwned], ...] = (Tag, AutotagRule, ContactTag, ContactTagSuppression)
"""Every table this module adds, in creation order, for tests and tooling that iterate them."""
