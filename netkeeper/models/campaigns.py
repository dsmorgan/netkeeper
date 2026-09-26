"""Campaign tables (spec 8.5). Templates so far (item P3-03); campaigns join them in P3-04.

A ``templates`` row is one version of a message template. Versions form a
chain through ``previous_id``: editing a template that an active campaign uses
adds a new row that points back at the one being replaced, and the campaign
keeps the old row until someone upgrades it (spec 8.5). The newest row of a
chain, the one nothing points back at, is the template as the person sees it
and edits it; older rows are kept for the campaigns that still use them.

The unique constraint on ``(user_id, previous_id)`` keeps a chain a line:
a version can be replaced once. ``NULL`` is not equal to ``NULL`` on either
database, so any number of first versions coexist.

Every rule about what may be stored and when a new version is made lives in
:mod:`netkeeper.campaigns.templates`; the render and lint rules in
:mod:`netkeeper.campaigns.render`.
"""

from __future__ import annotations

import enum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned, string_enum

TEMPLATE_NAME_MAX_LENGTH = 200
TEMPLATE_SUBJECT_MAX_LENGTH = 500


class TemplateChannel(enum.StrEnum):
    """Where a template's message goes (spec 8.5)."""

    EMAIL = "email"
    LINKEDIN = "linkedin"


class Template(UserOwned, TimestampMixin, Base):
    """One version of a message template (spec 8.5, 11.1)."""

    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("user_id", "previous_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(TEMPLATE_NAME_MAX_LENGTH), nullable=False)
    channel: Mapped[TemplateChannel] = mapped_column(
        string_enum(TemplateChannel, "template_channel"), nullable=False
    )
    # NULL for no subject. LinkedIn messages have none; an email template without one fails lint.
    subject: Mapped[str | None] = mapped_column(String(TEMPLATE_SUBJECT_MAX_LENGTH))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # The save-time lint result: a list of netkeeper.campaigns.render.LintIssue.to_json()
    # rows. A record for the editor, not the gate: activation lints again, because the
    # ``me.<key>`` fields that exist can change with the config after a save.
    lint_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    # 1 for a first version; each replacement is one more than the row it replaces.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # The version this row replaced. SET NULL rather than CASCADE: deleting an old version
    # must never take the newer ones with it.
    previous_id: Mapped[int | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL"), nullable=True
    )


TEMPLATE_TABLES: tuple[type[UserOwned], ...] = (Template,)
"""Every table this module adds, in creation order, for tests and tooling that iterate them."""
