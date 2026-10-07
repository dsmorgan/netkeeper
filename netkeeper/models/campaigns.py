"""Campaign tables (spec 8.5): templates (item P3-03), campaigns and what they send (P3-04).

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

A ``campaigns`` row is a sequence (spec 11.2) of ``campaign_steps``, sent to
the contacts it has ``enrollments`` for (spec 11.3). ``messages`` is every
message a step produced for an enrollment, and every reply detected to one.
Who may be enrolled, and who may be sent the next step, is decided by
:mod:`netkeeper.services.campaign_guards` (spec 11.9).

What the database refuses to lose (spec 8, "contacts are archived, never
deleted, because messages reference them"): a message's contact, enrollment,
and step are plain foreign keys with no ``ON DELETE`` action, so deleting a
contact, an enrollment, a campaign, or a step that a message names fails,
rather than taking the record of what was sent with it. What was never sent
goes with its owner: a contact's enrollments cascade with the contact, and a
campaign's steps and enrollments with the campaign, so a draft campaign (or a
contact an import rollback removes) can still be deleted while nothing has
been sent. Deleting a user takes everything, as it does for every table.

A step's template is a plain foreign key too: a template a campaign names
cannot be deleted (:func:`netkeeper.campaigns.templates.delete_template`
refuses first, with a reason).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum

TEMPLATE_NAME_MAX_LENGTH = 200
TEMPLATE_SUBJECT_MAX_LENGTH = 500
CAMPAIGN_NAME_MAX_LENGTH = 200
MESSAGE_SUBJECT_MAX_LENGTH = 1000
MESSAGE_SNIPPET_MAX_LENGTH = 500


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


class CampaignStatus(enum.StrEnum):
    """Where a campaign is in its life (spec 8.5, 11.8)."""

    DRAFT = "draft"
    REVIEWING = "reviewing"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class StepMode(enum.StrEnum):
    """What a step does with the message it renders (spec 8.5, 11.5, 11.6).

    ``draft`` and ``send`` are email modes, ``prefill`` and ``auto_send``
    LinkedIn ones; the ``step_channel_mode`` CHECK keeps them to their channel.
    """

    DRAFT = "draft"
    SEND = "send"
    PREFILL = "prefill"
    AUTO_SEND = "auto_send"


class StepCondition(enum.StrEnum):
    """When a step fires (spec 11.2): every time, or only if nobody has replied yet."""

    ALWAYS = "always"
    NO_REPLY = "no_reply"


class EnrollmentStatus(enum.StrEnum):
    """One contact's state in one campaign: spec 11.3's state machine."""

    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    REPLIED = "replied"
    COMPLETED = "completed"
    BOUNCED = "bounced"
    OPTED_OUT = "opted_out"
    REMOVED = "removed"


class MessageDirection(enum.StrEnum):
    OUT = "out"
    IN = "in"


class MessageStatus(enum.StrEnum):
    """Where one message is (spec 11.5, 11.6, 11.7).

    Outbound: ``scheduled`` (rendered, waiting for its send), ``drafted`` (a
    Gmail draft, waiting for you), ``prefilled`` (typed into LinkedIn's compose
    box, waiting for you), ``sent``, ``stale`` (a prefill not seen sent within
    three days), ``discarded`` (a draft you deleted instead of sending),
    ``bounced``, ``failed`` (with ``error``). Inbound: ``received``.
    """

    SCHEDULED = "scheduled"
    DRAFTED = "drafted"
    PREFILLED = "prefilled"
    SENT = "sent"
    STALE = "stale"
    DISCARDED = "discarded"
    BOUNCED = "bounced"
    FAILED = "failed"
    RECEIVED = "received"


class CampaignMailboxLocked(ValueError):
    """A campaign's mailbox changes only while the campaign is a ``draft`` (#269)."""


class Campaign(UserOwned, TimestampMixin, Base):
    """A sequence of steps sent to an audience (spec 8.5, 11.2).

    ``mailbox_id`` is locked once the campaign leaves ``draft`` (#264 question 2,
    #269): the daily caps count a campaign's sends through its current mailbox,
    since ``messages`` has no mailbox column, and a reconcile searches for a
    message in that mailbox by its Message-ID. Setting it on a stored campaign
    that is not a draft raises :class:`CampaignMailboxLocked`.
    """

    __tablename__ = "campaigns"
    __table_args__ = (
        # The name is the Gmail label's (spec 11.5, ``netkeeper/<campaign name>``), so two
        # campaigns of one user sharing it would share a label.
        UniqueConstraint("user_id", "name"),
        # The audience is a list or a filter, never both; a new draft may have neither yet
        # (spec 11.8: a campaign needs an audience to leave ``draft``).
        CheckConstraint(
            "source_list_id IS NULL OR filter_json IS NULL", name="campaign_one_audience"
        ),
        CheckConstraint("daily_cap IS NULL OR daily_cap >= 0", name="campaign_daily_cap"),
        CheckConstraint(
            "contacted_within_days_guard >= 0", name="campaign_contacted_within_days_guard"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    name: Mapped[str] = mapped_column(String(CAMPAIGN_NAME_MAX_LENGTH), nullable=False)
    status: Mapped[CampaignStatus] = mapped_column(
        string_enum(CampaignStatus, "campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )
    # SET NULL: deleting a list must not delete a campaign that was built from it. The
    # enrollments already made are the audience from then on.
    source_list_id: Mapped[int | None] = mapped_column(
        ForeignKey("lists.id", ondelete="SET NULL"), index=True
    )
    # A FilterTree as netkeeper.crm.filters dumps it, like ``lists.filter_json``.
    # ``none_as_null`` so None is SQL NULL, which the CHECK above reads.
    filter_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # No ON DELETE: a mailbox is never deleted, only disconnected (``mailboxes`` since
    # 0019, P3-01), so a campaign never loses the account it sent from.
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailboxes.id"), index=True)
    # The campaign's own send window, from before #338. Nothing reads it any more:
    # netkeeper no longer limits when a campaign sends (``starts_at`` below). Kept so
    # a downgrade has its values back.
    send_window_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    daily_cap: Mapped[int | None] = mapped_column(Integer)
    # The scheduled start (#338): the engine sends nothing for the campaign before it.
    # Set at activation, and changeable until the campaign's first message fires. NULL
    # before activation; an active or paused campaign with none sends nothing.
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Whether a person chose ``starts_at`` (activation, ``campaigns start``, the API).
    # Only a chosen start is exempt from the sending hours on its own day; one 0031
    # backfilled from ``approved_at`` is not (#338 review N5).
    start_chosen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # Spec 11.9's recency guard, in days; 0 turns it off. No default: whatever creates a
    # campaign copies ``[campaigns] contacted_within_days_guard`` from the config, so a
    # later config change never changes a campaign already reviewed.
    contacted_within_days_guard: Mapped[int] = mapped_column(Integer, nullable=False)
    # Set by the review gate (spec 11.8; P3-09) in the transaction that activates the
    # campaign, and only once every requirement below is recorded and current.
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The latest test send (``campaign_test_sends`` holds each one).
    test_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The review's lint record (0024): when every step's template was found free of lint
    # errors, and the content fingerprint it was found for. A later change to a step or
    # template changes the fingerprint, and the record no longer counts.
    lint_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    lint_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Retired (#346): the guard acknowledgement these recorded (0024) no longer exists,
    # and nothing reads or writes them. They stay mapped so the schema matches the
    # migrations; a later migration drops them.
    guards_acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    guards_fingerprint: Mapped[str | None] = mapped_column(String(64))
    guards_summary: Mapped[str | None] = mapped_column(Text)

    @validates("mailbox_id")
    def _lock_mailbox(self, _key: str, value: int | None) -> int | None:
        if inspect(self).key is None:  # not stored yet: nothing has been sent through it
            return value
        if value != self.mailbox_id and self.status is not CampaignStatus.DRAFT:
            raise CampaignMailboxLocked(
                f"campaign {self.id} is {self.status}: its mailbox cannot change once it"
                " leaves draft, because its caps and its sent mail are counted through it"
            )
        return value

    steps: Mapped[list[CampaignStep]] = relationship(
        back_populates="campaign",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: CampaignStep.position,
    )


class CampaignStep(UserOwned, TimestampMixin, Base):
    """One message of a campaign's sequence (spec 8.5, 11.2)."""

    __tablename__ = "campaign_steps"
    __table_args__ = (
        UniqueConstraint("user_id", "campaign_id", "position"),
        CheckConstraint("position >= 1", name="step_position"),
        CheckConstraint("delay_days >= 0", name="step_delay_days"),
        CheckConstraint(
            "(channel = 'email' AND mode IN ('draft', 'send'))"
            " OR (channel = 'linkedin' AND mode IN ('prefill', 'auto_send'))",
            name="step_channel_mode",
        ),
        # Threading is a Gmail idea (spec 11.5); a LinkedIn conversation is one thread anyway.
        CheckConstraint("NOT same_thread OR channel = 'email'", name="step_same_thread"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 1 for the first step. Unique within the campaign.
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[TemplateChannel] = mapped_column(
        string_enum(TemplateChannel, "step_channel"), nullable=False
    )
    # The template version this step sends. No ON DELETE action: see the module docstring.
    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"), nullable=False, index=True)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # An explicit local time of day, ``HH:MM`` (#338): the step is due at that time on
    # the day ``delay_days`` after the step before. NULL: it aims for the next
    # suggested send slot after its delay (``netkeeper.campaigns.schedule``).
    send_time: Mapped[str | None] = mapped_column(String(5))
    mode: Mapped[StepMode] = mapped_column(string_enum(StepMode, "step_mode"), nullable=False)
    condition: Mapped[StepCondition] = mapped_column(
        string_enum(StepCondition, "step_condition"),
        nullable=False,
        default=StepCondition.ALWAYS,
    )
    same_thread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    campaign: Mapped[Campaign] = relationship(back_populates="steps")
    template: Mapped[Template] = relationship()


class Enrollment(UserOwned, TimestampMixin, Base):
    """One contact in one campaign (spec 8.5, 11.3)."""

    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "campaign_id", "contact_id"),
        # The tick's question (spec 11.4): whose next action is due.
        Index(
            "ix_enrollments_user_id_status_next_action_at", "user_id", "status", "next_action_at"
        ),
        CheckConstraint(
            "current_step IS NULL OR current_step >= 1", name="enrollment_current_step"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # CASCADE: an enrollment nothing was sent for goes with its contact. One with messages
    # cannot, because the messages keep both (see the module docstring).
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[EnrollmentStatus] = mapped_column(
        string_enum(EnrollmentStatus, "enrollment_status"),
        nullable=False,
        default=EnrollmentStatus.PENDING,
    )
    # The position of the step that fired most recently; NULL before the first.
    current_step: Mapped[int | None] = mapped_column(Integer)
    next_action_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Why the enrollment left the sequence, once it has: the guard reason or event that
    # ended it (``replied``, ``bounced``, ``do_not_contact`` ...). NULL while it is in it.
    exit_reason: Mapped[str | None] = mapped_column(String(100))
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Per-channel conversation handles the follow-ups reuse (spec 11.5, 11.6): the Gmail
    # thread and step-1 message ids, the LinkedIn conversation URN.
    channel_ids_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # Consecutive sends that certainly sent nothing (``not_sent``, P3-07; 0022), since
    # when, and the latest one's reason: each retry waits longer, and after enough of
    # them over long enough the step is failed for a person (#280). Cleared by any
    # other outcome.
    not_sent_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    not_sent_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    not_sent_error: Mapped[str | None] = mapped_column(String(500))
    # The ``message_send`` run whose outcome was recorded last for this enrollment (#445;
    # 0037). A ``not_typed`` prefill deletes its message, so this is where its run, and
    # so whether it clicked Message or spent a ``li_prefills`` unit, is found again.
    last_prefill_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL")
    )
    # A person overrode the recent-contact guard for this contact at enrollment (#446;
    # 0038): when, who, and the cutoff, the newest outbound contact the guard saw then.
    # Outbound contact dated at or before the cutoff no longer counts as recent at any
    # step fire; anything dated after it still does. Every other guard applies as
    # always. NULL for an enrollment the guard passed, and cleared by a contact merge.
    recent_contact_override_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recent_contact_cutoff: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The user id, without a foreign key: ``UserOwned``'s ``user`` relationship needs
    # ``user_id`` to be the only key to ``users``. Today it is always ``user_id``.
    recent_contact_override_by: Mapped[int | None] = mapped_column(Integer)

    campaign: Mapped[Campaign] = relationship()


OPEN_PREFILL_WHERE = (
    "channel = 'linkedin' AND direction = 'out' AND status IN ('scheduled', 'prefilled')"
    " AND send_clicked_at IS NULL"
)
"""The rows ``uq_messages_one_open_prefill`` holds to one per user: an open prefill. An
auto-sent message (``send_clicked_at`` set, ADR 0008; 0039) waits on the inbox poll, not
on the person, and is not one."""


class Message(UserOwned, TimestampMixin, Base):
    """One message a campaign sent (or tried to), or one reply to it (spec 8.5, 11.5 to 11.7)."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_user_id_status", "user_id", "status"),
        # The inbox poll finds a prefilled message sent by its conversation (P4-02; 0036).
        Index("ix_messages_user_id_li_conversation_urn", "user_id", "li_conversation_urn"),
        # One open LinkedIn prefill per user (spec 11.6; P4-09, 0036), held by the database.
        Index(
            "uq_messages_one_open_prefill",
            "user_id",
            unique=True,
            sqlite_where=text(OPEN_PREFILL_WHERE),
            postgresql_where=text(OPEN_PREFILL_WHERE),
        ),
        # An inbound message is received and nothing else; an outbound one never is.
        CheckConstraint(
            "(direction = 'in') = (status = 'received')", name="message_direction_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    # No ON DELETE action on the next three: see the module docstring.
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id"), nullable=False, index=True
    )
    # The step that produced it; for a reply, the step it answers, when that is known.
    step_id: Mapped[int | None] = mapped_column(ForeignKey("campaign_steps.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), nullable=False, index=True)
    channel: Mapped[TemplateChannel] = mapped_column(
        string_enum(TemplateChannel, "message_channel"), nullable=False
    )
    direction: Mapped[MessageDirection] = mapped_column(
        string_enum(MessageDirection, "message_direction"), nullable=False
    )
    status: Mapped[MessageStatus] = mapped_column(
        string_enum(MessageStatus, "message_status"), nullable=False
    )
    subject: Mapped[str | None] = mapped_column(String(MESSAGE_SUBJECT_MAX_LENGTH))
    # The text as rendered for this contact, stored so what was sent never depends on a
    # template that may have changed since (spec 11.1).
    body_rendered: Mapped[str | None] = mapped_column(Text)
    # An inbound message's snippet, as Gmail gives it (P3-08; 0025): the reply poll
    # stores the subject and this, never the body (spec 11.7). NULL for outbound.
    snippet: Mapped[str | None] = mapped_column(String(MESSAGE_SNIPPET_MAX_LENGTH))
    # The inbox (P3-11b; 0026). Whether an inbound message asked to unsubscribe (spec
    # 11.7's phrases), when detection found an outbound one bounced, and when a person
    # marked either handled. NULL, or false, for everything else.
    asks_unsubscribe: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    bounced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    handled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    gmail_message_id: Mapped[str | None] = mapped_column(String(200))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(200))
    gmail_draft_id: Mapped[str | None] = mapped_column(String(200))
    li_conversation_urn: Mapped[str | None] = mapped_column(String(300))
    li_message_urn: Mapped[str | None] = mapped_column(String(300))
    # A LinkedIn step's prefill (P4-09; 0036): when the run typed it into the composer
    # (``stale`` three days later), and the ``message_send`` run that did.
    prefilled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When the person discarded a LinkedIn prefill (P4-09; 0036): the next step's delay
    # counts from it, as from a send. Nothing else sets it.
    discarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When auto-send's one click on Send was sent for it (P4-04, ADR 0008; 0039). It
    # stays ``prefilled`` until the inbox poll sees it sent. NULL for everything else.
    send_clicked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sync_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), index=True
    )
    # One line on why a send failed. Never a message body, a token, or a header.
    error: Mapped[str | None] = mapped_column(String(500))
    # For the sender's reconcile (P3-07; 0021). The Gmail message ids the thread already
    # held when the draft was made: a draft gone from Gmail reads as sent only for a sent
    # message outside them. NULL for anything but a draft.
    thread_known_json: Mapped[list[str] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    # Searches by Message-ID that found nothing, with the first's and the latest's time. A
    # leftover is ruled "not in Gmail" only after several, spread out: Gmail's search can
    # lag a send by minutes.
    reconcile_misses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    reconcile_first_miss_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reconcile_last_miss_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    enrollment: Mapped[Enrollment] = relationship()


class ReviewPreview(UserOwned, TimestampMixin, Base):
    """One enrollment's rendered previews, viewed in a campaign's review (spec 11.8; P3-09).

    ``sampled`` rows are the server's draw of up to ten, for the audience whose
    fingerprint is ``sample_fingerprint``; the others are enrollments the person
    looked up. Every one must be approved (``approved_at``) for the content
    fingerprint that is current at activation: a change to a step or a template
    after the approval undoes it.
    """

    __tablename__ = "campaign_review_previews"
    __table_args__ = (UniqueConstraint("user_id", "campaign_id", "enrollment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sampled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sample_fingerprint: Mapped[str | None] = mapped_column(String(64))
    viewed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    approved_fingerprint: Mapped[str | None] = mapped_column(String(64))


class StepApproval(UserOwned, TimestampMixin, Base):
    """A campaign step approved in its review (spec 11.8; #339).

    With no ``enrollment_id``, the step is approved as a whole: once, for every
    message of it, including messages rendered later for contacts enrolled or
    edited since. It never covers a message that fails to render, has a lint
    error or is excluded by a guard: those stay blocked.

    A step whose template uses ``{{ personal_line }}`` differs message by message,
    so it is not approved as a whole: each of its messages is approved on its
    own, one row per ``enrollment_id``.

    Each row counts while ``fingerprint`` is the current one: the step's
    (:func:`netkeeper.services.campaign_review.step_fingerprint`), or for one
    message, the step's with the contact's merge values and address. Any change
    to the step or its template undoes it. A partial unique index keeps one
    whole-step row per step, since ``NULL`` is not equal to ``NULL`` in the unique
    constraint.
    """

    __tablename__ = "campaign_step_approvals"
    __table_args__ = (
        UniqueConstraint("user_id", "step_id", "enrollment_id"),
        # NULL is not equal to NULL above, so one whole-step row per step is kept here.
        Index(
            "uq_campaign_step_approvals_whole_step",
            "user_id",
            "step_id",
            unique=True,
            sqlite_where=text("enrollment_id IS NULL"),
            postgresql_where=text("enrollment_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int] = mapped_column(
        ForeignKey("campaign_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enrollment_id: Mapped[int | None] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class TestSend(UserOwned, TimestampMixin, Base):
    """One test send of an email step to the campaign's own mailbox (spec 11.8; P3-09).

    Not a :class:`Message`: nothing that counts caps, recency or an enrollment's
    progress reads this table, so a test send can never count toward any of them.
    It counts for the review only while ``fingerprint`` is the step's current one.

    On a mailbox armed for drafts only, the test is a Gmail draft instead (#304):
    ``gmail_draft_id`` is set, and ``sent_at`` is when it was drafted. The drafts
    check searches for ``rfc822_message_id``, the Message-ID the test went in
    with, to verify the mailbox (:attr:`Mailbox.message_id_verified_at`), and sets
    ``not_found_at`` when a search found nothing, so a refused arming to send can say so.
    """

    __tablename__ = "campaign_test_sends"
    __test__ = False  # not a pytest class, whatever its name

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int] = mapped_column(
        ForeignKey("campaign_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    gmail_message_id: Mapped[str | None] = mapped_column(String(200))
    gmail_draft_id: Mapped[str | None] = mapped_column(String(200))
    rfc822_message_id: Mapped[str | None] = mapped_column(String(200))
    not_found_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


REVIEW_TABLES: tuple[type[UserOwned], ...] = (ReviewPreview, StepApproval, TestSend)
"""The review gate's tables (P3-09, #339), for tests and tooling that iterate them."""

TEMPLATE_TABLES: tuple[type[UserOwned], ...] = (Template,)
"""The tables P3-03 added, for tests and tooling that iterate them."""

CAMPAIGN_TABLES: tuple[type[UserOwned], ...] = (Campaign, CampaignStep, Enrollment, Message)
"""The tables P3-04 added, in creation order, for tests and tooling that iterate them."""
