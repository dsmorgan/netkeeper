"""Run Alembic programmatically against an engine you already built.

The CLI and the tests go through here rather than the ``alembic`` command so the
database URL is resolved once, by :func:`netkeeper.db.database_url`, and so the
scripts (``netkeeper/alembic/``) are found from any working directory.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

INI_FILENAME = "alembic.ini"
ENGINE_ATTRIBUTE = "engine"  # key in Config.attributes that env.py reads

_PACKAGE_DIR = Path(__file__).resolve().parent


def alembic_root() -> Path:
    """The directory holding ``alembic.ini`` and ``alembic/``: the package itself.

    Keeping the scripts inside the package means a checkout, an editable install,
    and an installed wheel all find them the same way.
    """
    return _PACKAGE_DIR


def alembic_config(engine: Engine | None = None) -> Config:
    """Alembic's config from ``alembic.ini``, carrying ``engine`` to env.py when given."""
    config = Config(alembic_root() / INI_FILENAME)
    if engine is not None:
        config.attributes[ENGINE_ATTRIBUTE] = engine
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Apply migrations up to ``revision``."""
    command.upgrade(alembic_config(engine), revision)


def downgrade(engine: Engine, revision: str = "base") -> None:
    """Revert migrations down to ``revision``."""
    command.downgrade(alembic_config(engine), revision)


def current_revision(engine: Engine) -> str | None:
    """The revision the database is at, or None before the first migration."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision() -> str | None:
    """The newest revision in the scripts directory."""
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def next_revision_id() -> str:
    """The next id in the ``0001``, ``0002``, ... sequence the migrations use."""
    script = ScriptDirectory.from_config(alembic_config())
    ids = [int(rev.revision) for rev in script.walk_revisions() if rev.revision.isdigit()]
    return f"{max(ids, default=0) + 1:04d}"


def create_revision(engine: Engine, message: str) -> list[Path]:
    """Autogenerate a migration from the models/``engine`` diff; return the files written."""
    result = command.revision(
        alembic_config(engine), message=message, autogenerate=True, rev_id=next_revision_id()
    )
    scripts = result if isinstance(result, list) else [result]
    return [Path(script.path) for script in scripts if script is not None]
