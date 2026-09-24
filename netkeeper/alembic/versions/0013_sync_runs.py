"""Extractor runs (``sync_runs``), and scheduled runs disarmed until a person arms them.

``sync_runs`` records every LinkedIn extractor run (spec 8.4, P2-10). It also
takes over the enrichment plan P2-07 kept in ``settings_kv`` under
``linkedin.enrich.<account>.plan.<id>``: each plan that has anything left to do
becomes an ``aborted`` enrichment run carrying the same contact ids and the
same completed ids, so a resume of that run (``linkedin enrich --resume <run id>``) picks it
up where it stopped. A plan that was ``running`` when this migrated belonged to
a process that is not running this code, so it is recorded ``aborted`` too:
nothing resumes it on its own. A plan that completed has nothing left to
resume and is dropped. Every plan key this moves or drops is deleted; a value
this cannot read, or one whose account does not exist, is left where it is.

``linkedin_accounts.scheduled_runs_armed_at`` is added ``NULL`` for every
account: scheduled runs start disarmed on every install, and nothing here arms
any of them.

A downgrade drops both. Enrichment plans are not written back to
``settings_kv``: an aborted run's plan is lost to the older code.

The key shape is restated here rather than imported: a migration is frozen
history and must not track the application's modules.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-23 00:00:00 UTC
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLAN_KEY = re.compile(r"linkedin\.enrich\.(\d+)\.plan\.([0-9A-Za-z_-]+)")

_settings = sa.table(
    "settings_kv",
    sa.column("id", sa.Integer()),
    sa.column("user_id", sa.Integer()),
    sa.column("key", sa.String()),
    sa.column("value", sa.JSON()),
)
_accounts = sa.table(
    "linkedin_accounts", sa.column("id", sa.Integer()), sa.column("user_id", sa.Integer())
)
_runs = sa.table(
    "sync_runs",
    sa.column("user_id", sa.Integer()),
    sa.column("linkedin_account_id", sa.Integer()),
    sa.column("kind", sa.String()),
    sa.column("status", sa.String()),
    sa.column("trigger", sa.String()),
    sa.column("started_at", sa.DateTime()),
    sa.column("completed_at", sa.DateTime()),
    sa.column("plan_json", sa.JSON()),
    sa.column("stop_reason", sa.String()),
    sa.column("browser_mode", sa.String()),
    sa.column("notes", sa.Text()),
    sa.column("created_at", sa.DateTime()),
    sa.column("updated_at", sa.DateTime()),
)


def upgrade() -> None:
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("linkedin_account_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "connections_full",
                "connections_incremental",
                "enrich",
                "inbox",
                "message_send",
                name="sync_run_kind",
                native_enum=False,
                length=32,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "completed",
                "aborted",
                "failed",
                name="sync_run_status",
                native_enum=False,
                length=16,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "trigger",
            sa.Enum(
                "manual",
                "scheduled",
                name="sync_run_trigger",
                native_enum=False,
                length=16,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("progress_json", sa.JSON(), nullable=True),
        sa.Column("counts_json", sa.JSON(), nullable=True),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("browser_mode", sa.String(length=16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("max_visits", sa.Integer(), nullable=True),
        sa.Column("resume_of_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sync_runs_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["linkedin_account_id"],
            ["linkedin_accounts.id"],
            name=op.f("fk_sync_runs_linkedin_account_id_linkedin_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resume_of_id"],
            ["sync_runs.id"],
            name=op.f("fk_sync_runs_resume_of_id_sync_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(op.f("ix_sync_runs_user_id"), "sync_runs", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_sync_runs_linkedin_account_id"),
        "sync_runs",
        ["linkedin_account_id"],
        unique=False,
    )
    op.create_index(op.f("ix_sync_runs_resume_of_id"), "sync_runs", ["resume_of_id"], unique=False)
    # Nullable, no default: every existing account starts disarmed.
    op.add_column(
        "linkedin_accounts", sa.Column("scheduled_runs_armed_at", sa.DateTime(), nullable=True)
    )
    _move_enrichment_plans(op.get_bind())


def _move_enrichment_plans(connection: sa.Connection) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)  # stored naive UTC, as UTCDateTime does
    accounts = {(row.user_id, row.id) for row in connection.execute(sa.select(_accounts)).all()}
    rows = connection.execute(
        sa.select(_settings.c.id, _settings.c.user_id, _settings.c.key, _settings.c.value).where(
            _settings.c.key.like("linkedin.enrich.%.plan.%")
        )
    ).all()
    for row_id, user_id, key, value in rows:
        match = _PLAN_KEY.fullmatch(key)
        if match is None:
            continue
        account_id = int(match.group(1))
        plan = _readable_plan(value)
        if plan is None or (user_id, account_id) not in accounts:
            continue  # not ours to interpret; left exactly where it is
        contact_ids, completed, status, stopped, created_at = plan
        remaining = [contact_id for contact_id in contact_ids if contact_id not in set(completed)]
        if status != "completed" and remaining:
            connection.execute(
                sa.insert(_runs).values(
                    user_id=user_id,
                    linkedin_account_id=account_id,
                    kind="enrich",
                    status="aborted",
                    trigger="manual",
                    started_at=created_at or now,
                    completed_at=now,
                    plan_json={"contact_ids": contact_ids, "completed": completed},
                    stop_reason=(stopped or "interrupted")[:32],
                    browser_mode="attach",
                    notes=(
                        f"moved from the enrichment plan {match.group(2)} kept in settings_kv"
                        " before runs were recorded (migration 0013)"
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
        connection.execute(sa.delete(_settings).where(_settings.c.id == row_id))


def _readable_plan(
    value: Any,
) -> tuple[list[int], list[int], str, str | None, datetime | None] | None:
    """``(contact ids, completed, status, stopped, created_at)``, or None when unreadable."""
    if not isinstance(value, dict):
        return None
    try:
        contact_ids = [int(item) for item in value["contact_ids"]]
        completed = [int(item) for item in value["completed"]]
        status = str(value["status"])
        stopped = value.get("stopped")
        created_raw = value.get("created_at")
        created_at = (
            datetime.fromisoformat(str(created_raw)).astimezone(UTC).replace(tzinfo=None)
            if created_raw
            else None
        )
    except (KeyError, TypeError, ValueError):
        return None
    if status not in ("running", "aborted", "completed"):
        return None
    return contact_ids, completed, status, None if stopped is None else str(stopped), created_at


def downgrade() -> None:
    op.drop_column("linkedin_accounts", "scheduled_runs_armed_at")
    # Dropping a table drops its indexes.
    op.drop_table("sync_runs")
