"""``sync_runs``: one row per LinkedIn extractor run, what it did, and how it ended (spec 8.4).

A run is created the moment somebody (a person, through the API or the CLI, or
the scheduler) asks for one, and it is ``running`` from then until the worker
records how it ended: ``completed`` when it reached its natural end,
``aborted`` when it stopped early and kept what it had (a cancel, the budget,
the active window closing, a response that stopped it), ``failed`` when it
could not run or ended by exception. A run left ``running`` by a process that
went away is marked ``failed`` at the next start (``services.runs``); it is
never picked up again on its own.

Columns beyond spec 8.4's list, each read by something:

* ``linkedin_account_id`` -- the account the run belongs to (budgets, heat, and
  the browser activity lock are per account, ADR 0005).
* ``trigger`` -- ``manual`` or ``scheduled``. A scheduled run may only start
  while the account's scheduled runs are armed (``linkedin_accounts``).
* ``stop_reason`` -- the job's own reason (``end_of_list``, ``budget``,
  ``cancelled``, ``heat_skip`` ...), kept apart from ``status`` so a person can
  see *why* a run was aborted.
* ``plan_json`` -- an enrichment run's plan (spec 9.9): the contact ids in
  visiting order and the ones completed, written in the same transaction as
  each harvest. It moved here from ``settings_kv`` (migration 0013).
* ``cancel_requested_at`` -- spec 9.9's cooperative cancel flag, checked
  between units of work and inside sliced waits.
* ``heartbeat_at`` -- when the process running the run last said it is still
  running it (#467). A live runner refreshes it every
  ``services.runs.HEARTBEAT_EVERY``, so another data directory that shares this
  database can tell a live run from one whose process went away.
* ``max_visits`` -- a manual enrichment's own cap. It only ever lowers the
  day's budget, never raises it.
* ``error`` -- what went wrong, one line, for a ``failed`` run. Never a cookie,
  a token, a body, or a slug.

``progress_json`` and ``counts_json`` hold counts only, as the extractor's
progress events do: no names, no URNs, no slugs. The one exception is a record of
netkeeper's own ids with fixed reason codes: an enrichment's ``unreadable_visits``
(visit number, contact id, reason code; #405) and a connections sync's ``lost``
(list offset, fixed cause).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Final

from sqlalchemy import JSON, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from netkeeper.models.base import Base, TimestampMixin, UserOwned, UTCDateTime, string_enum

STOP_REASON_MAX_LENGTH: Final = 32
BROWSER_MODE_MAX_LENGTH: Final = 16


class SyncRunKind(enum.StrEnum):
    """Spec 8.4's run kinds. ``message_send`` has no runner yet."""

    CONNECTIONS_FULL = "connections_full"
    CONNECTIONS_INCREMENTAL = "connections_incremental"
    ENRICH = "enrich"
    INBOX = "inbox"
    MESSAGE_SEND = "message_send"


class SyncRunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class SyncRunTrigger(enum.StrEnum):
    """Who asked for the run: a person (API or CLI), or the scheduler."""

    MANUAL = "manual"
    SCHEDULED = "scheduled"


class SyncRun(UserOwned, TimestampMixin, Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True, sort_order=-100)
    linkedin_account_id: Mapped[int] = mapped_column(
        ForeignKey("linkedin_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[SyncRunKind] = mapped_column(
        string_enum(SyncRunKind, "sync_run_kind", length=32), nullable=False
    )
    status: Mapped[SyncRunStatus] = mapped_column(
        string_enum(SyncRunStatus, "sync_run_status"),
        nullable=False,
        default=SyncRunStatus.RUNNING,
    )
    trigger: Mapped[SyncRunTrigger] = mapped_column(
        string_enum(SyncRunTrigger, "sync_run_trigger"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    counts_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    stop_reason: Mapped[str | None] = mapped_column(String(STOP_REASON_MAX_LENGTH))
    browser_mode: Mapped[str] = mapped_column(
        String(BROWSER_MODE_MAX_LENGTH), nullable=False, default="attach"
    )
    notes: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    max_visits: Mapped[int | None] = mapped_column(Integer)
    resume_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("sync_runs.id", ondelete="SET NULL"), index=True
    )
