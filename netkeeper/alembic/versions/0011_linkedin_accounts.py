"""LinkedIn accounts, and the settings_kv keys that already belong to one.

Budgets, heat, and the scheduler's next-fire state were keyed by a plain
``account_id`` before this table existed, and every caller passed 1. This
creates one ``default`` account per existing user, in user id order, and
renames each user's account-keyed rows from 1 to that user's new account id.
For the first user the new id is 1 and nothing moves; for any other user the
rows move instead of being orphaned under an id no account has.

The keys moved are exactly the three shapes the code wrote:

* ``linkedin.budget.1.<action>.<period>.<when>`` (``services.budgets``)
* ``linkedin.heat.1`` (``services.heat``)
* ``scheduler.job.1.<kind>`` (``services.scheduler``)

Matched on the full dotted segment, so ``linkedin.budget.10.…`` and
``linkedin.heat.12`` are someone else's and stay put. ``linkedin.session_flag``
is keyed by user alone and is not touched.

The key shapes are restated here rather than imported: a migration is frozen
history and must not track the application's modules.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-23 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The id every caller passed before this table existed.
_LEGACY_ACCOUNT_ID = 1
_DEFAULT_LABEL = "default"


def _renamed(key: str, old: int, new: int) -> str | None:
    """``key`` moved from account ``old`` to ``new``; None when it is not an account key."""
    for prefix in ("linkedin.budget.", "scheduler.job."):
        head = f"{prefix}{old}."
        if key.startswith(head):
            return f"{prefix}{new}.{key[len(head) :]}"
    if key == f"linkedin.heat.{old}":
        return f"linkedin.heat.{new}"
    return None


def _move_keys(connection: sa.Connection, user_id: int, old: int, new: int) -> None:
    if old == new:
        return
    rows = connection.execute(
        sa.text("SELECT id, key FROM settings_kv WHERE user_id = :user_id"),
        {"user_id": user_id},
    ).all()
    taken = {key for _, key in rows}
    for row_id, key in rows:
        moved = _renamed(key, old, new)
        # A key already present at the destination stays where it is rather than
        # failing the unique (user_id, key): only reachable on a downgrade after
        # something wrote under the legacy id again, and neither row is ours to drop.
        if moved is not None and moved not in taken:
            connection.execute(
                sa.text("UPDATE settings_kv SET key = :key WHERE id = :id"),
                {"key": moved, "id": row_id},
            )


def upgrade() -> None:
    op.create_table(
        "linkedin_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_linkedin_accounts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_linkedin_accounts")),
        sa.UniqueConstraint("user_id", "label", name=op.f("uq_linkedin_accounts_user_id_label")),
    )
    op.create_index(
        op.f("ix_linkedin_accounts_user_id"), "linkedin_accounts", ["user_id"], unique=False
    )

    connection = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)  # stored naive UTC, as UTCDateTime does
    user_ids: Sequence[int] = (
        connection.execute(sa.text("SELECT id FROM users ORDER BY id")).scalars().all()
    )
    for user_id in user_ids:
        connection.execute(
            sa.text(
                "INSERT INTO linkedin_accounts (user_id, label, created_at, updated_at)"
                " VALUES (:user_id, :label, :now, :now)"
            ).bindparams(sa.bindparam("now", type_=sa.DateTime())),
            {"user_id": user_id, "label": _DEFAULT_LABEL, "now": now},
        )
        account_id: int = connection.execute(
            sa.text("SELECT id FROM linkedin_accounts WHERE user_id = :user_id AND label = :label"),
            {"user_id": user_id, "label": _DEFAULT_LABEL},
        ).scalar_one()
        _move_keys(connection, user_id, _LEGACY_ACCOUNT_ID, account_id)


def downgrade() -> None:
    connection = op.get_bind()
    accounts = connection.execute(
        sa.text("SELECT id, user_id FROM linkedin_accounts WHERE label = :label ORDER BY id"),
        {"label": _DEFAULT_LABEL},
    ).all()
    for account_id, user_id in accounts:
        _move_keys(connection, user_id, account_id, _LEGACY_ACCOUNT_ID)
    # Dropping a table drops its indexes.
    op.drop_table("linkedin_accounts")
