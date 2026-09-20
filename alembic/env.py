"""Alembic environment.

The URL comes from :func:`netkeeper.db.database_url` and the target metadata from
``netkeeper.models``. :mod:`netkeeper.migrations` may hand over an engine through
``config.attributes`` so the CLI and the tests migrate the database they hold.
"""

from __future__ import annotations

from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Engine

from netkeeper.db import database_url, make_engine, sqlite_foreign_keys_disabled
from netkeeper.migrations import ENGINE_ATTRIBUTE
from netkeeper.models import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render ``UTCDateTime`` as ``sa.DateTime()`` so migrations never import netkeeper."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) without a connection."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on the engine passed in, or on one built from the resolved URL."""
    engine: Engine | None = config.attributes.get(ENGINE_ATTRIBUTE)
    if engine is not None:
        _run_on(engine)
        return
    engine = make_engine(database_url())
    try:
        _run_on(engine)
    finally:
        engine.dispose()


def _run_on(engine: Engine) -> None:
    # One transaction around the whole run, owned here rather than by Alembic so the
    # SQLite foreign-key toggle can bracket it (the pragma is ignored inside one).
    with (
        engine.connect() as connection,
        sqlite_foreign_keys_disabled(connection),
        connection.begin(),
    ):
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
