"""Migrations against a fresh database: SQLite always, PostgreSQL when
NETKEEPER_TEST_DATABASE_URL points at one.

CI (.github/workflows/ci.yml) sets that variable against a postgres:16 service, so
a construct that only SQLite accepts fails there, not on a hosted install. To run
the PostgreSQL params locally, start a throwaway server and point the variable at it:

    docker run --rm -e POSTGRES_PASSWORD=netkeeper -e POSTGRES_USER=netkeeper \
        -e POSTGRES_DB=netkeeper_test -p 5432:5432 postgres:16
    NETKEEPER_TEST_DATABASE_URL=postgresql+psycopg://netkeeper:netkeeper@localhost/netkeeper_test \
        make test
"""

import io
import json
import os
import re
import tokenize
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, MetaData, inspect, text
from sqlalchemy.exc import IntegrityError

from netkeeper import migrations
from netkeeper.db import database_url, make_engine
from netkeeper.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIR = migrations.alembic_root() / "alembic" / "versions"
PG_ENV = "NETKEEPER_TEST_DATABASE_URL"

# Text only SQLite understands. Alembic's batch mode (render_as_batch,
# batch_alter_table) is deliberately absent: it is how Alembic edits tables on
# SQLite and renders as a plain ALTER TABLE everywhere else.
SQLITE_ONLY_MARKERS: dict[str, re.Pattern[str]] = {
    "sqlite_autoincrement": re.compile(r"sqlite_autoincrement"),
    "PRAGMA": re.compile(r"\bPRAGMA\b", re.IGNORECASE),
    "strftime(": re.compile(r"\bstrftime\s*\(", re.IGNORECASE),
    # The SQL keyword, not SQLAlchemy's portable ``autoincrement=`` column argument.
    "AUTOINCREMENT": re.compile(r"\bAUTOINCREMENT\b(?!\s*=)", re.IGNORECASE),
    "WITHOUT ROWID": re.compile(r"\bWITHOUT\s+ROWID\b", re.IGNORECASE),
}

BACKENDS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "postgresql",
        id="postgresql",
        marks=pytest.mark.skipif(not os.environ.get(PG_ENV), reason=f"{PG_ENV} not set"),
    ),
]


def _drop_everything(engine: Engine) -> None:
    reflected = MetaData()
    reflected.reflect(engine)
    reflected.drop_all(engine)


@pytest.fixture(params=BACKENDS)
def migration_engine(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """An empty database on the requested backend."""
    backend: str = request.param
    if backend == "sqlite":
        engine = make_engine(database_url(tmp_path))
    else:
        engine = make_engine(os.environ[PG_ENV])
        _drop_everything(engine)
    try:
        yield engine
    finally:
        if backend == "postgresql":
            _drop_everything(engine)
        engine.dispose()


def _revision_chain() -> list[str]:
    """Revision ids from the first migration to head, in upgrade order."""
    script = ScriptDirectory.from_config(migrations.alembic_config())
    return [rev.revision for rev in reversed(list(script.walk_revisions()))]


def _strip_comments(source: str) -> str:
    """Drop comment and docstring tokens so prose that mentions a marker is not flagged."""
    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    try:
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.string.lstrip("rRbBuUfF").startswith(
                ('"""', "'''")
            ):
                continue
            kept.append(tok.string)
    except tokenize.TokenError:
        return source
    return " ".join(kept)


def _sqlite_only_markers(source: str) -> list[str]:
    code = _strip_comments(source)
    return [name for name, pattern in SQLITE_ONLY_MARKERS.items() if pattern.search(code)]


def _diff_against_models(engine: Engine) -> list[Any]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        diff: list[Any] = compare_metadata(context, Base.metadata)
        return diff


def test_fresh_database_migrates_to_head_and_matches_the_models(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    assert migrations.current_revision(migration_engine) == migrations.head_revision()
    # A model change without a migration (or the reverse) shows up here.
    assert _diff_against_models(migration_engine) == []


def test_upgrade_downgrade_upgrade(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    migrations.downgrade(migration_engine)
    assert migrations.current_revision(migration_engine) is None
    assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}
    migrations.upgrade(migration_engine)
    assert migrations.current_revision(migration_engine) == migrations.head_revision()
    assert _diff_against_models(migration_engine) == []


def test_every_revision_upgrades_and_downgrades_one_step_at_a_time(
    migration_engine: Engine,
) -> None:
    """Each migration's downgrade has to work on its own, not only inside a full run."""
    chain = _revision_chain()
    assert chain[0] == "0001"
    for revision in chain:
        migrations.upgrade(migration_engine, "+1")
        assert migrations.current_revision(migration_engine) == revision
    assert _diff_against_models(migration_engine) == []
    expected: str | None
    for expected in [*reversed(chain[:-1]), None]:
        migrations.downgrade(migration_engine, "-1")
        assert migrations.current_revision(migration_engine) == expected
    assert set(inspect(migration_engine).get_table_names()) <= {"alembic_version"}


def test_user_kind_is_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    insert = text(
        "INSERT INTO users (kind, timezone, created_at) VALUES (:kind, 'UTC', :created_at)"
    )
    with migration_engine.begin() as connection:
        connection.execute(insert, {"kind": "local", "created_at": "2026-09-20 12:00:00"})
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"kind": "other", "created_at": "2026-09-20 12:00:00"})


def test_settings_key_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    stamp = "2026-09-20 12:00:00"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (1, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (2, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
    insert = text(
        "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
        " VALUES (:user_id, 'k', '1', :t, :t)"
    )
    with migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "t": stamp})
        connection.execute(insert, {"user_id": 2, "t": stamp})  # same key, other user: fine
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "t": stamp})


def test_deleting_a_user_cascades_to_its_settings(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    stamp = "2026-09-20 12:00:00"
    with migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at) VALUES (1, 'local', 'UTC', :t)"
            ),
            {"t": stamp},
        )
        connection.execute(
            text(
                "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
                " VALUES (1, 'k', '1', :t, :t)"
            ),
            {"t": stamp},
        )
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        remaining = connection.execute(text("SELECT count(*) FROM settings_kv")).scalar()
    assert remaining == 0


# --- contacts (0002) --------------------------------------------------------

STAMP = "2026-09-20 12:00:00"


def _seed_users(connection: Connection, *ids: int) -> None:
    for user_id in ids:
        connection.execute(
            text(
                "INSERT INTO users (id, kind, timezone, created_at)"
                " VALUES (:id, 'local', 'UTC', :t)"
            ),
            {"id": user_id, "t": STAMP},
        )


def _insert_contact(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    li_urn: str | None = None,
    met: str = "unknown",
    source: str = "manual",
    merged_into_id: int | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO contacts (id, user_id, li_urn, first_name, last_name, preferred_name,"
            " degree, met, do_not_contact, li_missing_count, enrich_priority, source,"
            " field_sources, synced_values, merged_into_id, created_at, updated_at)"
            " VALUES (:id, :user_id, :li_urn, 'F', 'L', 'F', 1, :met, false, 0, 0, :source,"
            " '{}', '{}', :merged_into_id, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "li_urn": li_urn,
            "met": met,
            "source": source,
            "merged_into_id": merged_into_id,
            "t": STAMP,
        },
    )


# The columns each child table needs beyond the shared ones, with portable literals.
CHILD_ROWS = {
    "contact_emails": ("email, kind, is_primary, status", "'a@example.test', 'work', true, 'ok'"),
    "contact_phones": ("raw, kind, is_primary", "'+15550100', 'mobile', true"),
    "contact_links": ("url, kind", "'https://example.test', 'website'"),
    "contact_positions": ("is_current", "true"),
    "contact_snapshots": ("headline", "'then'"),
    "contact_aliases": ("li_public_id", ":slug"),  # unique per user
    "interactions": ("kind, at", "'note', :t"),
}


def _insert_children(connection: Connection, *, user_id: int, contact_id: int) -> None:
    for table, (columns, values) in CHILD_ROWS.items():
        connection.execute(
            text(
                f"INSERT INTO {table} (user_id, contact_id, {columns}, source, observed_at,"
                f" created_at, updated_at)"
                f" VALUES (:user_id, :contact_id, {values}, 'manual', :t, :t, :t)"
            ),
            {
                "user_id": user_id,
                "contact_id": contact_id,
                "slug": f"old-slug-{contact_id}",
                "t": STAMP,
            },
        )


def _count(connection: Connection, table: str) -> int:
    return int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_migration_creates_every_contact_table(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    names = set(inspect(migration_engine).get_table_names())
    assert {"contacts", *CHILD_ROWS} <= names


def test_contact_li_urn_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_contact(connection, id=1, user_id=1, li_urn="urn:li:fsd_profile/X")
        _insert_contact(connection, id=2, user_id=2, li_urn="urn:li:fsd_profile/X")  # fine
        _insert_contact(connection, id=3, user_id=1, li_urn=None)
        _insert_contact(connection, id=4, user_id=1, li_urn=None)  # NULLs never collide
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_contact(connection, id=5, user_id=1, li_urn="urn:li:fsd_profile/X")


@pytest.mark.parametrize("column", ["met", "source"])
def test_contact_enums_are_checked_by_the_database(migration_engine: Engine, column: str) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
    bad: dict[str, Any] = {column: "bogus"}
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_contact(connection, id=2, user_id=1, **bad)


def test_child_enums_are_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
    insert = text(
        "INSERT INTO contact_emails (user_id, contact_id, email, kind, is_primary, status,"
        " source, observed_at, created_at, updated_at)"
        " VALUES (1, 1, 'a@example.test', :kind, false, 'ok', :source, :t, :t, :t)"
    )
    with migration_engine.begin() as connection:
        connection.execute(insert, {"kind": "work", "source": "sync", "t": STAMP})
    for bad in ({"kind": "bogus", "source": "sync"}, {"kind": "work", "source": "bogus"}):
        with pytest.raises(IntegrityError), migration_engine.begin() as connection:
            connection.execute(insert, {**bad, "t": STAMP})


def test_deleting_a_contact_cascades_to_every_child_table(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_contact(connection, id=2, user_id=1)
        _insert_children(connection, user_id=1, contact_id=1)
        _insert_children(connection, user_id=1, contact_id=2)
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        for table in CHILD_ROWS:
            assert _count(connection, table) == 1, table
        connection.execute(text("DELETE FROM users WHERE id = 1"))  # user -> contacts -> children
        assert _count(connection, "contacts") == 0
        for table in CHILD_ROWS:
            assert _count(connection, table) == 0, table


def test_deleting_a_merge_winner_clears_merged_into(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_contact(connection, id=2, user_id=1, merged_into_id=1)
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        rows = connection.execute(text("SELECT id, merged_into_id FROM contacts")).all()
    assert [tuple(row) for row in rows] == [(2, None)]


def test_alias_slug_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    insert = text(
        "INSERT INTO contact_aliases (user_id, contact_id, li_public_id, source, observed_at,"
        " created_at, updated_at) VALUES (:user_id, :contact_id, 'old-slug', 'sync', :t, :t, :t)"
    )
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_contact(connection, id=1, user_id=1)
        _insert_contact(connection, id=2, user_id=1)
        _insert_contact(connection, id=3, user_id=2)
        connection.execute(insert, {"user_id": 1, "contact_id": 1, "t": STAMP})
        connection.execute(insert, {"user_id": 2, "contact_id": 3, "t": STAMP})  # other user
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "contact_id": 2, "t": STAMP})


# --- synced values (0003) ---------------------------------------------------


def _json(value: Any) -> Any:
    """A JSON column read through ``text()``: a string on SQLite, parsed already on psycopg."""
    return json.loads(value) if isinstance(value, str) else value


def test_synced_values_fills_existing_contacts_and_downgrades_cleanly(
    migration_engine: Engine,
) -> None:
    """0003 adds a NOT NULL column to a table that may hold rows; the database default fills it."""
    migrations.upgrade(migration_engine, "0002")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        connection.execute(
            text(
                "INSERT INTO contacts (id, user_id, first_name, last_name, preferred_name, degree,"
                " met, do_not_contact, li_missing_count, enrich_priority, source, field_sources,"
                " created_at, updated_at)"
                " VALUES (1, 1, 'F', 'L', 'F', 1, 'unknown', false, 0, 0, 'manual', '{}', :t, :t)"
            ),
            {"t": STAMP},
        )
    migrations.upgrade(migration_engine, "0003")
    select = text("SELECT synced_values FROM contacts WHERE id = 1")
    with migration_engine.begin() as connection:
        assert _json(connection.execute(select).scalar_one()) == {}
        _insert_contact(connection, id=2, user_id=1)  # a new row names the column
        connection.execute(  # and one that leaves it out gets the default
            text(
                "INSERT INTO contacts (id, user_id, first_name, last_name, preferred_name, degree,"
                " met, do_not_contact, li_missing_count, enrich_priority, source, field_sources,"
                " created_at, updated_at)"
                " VALUES (3, 1, 'F', 'L', 'F', 1, 'unknown', false, 0, 0, 'manual', '{}', :t, :t)"
            ),
            {"t": STAMP},
        )
        rows = connection.execute(text("SELECT synced_values FROM contacts ORDER BY id")).all()
    assert [_json(row[0]) for row in rows] == [{}, {}, {}]
    migrations.downgrade(migration_engine, "0002")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("contacts")}
    assert "synced_values" not in columns and "field_sources" in columns
    with migration_engine.connect() as connection:
        assert _count(connection, "contacts") == 3  # the rows survive the round trip
    migrations.upgrade(migration_engine)
    assert _diff_against_models(migration_engine) == []


# --- tags (0004) ------------------------------------------------------------

TAG_TABLES = ("tags", "autotag_rules", "contact_tags", "contact_tag_suppressions")


def _insert_tag(
    connection: Connection, *, id: int, user_id: int, name: str, kind: str = "auto"
) -> None:
    connection.execute(
        text(
            "INSERT INTO tags (id, user_id, name, name_key, color, kind, created_at, updated_at)"
            " VALUES (:id, :user_id, :name, :key, NULL, :kind, :t, :t)"
        ),
        {"id": id, "user_id": user_id, "name": name, "key": name.lower(), "kind": kind, "t": STAMP},
    )


def _insert_rule(
    connection: Connection, *, id: int, user_id: int, tag_id: int, field: str = "title"
) -> None:
    connection.execute(
        text(
            "INSERT INTO autotag_rules (id, user_id, tag_id, field, pattern, enabled, position,"
            " created_at, updated_at) VALUES (:id, :user_id, :tag_id, :field, 'x', true, 0, :t, :t)"
        ),
        {"id": id, "user_id": user_id, "tag_id": tag_id, "field": field, "t": STAMP},
    )


def _insert_contact_tag(
    connection: Connection,
    *,
    user_id: int,
    contact_id: int,
    tag_id: int,
    source: str = "rule",
    rule_id: int | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO contact_tags (user_id, contact_id, tag_id, source, rule_id, created_at,"
            " updated_at) VALUES (:user_id, :contact_id, :tag_id, :source, :rule_id, :t, :t)"
        ),
        {
            "user_id": user_id,
            "contact_id": contact_id,
            "tag_id": tag_id,
            "source": source,
            "rule_id": rule_id,
            "t": STAMP,
        },
    )


def _insert_suppression(
    connection: Connection, *, user_id: int, contact_id: int, tag_id: int
) -> None:
    connection.execute(
        text(
            "INSERT INTO contact_tag_suppressions (user_id, contact_id, tag_id, created_at,"
            " updated_at) VALUES (:user_id, :contact_id, :tag_id, :t, :t)"
        ),
        {"user_id": user_id, "contact_id": contact_id, "tag_id": tag_id, "t": STAMP},
    )


def test_migration_creates_every_tag_table(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    assert set(TAG_TABLES) <= set(inspect(migration_engine).get_table_names())


def test_tag_name_key_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_tag(connection, id=1, user_id=1, name="VP")
        _insert_tag(connection, id=2, user_id=2, name="vp")  # other user: fine
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_tag(connection, id=3, user_id=1, name="vp")


def test_tag_enums_are_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_tag(connection, id=1, user_id=1, name="a")
        _insert_rule(connection, id=1, user_id=1, tag_id=1)
        _insert_contact_tag(connection, user_id=1, contact_id=1, tag_id=1, rule_id=1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_tag(connection, id=2, user_id=1, name="b", kind="bogus")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_rule(connection, id=2, user_id=1, tag_id=1, field="bogus")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_contact_tag(connection, user_id=1, contact_id=1, tag_id=1, source="bogus")


def test_an_assignment_and_a_suppression_are_unique_per_contact_and_tag(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_tag(connection, id=1, user_id=1, name="a")
        _insert_contact_tag(connection, user_id=1, contact_id=1, tag_id=1)
        _insert_suppression(connection, user_id=1, contact_id=1, tag_id=1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_contact_tag(connection, user_id=1, contact_id=1, tag_id=1, source="manual")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_suppression(connection, user_id=1, contact_id=1, tag_id=1)


def test_deleting_a_tag_cascades_and_deleting_a_rule_clears_rule_id(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        for tag_id in (1, 2):
            _insert_tag(connection, id=tag_id, user_id=1, name=f"tag{tag_id}")
            _insert_rule(connection, id=tag_id, user_id=1, tag_id=tag_id)
            _insert_contact_tag(connection, user_id=1, contact_id=1, tag_id=tag_id, rule_id=tag_id)
            _insert_suppression(connection, user_id=1, contact_id=1, tag_id=tag_id)
        connection.execute(text("DELETE FROM autotag_rules WHERE id = 1"))
        rows = connection.execute(
            text("SELECT tag_id, rule_id FROM contact_tags ORDER BY tag_id")
        ).all()
        assert [tuple(row) for row in rows] == [(1, None), (2, 2)]
        connection.execute(text("DELETE FROM tags WHERE id = 2"))
        assert _count(connection, "autotag_rules") == 0
        assert _count(connection, "contact_tags") == 1
        assert _count(connection, "contact_tag_suppressions") == 1
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        assert _count(connection, "contact_tags") == 0
        assert _count(connection, "contact_tag_suppressions") == 0
        assert _count(connection, "tags") == 1
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        for table in TAG_TABLES:
            assert _count(connection, table) == 0, table


# --- imports (0005) ---------------------------------------------------------

IMPORT_TABLES = ("import_runs", "import_rows")
RAW_CELLS = """{"Given": "Hortensia"}"""


def _insert_import_run(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    source_kind: str = "csv",
    status: str = "draft",
) -> None:
    connection.execute(
        text(
            "INSERT INTO import_runs (id, user_id, source_kind, filename, preset, mapping_json,"
            " status, total_rows, matched_count, created_count, candidate_count, skipped_count,"
            " committed_at, rolled_back_at, created_at, updated_at)"
            " VALUES (:id, :user_id, :source_kind, 'people.csv', NULL, '{}', :status,"
            " 0, 0, 0, 0, 0, NULL, NULL, :t, :t)"
        ),
        {"id": id, "user_id": user_id, "source_kind": source_kind, "status": status, "t": STAMP},
    )


def _insert_import_row(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    run_id: int,
    row_number: int = 1,
    resolution: str = "created",
    contact_id: int | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO import_rows (id, user_id, run_id, row_number, raw_json, resolution,"
            " contact_id, matched_by, candidate_ids_json, decision_json, changes_json, error,"
            f" created_at, updated_at) VALUES (:id, :user_id, :run_id, :row_number, '{RAW_CELLS}',"
            " :resolution, :contact_id, NULL, NULL, NULL, NULL, NULL, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "run_id": run_id,
            "row_number": row_number,
            "resolution": resolution,
            "contact_id": contact_id,
            "t": STAMP,
        },
    )


def _raw_cells(connection: Connection, row_id: int) -> Any:
    """``raw_json`` as a dict: SQLite hands back the text, PostgreSQL the parsed JSON."""
    value = connection.execute(
        text("SELECT raw_json FROM import_rows WHERE id = :id"), {"id": row_id}
    ).scalar_one()
    return json.loads(value) if isinstance(value, str) else value


def test_migration_creates_the_import_tables(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    assert set(IMPORT_TABLES) <= set(inspect(migration_engine).get_table_names())


def test_import_enums_are_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_import_run(connection, id=1, user_id=1)
        _insert_import_row(connection, id=1, user_id=1, run_id=1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_import_run(connection, id=2, user_id=1, source_kind="bogus")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_import_run(connection, id=3, user_id=1, status="bogus")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_import_row(connection, id=2, user_id=1, run_id=1, row_number=2, resolution="bogus")


def test_a_row_number_is_unique_within_a_run(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_import_run(connection, id=1, user_id=1)
        _insert_import_run(connection, id=2, user_id=2)
        _insert_import_row(connection, id=1, user_id=1, run_id=1, row_number=1)
        _insert_import_row(connection, id=2, user_id=1, run_id=1, row_number=2)  # same run: fine
        _insert_import_row(connection, id=3, user_id=2, run_id=2, row_number=1)  # other user: fine
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_import_row(connection, id=4, user_id=1, run_id=1, row_number=1)


def test_deleting_a_contact_leaves_the_import_row_with_its_raw_cells(
    migration_engine: Engine,
) -> None:
    """SET NULL, not CASCADE: a rollback deletes the contact and the audit row stays."""
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_import_run(connection, id=1, user_id=1, status="committed")
        _insert_import_row(connection, id=1, user_id=1, run_id=1, contact_id=1)
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))

        assert _count(connection, "import_rows") == 1
        assert (
            connection.execute(text("SELECT contact_id FROM import_rows WHERE id = 1")).scalar_one()
            is None
        )
        assert _raw_cells(connection, 1) == {"Given": "Hortensia"}


def test_deleting_a_run_cascades_to_its_rows(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_import_run(connection, id=1, user_id=1)
        _insert_import_run(connection, id=2, user_id=1)
        _insert_import_row(connection, id=1, user_id=1, run_id=1)
        _insert_import_row(connection, id=2, user_id=1, run_id=2)
        connection.execute(text("DELETE FROM import_runs WHERE id = 1"))

        assert _count(connection, "import_rows") == 1
        assert _count(connection, "import_runs") == 1


def test_deleting_a_user_cascades_to_both_import_tables(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        for user_id in (1, 2):
            _insert_import_run(connection, id=user_id, user_id=user_id)
            _insert_import_row(connection, id=user_id, user_id=user_id, run_id=user_id)
        connection.execute(text("DELETE FROM users WHERE id = 1"))

        assert _count(connection, "import_runs") == 1
        assert _count(connection, "import_rows") == 1
        connection.execute(text("DELETE FROM users WHERE id = 2"))
        for table in IMPORT_TABLES:
            assert _count(connection, table) == 0, table


# --- script directory -------------------------------------------------------


def test_alembic_root_is_the_package_directory() -> None:
    root = migrations.alembic_root()
    assert root == REPO_ROOT / "netkeeper"
    assert (root / "alembic.ini").is_file()
    assert (root / "alembic" / "env.py").is_file()
    assert (root / "alembic" / "script.py.mako").is_file()


def test_revision_ids_are_sequential() -> None:
    head = migrations.head_revision()
    assert head is not None and head.isdigit() and len(head) == 4
    assert migrations.next_revision_id() == f"{int(head) + 1:04d}"


def test_migration_files_do_not_import_netkeeper() -> None:
    """Migrations are frozen history; they must not track the models module."""
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        assert "netkeeper" not in path.read_text(), path


# --- portability ------------------------------------------------------------


def test_migration_files_use_no_sqlite_only_constructs() -> None:
    """The PostgreSQL params catch these at run time; this names them at a glance."""
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        assert _sqlite_only_markers(path.read_text()) == [], path


@pytest.mark.parametrize(
    ("source", "markers"),
    [
        (
            'op.create_table("t", sa.Column("id", sa.Integer()), sqlite_autoincrement=True)',
            ["sqlite_autoincrement"],
        ),
        ('op.execute("PRAGMA foreign_keys=OFF")', ["PRAGMA"]),
        ('op.execute("pragma journal_mode=WAL")', ["PRAGMA"]),
        ("""server_default=sa.text("(strftime('%s','now'))")""", ["strftime("]),
        ('op.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT)")', ["AUTOINCREMENT"]),
        ('op.execute("CREATE TABLE t (id INTEGER PRIMARY KEY) WITHOUT ROWID")', ["WITHOUT ROWID"]),
        ("context.configure(render_as_batch=True)", []),
        ('with op.batch_alter_table("users") as batch_op:', []),
        ('sa.Column("id", sa.Integer(), autoincrement=True)', []),
        ("# PostgreSQL has no PRAGMA equivalent\nop.execute('SELECT 1')", []),
        ('"""Avoid strftime( here."""\nop.execute("SELECT 1")', []),
    ],
)
def test_sqlite_only_marker_scan(source: str, markers: list[str]) -> None:
    assert _sqlite_only_markers(source) == markers


# --- lists and saved views (0006) -------------------------------------------


def _insert_list(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    name: str,
    kind: str = "static",
    filter_json: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO lists (id, user_id, name, kind, filter_json, created_at, updated_at)"
            " VALUES (:id, :user_id, :name, :kind, :filter_json, :now, :now)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "name": name,
            "kind": kind,
            "filter_json": filter_json,
            "now": STAMP,
        },
    )


def test_a_lists_kind_and_filter_must_agree_in_the_database(migration_engine: Engine) -> None:
    """The repository's first cross-column CHECK, and the one thing 0006 promises that
    Alembic's ``compare_metadata`` cannot see: it does not diff CHECK constraints, so
    nothing else notices if a later migration drops this."""
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_list(connection, id=1, user_id=1, name="static ok")
        _insert_list(connection, id=2, user_id=1, name="smart ok", kind="smart", filter_json="{}")
    # A static list carrying a filter, a smart list without one, and an unknown kind.
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_list(connection, id=3, user_id=1, name="static with filter", filter_json="{}")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_list(connection, id=4, user_id=1, name="smart without", kind="smart")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_list(connection, id=5, user_id=1, name="bogus kind", kind="bogus")


def test_a_list_name_is_unique_per_user_and_its_rows_cascade(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_contact(connection, id=1, user_id=1)
        _insert_list(connection, id=1, user_id=1, name="First 100")
        _insert_list(connection, id=2, user_id=2, name="First 100")  # another user may reuse it
        connection.execute(
            text(
                "INSERT INTO list_members (user_id, list_id, contact_id, added_at)"
                " VALUES (1, 1, 1, :now)"
            ),
            {"now": STAMP},
        )
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_list(connection, id=3, user_id=1, name="First 100")

    # Deleting the contact takes its membership with it; the list stays.
    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        assert connection.execute(text("SELECT count(*) FROM list_members")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM lists")).scalar() == 2

    # Deleting the user takes the list and the saved views with it (ADR 0005).
    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        assert (
            connection.execute(text("SELECT count(*) FROM lists WHERE user_id = 1")).scalar() == 0
        )


# --- triage (0007) ----------------------------------------------------------


def _insert_decision(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    contact_id: int,
    kind: str = "decide",
    batch_id: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO triage_decisions (id, user_id, contact_id, kind, before_state,"
            " after_state, batch_id, decided_at, undone_at, created_at, updated_at)"
            " VALUES (:id, :user_id, :contact_id, :kind, '{}', '{}', :batch_id, :now, NULL,"
            " :now, :now)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "contact_id": contact_id,
            "kind": kind,
            "batch_id": batch_id,
            "now": STAMP,
        },
    )


def test_a_triage_decision_kind_is_checked_by_the_database(migration_engine: Engine) -> None:
    """The four kinds and nothing else.

    ``compare_metadata`` does not diff CHECK constraints (see the portability
    note above), so without this nothing notices if a later migration drops it
    and the undo log starts taking kinds no reader knows.
    """
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        kinds = ("decide", "preferred_name", "bulk_met", "bulk_not_met")
        for index, kind in enumerate(kinds, start=1):
            _insert_decision(connection, id=index, user_id=1, contact_id=1, kind=kind)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_decision(connection, id=5, user_id=1, contact_id=1, kind="bogus")


def test_deleting_a_contact_or_its_user_takes_the_triage_log_with_it(
    migration_engine: Engine,
) -> None:
    """Both cascades are the database's, and ``ondelete`` is not diffed either.

    ``scoped_delete`` is a Core delete, which runs no ORM cascade, so a decision
    row left behind would point at a contact that no longer exists (spec 8.1,
    ADR 0005).
    """
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_contact(connection, id=1, user_id=1)
        _insert_contact(connection, id=2, user_id=1)
        _insert_contact(connection, id=3, user_id=2)
        _insert_decision(connection, id=1, user_id=1, contact_id=1)
        _insert_decision(connection, id=2, user_id=1, contact_id=2, kind="bulk_met", batch_id="b1")
        _insert_decision(connection, id=3, user_id=2, contact_id=3)

    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        remaining = connection.execute(
            text("SELECT id FROM triage_decisions ORDER BY id")
        ).scalars()
        assert list(remaining) == [2, 3]

    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        remaining = connection.execute(
            text("SELECT id FROM triage_decisions ORDER BY id")
        ).scalars()
        assert list(remaining) == [3]


# --- the user's own positions (0009) -----------------------------------------


def _insert_user_position(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    company: str = "Acme",
    source: str = "archive",
) -> None:
    connection.execute(
        text(
            "INSERT INTO user_positions (id, user_id, company, is_current, source,"
            " observed_at, created_at, updated_at)"
            " VALUES (:id, :user_id, :company, false, :source, :t, :t, :t)"
        ),
        {"id": id, "user_id": user_id, "company": company, "source": source, "t": STAMP},
    )


def test_migration_creates_the_user_positions_table(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    assert "user_positions" in set(inspect(migration_engine).get_table_names())


def test_user_position_source_is_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_user_position(connection, id=1, user_id=1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_user_position(connection, id=2, user_id=1, source="bogus")


def test_deleting_a_user_cascades_to_its_positions(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_user_position(connection, id=1, user_id=1)
        _insert_user_position(connection, id=2, user_id=2)
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        assert _count(connection, "user_positions") == 1
        remaining = connection.execute(text("SELECT user_id FROM user_positions")).scalar()
    assert remaining == 2


# --- plain-text message summaries (0010) --------------------------------------


def _insert_interaction(
    connection: Connection,
    *,
    id: int,
    user_id: int,
    contact_id: int,
    summary: str | None,
    source: str = "archive",
) -> None:
    connection.execute(
        text(
            "INSERT INTO interactions (id, user_id, contact_id, kind, at, summary, source,"
            " observed_at, created_at, updated_at)"
            " VALUES (:id, :user_id, :contact_id, 'li_in', :t, :summary, :source, :t, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "contact_id": contact_id,
            "summary": summary,
            "source": source,
            "t": STAMP,
        },
    )


def test_the_backfill_cleans_only_archive_sourced_summaries(migration_engine: Engine) -> None:
    """B2 (pre-merge review of #127): migration 0010 had no test at all -- dropping its
    ``source = 'archive'`` restriction, or gutting ``upgrade()`` entirely, left the whole
    suite green. This pins both: the archive row is cleaned, the other three sources are
    byte-identical, and a NULL summary is left alone rather than turned into a string.
    """
    migrations.upgrade(migration_engine, "0009")
    html = "<p>Hello &amp; welcome</p>"
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_interaction(
            connection, id=1, user_id=1, contact_id=1, summary=html, source="archive"
        )
        _insert_interaction(
            connection, id=2, user_id=1, contact_id=1, summary=html, source="manual"
        )
        _insert_interaction(connection, id=3, user_id=1, contact_id=1, summary=html, source="sync")
        _insert_interaction(connection, id=4, user_id=1, contact_id=1, summary=html, source="csv")
        _insert_interaction(
            connection, id=5, user_id=1, contact_id=1, summary=None, source="archive"
        )
    migrations.upgrade(migration_engine, "0010")
    with migration_engine.begin() as connection:
        found = connection.execute(text("SELECT id, summary FROM interactions ORDER BY id")).all()
    rows: dict[int, str | None] = {row[0]: row[1] for row in found}
    assert rows[1] == "Hello & welcome"  # the only row the backfill is allowed to touch
    assert rows[2] == html
    assert rows[3] == html
    assert rows[4] == html
    assert rows[5] is None  # a NULL summary is left alone, not turned into the string "None"


def test_the_backfill_never_leaves_a_known_tag_stored_even_via_entities(
    migration_engine: Engine,
) -> None:
    """The same entity-decode-before-strip ordering bug that #75's importer fix addresses
    applies to the backfill too, since it duplicates the same logic."""
    migrations.upgrade(migration_engine, "0009")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_interaction(
            connection,
            id=1,
            user_id=1,
            contact_id=1,
            summary="She wrote &lt;p&gt;hello&lt;/p&gt; in the box",
            source="archive",
        )
    migrations.upgrade(migration_engine, "0010")
    with migration_engine.begin() as connection:
        summary = connection.execute(
            text("SELECT summary FROM interactions WHERE id = 1")
        ).scalar_one()
    assert summary == "She wrote\nhello\nin the box"
    assert "<" not in summary and ">" not in summary


def test_ci_runs_the_postgresql_params() -> None:
    """Without the URL the PostgreSQL params skip silently, so CI must always set it."""
    if not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("only meaningful on GitHub Actions")
    assert os.environ.get(PG_ENV), f"CI must set {PG_ENV}; see .github/workflows/ci.yml"


# --- the automatic first pass (0008) ----------------------------------------


def test_met_source_fills_existing_contacts_and_is_checked(migration_engine: Engine) -> None:
    """0008 rebuilds ``contacts`` on SQLite to add a checked column; rows already there keep up."""
    migrations.upgrade(migration_engine, "0007")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1, met="met")
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        source = connection.execute(text("SELECT met_source FROM contacts WHERE id = 1")).scalar()
        # A contact decided before the column existed was decided by hand or not at all.
        assert source == "manual"
        assert connection.execute(text("SELECT met FROM contacts WHERE id = 1")).scalar() == "met"
        connection.execute(text("UPDATE contacts SET met_source = 'automatic' WHERE id = 1"))
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text("UPDATE contacts SET met_source = 'bogus' WHERE id = 1"))


def test_a_tag_met_signal_is_checked_by_the_database(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_tag(connection, id=1, user_id=1, name="recruiter")
        assert connection.execute(text("SELECT met_signal FROM tags WHERE id = 1")).scalar() is None
        for signal in ("met", "not_met"):
            connection.execute(
                text("UPDATE tags SET met_signal = :signal WHERE id = 1"), {"signal": signal}
            )
        connection.execute(text("UPDATE tags SET met_signal = NULL WHERE id = 1"))
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text("UPDATE tags SET met_signal = 'maybe' WHERE id = 1"))


def test_an_import_run_starts_with_no_tagging_counts(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0007")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_import_run(connection, id=1, user_id=1)
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        counts = connection.execute(
            text("SELECT tagged_contacts, tags_added, tags_removed FROM import_runs WHERE id = 1")
        ).one()
    assert tuple(counts) == (0, 0, 0)


def test_downgrading_drops_the_decisions_the_old_schema_cannot_hold(
    migration_engine: Engine,
) -> None:
    """A ``bulk_not_met`` row fails 0007's CHECK, so the downgrade removes those and no others."""
    migrations.upgrade(migration_engine)
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_decision(connection, id=1, user_id=1, contact_id=1)
        _insert_decision(
            connection, id=2, user_id=1, contact_id=1, kind="bulk_not_met", batch_id="b1"
        )
    migrations.downgrade(migration_engine, "0007")
    with migration_engine.begin() as connection:
        kept = connection.execute(text("SELECT id FROM triage_decisions ORDER BY id")).scalars()
        assert list(kept) == [1]


# --- linkedin accounts, and the keys that already belonged to one (0011) --------------


def _put_setting(connection: Connection, user_id: int, key: str, value: str = "1") -> None:
    connection.execute(
        text(
            "INSERT INTO settings_kv (user_id, key, value, created_at, updated_at)"
            " VALUES (:user_id, :key, :value, :t, :t)"
        ),
        {"user_id": user_id, "key": key, "value": value, "t": STAMP},
    )


def _keys(connection: Connection, user_id: int) -> set[str]:
    rows = connection.execute(
        text("SELECT key FROM settings_kv WHERE user_id = :user_id"), {"user_id": user_id}
    ).scalars()
    return set(rows)


#: What CP3's code wrote for account 1, plus neighbors that only look like it.
LEGACY_KEYS = {
    "linkedin.budget.1.connection_pages.day.2026-09-22",
    "linkedin.budget.1.profile_visits.week.2026-W39",
    "linkedin.heat.1",
    "scheduler.job.1.connections_full",
    "linkedin.session_flag",  # keyed by user alone
    "linkedin.budget.10.connection_pages.day.2026-09-22",  # account 10, not 1
    "linkedin.heat.12",
    "tags.defaults_seeded",
}


def test_every_user_gets_an_account_and_keeps_their_counters(migration_engine: Engine) -> None:
    """The first user's account is 1 and nothing moves; the second's is 2 and its keys follow."""
    migrations.upgrade(migration_engine, "0010")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        for key in LEGACY_KEYS:
            _put_setting(connection, 1, key)
            _put_setting(connection, 2, key)

    migrations.upgrade(migration_engine, "0011")

    with migration_engine.begin() as connection:
        accounts = connection.execute(
            text("SELECT user_id, id, label FROM linkedin_accounts ORDER BY user_id")
        ).all()
        assert [tuple(row) for row in accounts] == [(1, 1, "default"), (2, 2, "default")]
        assert _keys(connection, 1) == LEGACY_KEYS
        assert _keys(connection, 2) == {
            "linkedin.budget.2.connection_pages.day.2026-09-22",
            "linkedin.budget.2.profile_visits.week.2026-W39",
            "linkedin.heat.2",
            "scheduler.job.2.connections_full",
            "linkedin.session_flag",
            "linkedin.budget.10.connection_pages.day.2026-09-22",
            "linkedin.heat.12",
            "tags.defaults_seeded",
        }

    migrations.downgrade(migration_engine, "0010")

    assert "linkedin_accounts" not in set(inspect(migration_engine).get_table_names())
    with migration_engine.begin() as connection:
        assert _keys(connection, 1) == LEGACY_KEYS
        assert _keys(connection, 2) == LEGACY_KEYS


def test_an_account_label_is_unique_per_user_and_goes_with_its_user(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine)
    insert = text(
        "INSERT INTO linkedin_accounts (user_id, label, created_at, updated_at)"
        " VALUES (:user_id, :label, :t, :t)"
    )
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        connection.execute(insert, {"user_id": 1, "label": "default", "t": STAMP})
        connection.execute(insert, {"user_id": 2, "label": "default", "t": STAMP})
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(insert, {"user_id": 1, "label": "default", "t": STAMP})
    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        assert _count(connection, "linkedin_accounts") == 1


# --- extractor runs; plans move off settings_kv; everyone disarmed (0013) --------------


def _plan_value(status: str, contact_ids: list[int], completed: list[int]) -> str:
    return json.dumps(
        {
            "version": 1,
            "plan_id": "x",
            "account_id": 1,
            "created_at": "2026-09-22T10:00:00+00:00",
            "status": status,
            "contact_ids": contact_ids,
            "completed": completed,
            "cancel_requested": False,
            "stopped": "cancelled" if status == "aborted" else None,
        }
    )


def test_unfinished_plans_become_resumable_runs_and_nobody_is_armed(
    migration_engine: Engine,
) -> None:
    """An in-flight plan (running or aborted, work left) becomes an aborted enrichment
    run with the same plan; a finished plan is dropped; what cannot be read, or whose
    account is not the user's, stays where it is. No account comes out armed."""
    migrations.upgrade(migration_engine, "0012")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        for user_id in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO linkedin_accounts (user_id, label, created_at, updated_at)"
                    " VALUES (:user_id, 'default', :t, :t)"
                ),
                {"user_id": user_id, "t": STAMP},
            )
        _put_setting(
            connection, 1, "linkedin.enrich.1.plan.aaaa", _plan_value("aborted", [5, 6, 7], [5])
        )
        _put_setting(
            connection, 1, "linkedin.enrich.1.plan.bbbb", _plan_value("running", [8, 9], [])
        )
        _put_setting(
            connection, 1, "linkedin.enrich.1.plan.cccc", _plan_value("completed", [1], [1])
        )
        _put_setting(connection, 1, "linkedin.enrich.1.plan.dddd", _plan_value("aborted", [2], [2]))
        _put_setting(connection, 1, "linkedin.enrich.1.plan.eeee", '"not a plan"')
        _put_setting(connection, 1, "linkedin.enrich.2.plan.ffff", _plan_value("aborted", [3], []))
        _put_setting(connection, 1, "linkedin.enrich.1.pins", "[4]")

    migrations.upgrade(migration_engine, "0013")

    with migration_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT user_id, linkedin_account_id, kind, status, trigger, plan_json,"
                " stop_reason, completed_at FROM sync_runs ORDER BY id"
            )
        ).all()
        assert [tuple(row[:5]) for row in rows] == [
            (1, 1, "enrich", "aborted", "manual"),
            (1, 1, "enrich", "aborted", "manual"),
        ]
        # SQLite hands JSON back as text; PostgreSQL's driver decodes it.
        plans = [row[5] if isinstance(row[5], dict) else json.loads(row[5]) for row in rows]
        assert {(tuple(p["contact_ids"]), tuple(p["completed"])) for p in plans} == {
            ((5, 6, 7), (5,)),
            ((8, 9), ()),
        }
        assert {row[6] for row in rows} == {"cancelled", "interrupted"}
        assert all(row[7] is not None for row in rows)
        assert _keys(connection, 1) == {
            "linkedin.enrich.1.plan.eeee",  # unreadable: left alone
            "linkedin.enrich.2.plan.ffff",  # account 2 is user 2's, not user 1's
            "linkedin.enrich.1.pins",
        }
        armed = connection.execute(
            text("SELECT scheduled_runs_armed_at FROM linkedin_accounts")
        ).scalars()
        assert list(armed) == [None, None]

    migrations.downgrade(migration_engine, "0012")

    names = set(inspect(migration_engine).get_table_names())
    assert "sync_runs" not in names
    columns = {c["name"] for c in inspect(migration_engine).get_columns("linkedin_accounts")}
    assert "scheduled_runs_armed_at" not in columns


def test_a_run_goes_with_its_user_and_its_resume_link_is_cleared(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine)
    insert = text(
        "INSERT INTO sync_runs (user_id, linkedin_account_id, kind, status, trigger, started_at,"
        " browser_mode, resume_of_id, created_at, updated_at)"
        " VALUES (:user_id, :account, 'enrich', 'aborted', 'manual', :t, 'attach', :resume, :t, :t)"
    )
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        connection.execute(
            text(
                "INSERT INTO linkedin_accounts (user_id, label, created_at, updated_at)"
                " VALUES (1, 'default', :t, :t), (2, 'default', :t, :t)"
            ),
            {"t": STAMP},
        )
        connection.execute(insert, {"user_id": 1, "account": 1, "t": STAMP, "resume": None})
        connection.execute(insert, {"user_id": 1, "account": 1, "t": STAMP, "resume": 1})
        connection.execute(insert, {"user_id": 2, "account": 2, "t": STAMP, "resume": None})
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sync_runs (user_id, linkedin_account_id, kind, status, trigger,"
                " started_at, browser_mode, created_at, updated_at)"
                " VALUES (1, 1, 'inbox_poll', 'running', 'manual', :t, 'attach', :t, :t)"
            ),
            {"t": STAMP},
        )
    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM sync_runs WHERE id = 1"))
        assert (
            connection.execute(text("SELECT resume_of_id FROM sync_runs WHERE id = 2")).scalar()
            is None
        )
        connection.execute(text("DELETE FROM users WHERE id = 2"))
        assert _count(connection, "sync_runs") == 1


# --- the needs-review mark (0014, #184) ------------------------------------------------


def test_existing_contacts_need_no_review_and_the_mark_downgrades_away(
    migration_engine: Engine,
) -> None:
    """Every contact before 0014 came from a source netkeeper trusts, so none is marked."""
    migrations.upgrade(migration_engine, "0013")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
    migrations.upgrade(migration_engine, "0014")
    with migration_engine.begin() as connection:
        mark = connection.execute(text("SELECT needs_review_at FROM contacts WHERE id = 1"))
        assert mark.scalar() is None
        connection.execute(
            text("UPDATE contacts SET needs_review_at = :t WHERE id = 1"), {"t": STAMP}
        )
    migrations.downgrade(migration_engine, "0013")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("contacts")}
    assert "needs_review_at" not in columns
    with migration_engine.begin() as connection:
        assert _count(connection, "contacts") == 1


# --- the builtin list mark (0016, #133) ------------------------------------------------

SEEDED_FILTER = '{"where": {"op": "eq", "field": "met", "value": "met"}, "include_archived": false}'


def test_the_seeded_validated_list_is_marked_builtin_and_nothing_else_is(
    migration_engine: Engine,
) -> None:
    """Only a list every sign says the app seeded is marked; doubt leaves a list unmarked."""
    migrations.upgrade(migration_engine, "0015")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2, 3, 4)
        for user_id in (1, 2, 3, 4):
            _put_setting(connection, user_id, "lists.validated_seeded", "true")
        # 1: as seeded, and a filter stored before include_archived existed also counts.
        _insert_list(
            connection, id=1, user_id=1, name="Validated", kind="smart", filter_json=SEEDED_FILTER
        )
        _insert_list(connection, id=2, user_id=1, name="First 100")
        _insert_list(
            connection,
            id=3,
            user_id=2,
            name="Validated",
            kind="smart",
            filter_json='{"where": {"op": "eq", "field": "met", "value": "met"}}',
        )
        # 3: renamed, and 4: filter edited. Past recognition, so left unmarked.
        _insert_list(
            connection, id=4, user_id=3, name="Met", kind="smart", filter_json=SEEDED_FILTER
        )
        _insert_list(
            connection,
            id=5,
            user_id=4,
            name="Validated",
            kind="smart",
            filter_json='{"where": {"op": "has_email"}, "include_archived": false}',
        )
        # 5: the seeded filter under a user never seeded, so the person made it.
        _seed_users(connection, 5)
        _insert_list(
            connection, id=6, user_id=5, name="Validated", kind="smart", filter_json=SEEDED_FILTER
        )

    migrations.upgrade(migration_engine, "0016")

    with migration_engine.begin() as connection:
        marked = connection.execute(text("SELECT id FROM lists WHERE builtin ORDER BY id"))
        assert marked.scalars().all() == [1, 3]
        _insert_list(connection, id=7, user_id=1, name="New")  # the default fills it
        new = connection.execute(text("SELECT builtin FROM lists WHERE id = 7"))
        assert not new.scalar_one()  # 0 on SQLite, false on PostgreSQL

    migrations.downgrade(migration_engine, "0015")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("lists")}
    assert "builtin" not in columns
    with migration_engine.begin() as connection:
        assert _count(connection, "lists") == 7


# --- templates (0017, P3-03) -----------------------------------------------------------


def _insert_template(
    connection: Connection,
    *,
    id: int,
    user_id: int = 1,
    channel: str = "email",
    version: int = 1,
    previous_id: int | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO templates (id, user_id, name, channel, subject, body, lint_json,"
            " version, previous_id, created_at, updated_at) VALUES (:id, :user_id, 'T',"
            " :channel, NULL, 'Hi', '[]', :version, :previous_id, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "channel": channel,
            "version": version,
            "previous_id": previous_id,
            "t": STAMP,
        },
    )


def test_templates_keep_a_version_chain_a_line(migration_engine: Engine) -> None:
    """A version is replaced at most once, per user; deleting one never takes a newer one."""
    migrations.upgrade(migration_engine, "0017")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_template(connection, id=1)
        _insert_template(connection, id=2)  # any number of first versions
        _insert_template(connection, id=3, version=2, previous_id=1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_template(connection, id=4, version=2, previous_id=1)  # a fork
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_template(connection, id=4, channel="fax")

    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM templates WHERE id = 1"))
        previous = connection.execute(text("SELECT previous_id FROM templates WHERE id = 3"))
        assert previous.scalar_one() is None  # SET NULL, the newer version kept

    migrations.downgrade(migration_engine, "0016")
    assert "templates" not in inspect(migration_engine).get_table_names()


# --- campaigns (0018, P3-04) -----------------------------------------------------------

CAMPAIGN_TABLES = ("campaigns", "campaign_steps", "enrollments", "messages")


def _insert_campaign(
    connection: Connection,
    *,
    id: int,
    user_id: int = 1,
    name: str | None = None,
    status: str = "draft",
    source_list_id: int | None = None,
    filter_json: str | None = None,
    guard_days: int = 30,
) -> None:
    connection.execute(
        text(
            "INSERT INTO campaigns (id, user_id, name, status, source_list_id, filter_json,"
            " contacted_within_days_guard, created_at, updated_at)"
            " VALUES (:id, :user_id, :name, :status, :source_list_id, :filter_json, :guard,"
            " :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "name": f"Campaign {id}" if name is None else name,
            "status": status,
            "source_list_id": source_list_id,
            "filter_json": filter_json,
            "guard": guard_days,
            "t": STAMP,
        },
    )


def _insert_step(
    connection: Connection,
    *,
    id: int,
    campaign_id: int,
    template_id: int,
    user_id: int = 1,
    position: int = 1,
    channel: str = "email",
    mode: str = "draft",
    same_thread: bool = False,
    delay_days: int = 0,
) -> None:
    connection.execute(
        text(
            "INSERT INTO campaign_steps (id, user_id, campaign_id, position, channel,"
            " template_id, delay_days, mode, condition, same_thread, created_at, updated_at)"
            " VALUES (:id, :user_id, :campaign_id, :position, :channel, :template_id,"
            " :delay_days, :mode, 'always', :same_thread, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "campaign_id": campaign_id,
            "position": position,
            "channel": channel,
            "template_id": template_id,
            "delay_days": delay_days,
            "mode": mode,
            "same_thread": same_thread,
            "t": STAMP,
        },
    )


def _insert_enrollment(
    connection: Connection, *, id: int, campaign_id: int, contact_id: int, user_id: int = 1
) -> None:
    connection.execute(
        text(
            "INSERT INTO enrollments (id, user_id, campaign_id, contact_id, status,"
            " channel_ids_json, created_at, updated_at)"
            " VALUES (:id, :user_id, :campaign_id, :contact_id, 'pending', '{}', :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "campaign_id": campaign_id,
            "contact_id": contact_id,
            "t": STAMP,
        },
    )


def _insert_message(
    connection: Connection,
    *,
    id: int,
    enrollment_id: int,
    contact_id: int,
    step_id: int | None = None,
    user_id: int = 1,
    direction: str = "out",
    status: str = "sent",
) -> None:
    connection.execute(
        text(
            "INSERT INTO messages (id, user_id, enrollment_id, step_id, contact_id, channel,"
            " direction, status, created_at, updated_at)"
            " VALUES (:id, :user_id, :enrollment_id, :step_id, :contact_id, 'email',"
            " :direction, :status, :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "enrollment_id": enrollment_id,
            "step_id": step_id,
            "contact_id": contact_id,
            "direction": direction,
            "status": status,
            "t": STAMP,
        },
    )


def _ids(connection: Connection, table: str, where: str) -> list[int]:
    return list(connection.execute(text(f"SELECT id FROM {table} WHERE {where}")).scalars())


def _seed_a_sent_campaign(connection: Connection) -> None:
    """User 1: contact 1 sent one message by campaign 1 (step 1, template 1)."""
    _seed_users(connection, 1)
    _insert_contact(connection, id=1, user_id=1)
    _insert_template(connection, id=1)
    _insert_campaign(connection, id=1, status="active")
    _insert_step(connection, id=1, campaign_id=1, template_id=1)
    _insert_enrollment(connection, id=1, campaign_id=1, contact_id=1)
    _insert_message(connection, id=1, enrollment_id=1, contact_id=1, step_id=1)


def test_migration_creates_the_campaign_tables(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0018")
    assert set(CAMPAIGN_TABLES) <= set(inspect(migration_engine).get_table_names())


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("campaigns", "status", "running"),
        ("campaign_steps", "mode", "carrier_pigeon"),
        ("campaign_steps", "condition", "sometimes"),
        ("enrollments", "status", "waiting"),
        ("messages", "status", "lost"),
        ("messages", "direction", "sideways"),
    ],
)
def test_campaign_enums_are_checked_by_the_database(
    migration_engine: Engine, table: str, column: str, value: str
) -> None:
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = 1"), {"v": value})


def test_a_campaign_has_one_audience_and_a_unique_name(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_list(connection, id=1, user_id=1, name="L")
        _insert_campaign(connection, id=1, name="Same")
        _insert_campaign(connection, id=2, user_id=2, name="Same")  # another user's
        _insert_campaign(connection, id=3, source_list_id=1)
        _insert_campaign(connection, id=4, filter_json='{"where": null}')
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_campaign(connection, id=5, name="Same")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_campaign(connection, id=5, source_list_id=1, filter_json='{"where": null}')
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_campaign(connection, id=5, guard_days=-1)

    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM lists WHERE id = 1"))
        kept = connection.execute(text("SELECT source_list_id FROM campaigns WHERE id = 3"))
        assert kept.scalar_one() is None  # SET NULL: the campaign outlives its list


@pytest.mark.parametrize(
    ("channel", "mode", "same_thread", "allowed"),
    [
        ("email", "draft", False, True),
        ("email", "send", True, True),
        ("linkedin", "prefill", False, True),
        ("linkedin", "auto_send", False, True),
        ("email", "prefill", False, False),
        ("linkedin", "send", False, False),
        ("linkedin", "prefill", True, False),  # threading is an email idea
    ],
)
def test_a_step_mode_belongs_to_its_channel(
    migration_engine: Engine, channel: str, mode: str, same_thread: bool, allowed: bool
) -> None:
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_template(connection, id=1)
        _insert_campaign(connection, id=1)

    def insert() -> None:
        with migration_engine.begin() as connection:
            _insert_step(
                connection,
                id=1,
                campaign_id=1,
                template_id=1,
                channel=channel,
                mode=mode,
                same_thread=same_thread,
            )

    if allowed:
        insert()
    else:
        with pytest.raises(IntegrityError):
            insert()


def test_steps_and_enrollments_are_unique_and_positions_start_at_one(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_template(connection, id=1)
        _insert_campaign(connection, id=1)
        _insert_step(connection, id=1, campaign_id=1, template_id=1)
        _insert_enrollment(connection, id=1, campaign_id=1, contact_id=1)
    refused: list[Callable[[Connection], None]] = [
        lambda c: _insert_step(c, id=2, campaign_id=1, template_id=1),  # position 1 again
        lambda c: _insert_step(c, id=2, campaign_id=1, template_id=1, position=0),
        lambda c: _insert_step(c, id=2, campaign_id=1, template_id=1, position=2, delay_days=-1),
        lambda c: _insert_enrollment(c, id=2, campaign_id=1, contact_id=1),
    ]
    for bad in refused:
        with pytest.raises(IntegrityError), migration_engine.begin() as connection:
            bad(connection)


def test_a_message_direction_and_status_agree(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        _insert_message(
            connection, id=2, enrollment_id=1, contact_id=1, direction="in", status="received"
        )
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_message(connection, id=3, enrollment_id=1, contact_id=1, status="received")
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_message(connection, id=3, enrollment_id=1, contact_id=1, direction="in")


@pytest.mark.parametrize(
    "delete",
    [
        "DELETE FROM contacts WHERE id = 1",
        "DELETE FROM enrollments WHERE id = 1",
        "DELETE FROM campaign_steps WHERE id = 1",
        "DELETE FROM campaigns WHERE id = 1",
        "DELETE FROM templates WHERE id = 1",
    ],
)
def test_nothing_a_message_names_can_be_deleted_out_from_under_it(
    migration_engine: Engine, delete: str
) -> None:
    """What was sent is the record of what was sent (spec 8): deleting what it names fails."""
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text(delete))
    with migration_engine.begin() as connection:
        assert _count(connection, "messages") == 1


def test_what_was_never_sent_goes_with_its_owner(migration_engine: Engine) -> None:
    """A contact takes its enrollments; a campaign its steps and enrollments; a user everything."""
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        _insert_contact(connection, id=2, user_id=1)
        _insert_contact(connection, id=3, user_id=1)
        _insert_campaign(connection, id=2)
        _insert_step(connection, id=2, campaign_id=2, template_id=1)
        _insert_enrollment(connection, id=2, campaign_id=2, contact_id=2)
        _insert_enrollment(connection, id=3, campaign_id=1, contact_id=3)

        connection.execute(text("DELETE FROM contacts WHERE id = 3"))
        assert _ids(connection, "enrollments", "contact_id = 3") == []
        connection.execute(text("DELETE FROM campaigns WHERE id = 2"))
        assert _ids(connection, "campaign_steps", "campaign_id = 2") == []
        assert _ids(connection, "enrollments", "campaign_id = 2") == []

        connection.execute(text("DELETE FROM users WHERE id = 1"))
        for table in (*CAMPAIGN_TABLES, "templates", "contacts"):
            assert _count(connection, table) == 0, table


def test_an_interaction_message_id_is_a_foreign_key_now(migration_engine: Engine) -> None:
    """0018 clears ids that pointed at nothing, then constrains the column (SET NULL)."""
    migrations.upgrade(migration_engine, "0017")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_interaction(connection, id=1, user_id=1, contact_id=1, summary="hi")
        connection.execute(text("UPDATE interactions SET message_id = 7 WHERE id = 1"))

    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        stale = connection.execute(text("SELECT message_id FROM interactions WHERE id = 1"))
        assert stale.scalar_one() is None
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text("UPDATE interactions SET message_id = 7 WHERE id = 1"))

    with migration_engine.begin() as connection:
        _insert_template(connection, id=1)
        _insert_campaign(connection, id=1)
        _insert_enrollment(connection, id=1, campaign_id=1, contact_id=1)
        _insert_message(connection, id=1, enrollment_id=1, contact_id=1)
        connection.execute(text("UPDATE interactions SET message_id = 1 WHERE id = 1"))
        connection.execute(text("DELETE FROM messages WHERE id = 1"))
        kept = connection.execute(text("SELECT message_id FROM interactions WHERE id = 1"))
        assert kept.scalar_one() is None  # the timeline entry outlives the message

    migrations.downgrade(migration_engine, "0017")
    assert not set(CAMPAIGN_TABLES) & set(inspect(migration_engine).get_table_names())
    with migration_engine.begin() as connection:
        assert _count(connection, "interactions") == 1
        connection.execute(text("UPDATE interactions SET message_id = 7 WHERE id = 1"))


# --- mailboxes (0019, P3-01) -------------------------------------------------------------


def _insert_mailbox(
    connection: Connection,
    *,
    id: int,
    user_id: int = 1,
    email: str = "me@example.com",
    status: str = "ok",
    daily_cap: int = 80,
) -> None:
    connection.execute(
        text(
            "INSERT INTO mailboxes (id, user_id, email, provider, keychain_ref, daily_cap,"
            " status, label_prefix, created_at, updated_at)"
            " VALUES (:id, :user_id, :email, 'gmail', :ref, :cap, :status, 'netkeeper', :t, :t)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "email": email,
            "ref": f"gmail/mailbox/{id}",
            "cap": daily_cap,
            "status": status,
            "t": STAMP,
        },
    )


def test_a_mailbox_address_is_unique_per_user(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0019")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1, 2)
        _insert_mailbox(connection, id=1, user_id=1)
        _insert_mailbox(connection, id=2, user_id=2)  # another user's: fine
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_mailbox(connection, id=3, user_id=1)


@pytest.mark.parametrize(
    "values", [{"status": "paused"}, {"daily_cap": -1}], ids=["status", "daily_cap"]
)
def test_a_mailbox_refuses_what_it_cannot_mean(
    migration_engine: Engine, values: dict[str, Any]
) -> None:
    migrations.upgrade(migration_engine, "0019")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        _insert_mailbox(connection, id=1, **values)


def test_a_campaign_mailbox_id_is_a_foreign_key_now(migration_engine: Engine) -> None:
    """0019 clears ids that pointed at nothing, then constrains the column (no ON DELETE)."""
    migrations.upgrade(migration_engine, "0018")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_campaign(connection, id=1)
        connection.execute(text("UPDATE campaigns SET mailbox_id = 7 WHERE id = 1"))

    migrations.upgrade(migration_engine, "0019")
    with migration_engine.begin() as connection:
        stale = connection.execute(text("SELECT mailbox_id FROM campaigns WHERE id = 1"))
        assert stale.scalar_one() is None
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text("UPDATE campaigns SET mailbox_id = 7 WHERE id = 1"))

    with migration_engine.begin() as connection:
        _insert_mailbox(connection, id=1)
        connection.execute(text("UPDATE campaigns SET mailbox_id = 1 WHERE id = 1"))
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM mailboxes WHERE id = 1"))  # a campaign names it

    with migration_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE id = 1"))
        assert _count(connection, "mailboxes") == 0
        assert _count(connection, "campaigns") == 0


def test_0019_downgrades_to_campaigns_without_the_key(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0019")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_mailbox(connection, id=1)
        _insert_campaign(connection, id=1)
        connection.execute(text("UPDATE campaigns SET mailbox_id = 1 WHERE id = 1"))

    migrations.downgrade(migration_engine, "0018")
    assert "mailboxes" not in inspect(migration_engine).get_table_names()
    with migration_engine.begin() as connection:
        assert _count(connection, "campaigns") == 1
        connection.execute(text("UPDATE campaigns SET mailbox_id = 7 WHERE id = 1"))


# --- mailbox generation (0020, #256) -----------------------------------------------------


def test_0020_starts_every_mailbox_at_generation_zero(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0019")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_mailbox(connection, id=1)
    migrations.upgrade(migration_engine, "0020")
    with migration_engine.begin() as connection:
        found = connection.execute(text("SELECT generation FROM mailboxes WHERE id = 1"))
        assert found.scalar_one() == 0
        _insert_mailbox(connection, id=2, email="other@example.com")  # the default fills it
        found = connection.execute(text("SELECT generation FROM mailboxes WHERE id = 2"))
        assert found.scalar_one() == 0


def test_0020_downgrades_to_mailboxes_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0020")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_mailbox(connection, id=1)
    migrations.downgrade(migration_engine, "0019")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("mailboxes")}
    assert "generation" not in columns
    with migration_engine.begin() as connection:
        assert _count(connection, "mailboxes") == 1


# --- message reconcile state (0021, P3-07 review) ----------------------------------------

_RECONCILE_COLUMNS = (
    "thread_known_json",
    "reconcile_misses",
    "reconcile_first_miss_at",
    "reconcile_last_miss_at",
)


def test_0021_starts_every_message_with_no_misses_and_nothing_known(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0020")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
    migrations.upgrade(migration_engine, "0021")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT {', '.join(_RECONCILE_COLUMNS)} FROM messages WHERE id = 1")
        ).one()
        assert tuple(row) == (None, 0, None, None)
        _insert_message(connection, id=2, enrollment_id=1, contact_id=1, step_id=1)
        found = connection.execute(text("SELECT reconcile_misses FROM messages WHERE id = 2"))
        assert found.scalar_one() == 0  # the default fills it


def test_0021_downgrades_to_messages_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0021")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        connection.execute(
            text("UPDATE messages SET reconcile_misses = 2, thread_known_json = '[\"a\"]'")
        )
    migrations.downgrade(migration_engine, "0020")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("messages")}
    assert columns.isdisjoint(_RECONCILE_COLUMNS)
    with migration_engine.begin() as connection:
        assert _count(connection, "messages") == 1


# --- enrollment not-sent retries (0022, #280) --------------------------------------------

_NOT_SENT_COLUMNS = ("not_sent_count", "not_sent_since", "not_sent_error")


def test_0022_starts_every_enrollment_with_no_tries_counted(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0021")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
    migrations.upgrade(migration_engine, "0022")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT {', '.join(_NOT_SENT_COLUMNS)} FROM enrollments WHERE id = 1")
        ).one()
        assert tuple(row) == (0, None, None)
        _insert_contact(connection, id=2, user_id=1)
        _insert_enrollment(connection, id=2, campaign_id=1, contact_id=2)
        found = connection.execute(text("SELECT not_sent_count FROM enrollments WHERE id = 2"))
        assert found.scalar_one() == 0  # the default fills it


def test_0022_downgrades_to_enrollments_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0022")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        connection.execute(
            text("UPDATE enrollments SET not_sent_count = 3, not_sent_error = 'rate limited'")
        )
    migrations.downgrade(migration_engine, "0021")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("enrollments")}
    assert columns.isdisjoint(_NOT_SENT_COLUMNS)
    with migration_engine.begin() as connection:
        assert _count(connection, "enrollments") == 1


# --- mailbox arming (0023, #277) ---------------------------------------------------------

_ARM_COLUMNS = ("armed_at", "send_armed_at", "armed_by", "message_id_verified_at")


def test_0023_starts_every_mailbox_disarmed_and_unverified(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0022")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_mailbox(connection, id=1)
    migrations.upgrade(migration_engine, "0023")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT {', '.join(_ARM_COLUMNS)} FROM mailboxes WHERE id = 1")
        ).one()
        assert tuple(row) == (None, None, None, None)


def test_0023_downgrades_to_mailboxes_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0023")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_mailbox(connection, id=1)
        connection.execute(
            text("UPDATE mailboxes SET armed_at = :t, armed_by = 'cli (me)'"), {"t": STAMP}
        )
    migrations.downgrade(migration_engine, "0022")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("mailboxes")}
    assert columns.isdisjoint(_ARM_COLUMNS)
    with migration_engine.begin() as connection:
        assert _count(connection, "mailboxes") == 1


# --- the review gate (0024, #288) --------------------------------------------------------

_REVIEW_COLUMNS = (
    "lint_checked_at",
    "lint_fingerprint",
    "guards_acknowledged_at",
    "guards_fingerprint",
    "guards_summary",
)
_REVIEW_TABLES = ("campaign_review_previews", "campaign_test_sends")


def test_0024_starts_every_campaign_unreviewed(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0023")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_campaign(connection, id=1, status="reviewing")
    migrations.upgrade(migration_engine, "0024")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT {', '.join(_REVIEW_COLUMNS)} FROM campaigns WHERE id = 1")
        ).one()
        assert tuple(row) == (None,) * len(_REVIEW_COLUMNS)
        for table in _REVIEW_TABLES:
            assert _count(connection, table) == 0


def test_0024_downgrades_to_campaigns_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0024")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_campaign(connection, id=1)
        connection.execute(text("UPDATE campaigns SET guards_summary = '1 in audience'"))
    migrations.downgrade(migration_engine, "0023")
    columns = {column["name"] for column in inspect(migration_engine).get_columns("campaigns")}
    assert columns.isdisjoint(_REVIEW_COLUMNS)
    assert not set(_REVIEW_TABLES) & set(inspect(migration_engine).get_table_names())
    with migration_engine.begin() as connection:
        assert _count(connection, "campaigns") == 1


# --- reply detection (0025, #295) --------------------------------------------------------


def test_0025_starts_every_mailbox_without_a_history_and_every_message_without_a_snippet(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0024")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        _insert_mailbox(connection, id=1)
    migrations.upgrade(migration_engine, "0025")
    with migration_engine.begin() as connection:
        assert connection.execute(
            text("SELECT history_id, replies_polled_at FROM mailboxes")
        ).one() == (None, None)
        assert connection.execute(text("SELECT snippet FROM messages")).scalars().all() == [None]
        # Gmail's historyId can outgrow a 32-bit integer.
        connection.execute(text("UPDATE mailboxes SET history_id = 9007199254740993"))
        found = connection.execute(text("SELECT history_id FROM mailboxes")).scalar_one()
        assert found == 9007199254740993


def test_0025_downgrades_to_mailboxes_and_messages_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0025")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        connection.execute(text("UPDATE messages SET snippet = 'Thanks'"))
    migrations.downgrade(migration_engine, "0024")
    inspector = inspect(migration_engine)
    mailbox_columns = {c["name"] for c in inspector.get_columns("mailboxes")}
    assert mailbox_columns.isdisjoint({"history_id", "replies_polled_at"})
    assert "snippet" not in {c["name"] for c in inspector.get_columns("messages")}
    with migration_engine.begin() as connection:
        assert _count(connection, "messages") == 1


# --- the inbox (0026, #300) ---------------------------------------------------------------


def test_0026_fills_in_the_inbox_columns_for_existing_messages(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0025")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)  # message 1: sent
        _insert_message(connection, id=2, enrollment_id=1, contact_id=1, status="bounced")
        for id, subject, snippet in (
            (3, "Re: Catching up", "Good to hear from you"),
            (4, "Re: Catching up", "Please REMOVE ME from this list"),
            (5, "Unsubscribe", None),
            (6, "Re: Catching up", "I unsubscribed from everything"),  # not a whole word
        ):
            _insert_message(
                connection, id=id, enrollment_id=1, contact_id=1, direction="in", status="received"
            )
            connection.execute(
                text("UPDATE messages SET subject = :s, snippet = :n WHERE id = :id"),
                {"s": subject, "n": snippet, "id": id},
            )
        # Outbound: only an inbound message can ask to unsubscribe, whatever it says (#299).
        _insert_message(connection, id=7, enrollment_id=1, contact_id=1)
        connection.execute(text("UPDATE messages SET subject = 'Unsubscribe' WHERE id = 7"))
        # The bounce's own updated_at, unlike every other row's, so bounced_at shows its source.
        connection.execute(
            text("UPDATE messages SET updated_at = :t WHERE id = 2"),
            {"t": "2026-03-04 05:06:07"},
        )
        before = connection.execute(
            text("SELECT id, status, updated_at FROM messages ORDER BY id")
        ).all()
    migrations.upgrade(migration_engine, "0026")
    with migration_engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT id, asks_unsubscribe, bounced_at IS NOT NULL, handled_at"
                " FROM messages ORDER BY id"
            )
        ).all()
        after = connection.execute(
            text("SELECT id, status, updated_at FROM messages ORDER BY id")
        ).all()
        bounced = connection.execute(
            text("SELECT bounced_at, updated_at FROM messages WHERE id = 2")
        ).one()
    assert [tuple(r) for r in rows] == [
        (1, False, False, None),
        (2, False, True, None),
        (3, False, False, None),
        (4, True, False, None),
        (5, True, False, None),
        (6, False, False, None),
        (7, False, False, None),
    ]
    assert bounced.bounced_at == bounced.updated_at
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_0026_downgrades_to_messages_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0026")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        connection.execute(text("UPDATE messages SET handled_at = :t"), {"t": STAMP})
    migrations.downgrade(migration_engine, "0025")
    columns = {c["name"] for c in inspect(migration_engine).get_columns("messages")}
    assert columns.isdisjoint({"handled_at", "bounced_at", "asks_unsubscribe"})
    with migration_engine.begin() as connection:
        assert _count(connection, "messages") == 1


# --- test drafts (0027, #304) -------------------------------------------------------------

_TEST_DRAFT_COLUMNS = ("gmail_draft_id", "rfc822_message_id", "not_found_at")


def _insert_test_send(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO campaign_test_sends (id, user_id, campaign_id, step_id, fingerprint,"
            " to_address, gmail_message_id, sent_at, created_at, updated_at)"
            " VALUES (1, 1, 1, 1, :f, 'me@example.test', 'm1', :t, :t, :t)"
        ),
        {"f": "0" * 64, "t": STAMP},
    )


def test_0027_keeps_every_existing_test_send_as_sent(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0026")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        _insert_test_send(connection)
    migrations.upgrade(migration_engine, "0027")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text(
                f"SELECT gmail_message_id, {', '.join(_TEST_DRAFT_COLUMNS)}"
                " FROM campaign_test_sends"
            )
        ).one()
    assert tuple(row) == ("m1", None, None, None)


def test_0027_downgrades_to_test_sends_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0027")
    with migration_engine.begin() as connection:
        _seed_a_sent_campaign(connection)
        _insert_test_send(connection)
        connection.execute(
            text("UPDATE campaign_test_sends SET gmail_draft_id = 'r1', rfc822_message_id = :m"),
            {"m": "<a@example.test>"},
        )
    migrations.downgrade(migration_engine, "0026")
    columns = {c["name"] for c in inspect(migration_engine).get_columns("campaign_test_sends")}
    assert columns.isdisjoint(_TEST_DRAFT_COLUMNS)
    with migration_engine.begin() as connection:
        assert _count(connection, "campaign_test_sends") == 1


# --- snapshot position change (0028, #286) -------------------------------------------------


def _seed_a_snapshot(connection: Connection) -> None:
    _seed_users(connection, 1)
    _insert_contact(connection, id=1, user_id=1)
    _insert_children(connection, user_id=1, contact_id=1)


def test_0028_marks_every_existing_snapshot_as_no_position_change(
    migration_engine: Engine,
) -> None:
    """What replaced an old snapshot's values was never kept, so it cannot claim a move."""
    migrations.upgrade(migration_engine, "0027")
    with migration_engine.begin() as connection:
        _seed_a_snapshot(connection)
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        row = connection.execute(
            text("SELECT headline, position_changed FROM contact_snapshots")
        ).one()
    assert (row.headline, bool(row.position_changed)) == ("then", False)


def test_0028_downgrades_to_snapshots_without_it(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        _seed_a_snapshot(connection)
        connection.execute(text("UPDATE contact_snapshots SET position_changed = true"))
    migrations.downgrade(migration_engine, "0027")
    columns = {c["name"] for c in inspect(migration_engine).get_columns("contact_snapshots")}
    assert "position_changed" not in columns
    with migration_engine.begin() as connection:
        assert _count(connection, "contact_snapshots") == 1


# --- the do-not-send list (0029, #238) ------------------------------------------------------


def _insert_email(
    connection: Connection, *, contact_id: int, email: str, status: str, user_id: int = 1
) -> None:
    connection.execute(
        text(
            "INSERT INTO contact_emails (user_id, contact_id, email, kind, is_primary, status,"
            " source, observed_at, created_at, updated_at)"
            " VALUES (:user_id, :contact_id, :email, 'other', false, :status, 'manual',"
            " :t, :t, :t)"
        ),
        {
            "user_id": user_id,
            "contact_id": contact_id,
            "email": email,
            "status": status,
            "t": STAMP,
        },
    )


def _seed_address_statuses(connection: Connection) -> None:
    """Contact 1 bounced at a@, contact 2 invalid at a@ and b@, contact 3 opted out at c@
    and holds a@ as ok; user 2's contact 4 bounced at d@. Contact 5 holds e+x@ ok."""
    _seed_users(connection, 1, 2)
    for contact_id, user_id in ((1, 1), (2, 1), (3, 1), (4, 2), (5, 1)):
        _insert_contact(connection, id=contact_id, user_id=user_id)
    _insert_email(connection, contact_id=1, email="a@example.test", status="bounced")
    _insert_email(connection, contact_id=2, email="a@example.test", status="invalid")
    _insert_email(connection, contact_id=2, email="b@example.test", status="invalid")
    _insert_email(connection, contact_id=3, email="c@example.test", status="ok")
    _insert_email(connection, contact_id=3, email="a+x@example.test", status="ok")
    _insert_email(connection, contact_id=4, email="d@example.test", status="bounced", user_id=2)
    _insert_email(connection, contact_id=5, email="e+x@example.test", status="ok")
    _insert_campaign(connection, id=1)
    _insert_enrollment(connection, id=1, campaign_id=1, contact_id=3)
    connection.execute(text("UPDATE enrollments SET status = 'opted_out' WHERE id = 1"))


def test_0029_lists_every_bounced_invalid_and_opted_out_address(
    migration_engine: Engine,
) -> None:
    """The strongest reason wins; a +tag is its own address; each user keeps their own."""
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        _seed_address_statuses(connection)
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        rows = connection.execute(
            text("SELECT user_id, email, reason, bounced, contact_id FROM do_not_send_addresses")
        ).all()
    # Sorted here, not by ORDER BY: PostgreSQL's locale collation puts "a@" before "a+x@".
    assert sorted((r.user_id, r.email, r.reason, bool(r.bounced), r.contact_id) for r in rows) == [
        (1, "a+x@example.test", "opted_out", False, 3),
        (1, "a@example.test", "bounced", True, 1),
        (1, "b@example.test", "invalid", False, 2),
        (1, "c@example.test", "opted_out", False, 3),
        (2, "d@example.test", "bounced", True, 4),
    ]


def test_0029_entries_outlive_their_contact_and_are_unique_per_user(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        _seed_address_statuses(connection)
        connection.execute(
            text(
                "INSERT INTO do_not_send_addresses (user_id, email, reason, bounced, contact_id,"
                " created_at, updated_at)"
                " VALUES (1, 'z@example.test', 'bounced', true, 1, :t, :t)"
            ),
            {"t": STAMP},
        )
        connection.execute(text("DELETE FROM contact_emails WHERE contact_id = 1"))
        connection.execute(text("DELETE FROM contacts WHERE id = 1"))
        row = connection.execute(
            text("SELECT contact_id FROM do_not_send_addresses WHERE email = 'z@example.test'")
        ).one()
    assert row.contact_id is None
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO do_not_send_addresses (user_id, email, reason, bounced, contact_id,"
                " created_at, updated_at)"
                " VALUES (1, 'z@example.test', 'manual', false, NULL, :t, :t)"
            ),
            {"t": STAMP},
        )
    with pytest.raises(IntegrityError), migration_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO do_not_send_addresses (user_id, email, reason, bounced, contact_id,"
                " created_at, updated_at)"
                " VALUES (1, 'y@example.test', 'bogus', false, NULL, :t, :t)"
            ),
            {"t": STAMP},
        )


def test_0029_downgrades_to_no_list(migration_engine: Engine) -> None:
    """The statuses on the contacts stay; only the list goes."""
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        _seed_address_statuses(connection)
    migrations.upgrade(migration_engine, "0029")
    migrations.downgrade(migration_engine, "0028")
    assert "do_not_send_addresses" not in inspect(migration_engine).get_table_names()
    with migration_engine.begin() as connection:
        assert _count(connection, "contact_emails") == 7
    migrations.upgrade(migration_engine, "0029")  # and back up again, filled the same way
    with migration_engine.begin() as connection:
        assert _count(connection, "do_not_send_addresses") == 5


def test_0029_lists_an_unsubscribe_on_a_completed_enrollment_with_its_bounce(
    migration_engine: Engine,
) -> None:
    """The enrollment had completed when the reply asked to unsubscribe, so it never became
    ``opted_out``: the message alone lists the contact's addresses. One of them had also
    bounced, and the entry keeps that."""
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        _insert_contact(connection, id=1, user_id=1)
        _insert_email(connection, contact_id=1, email="u@example.test", status="ok")
        _insert_email(connection, contact_id=1, email="old@example.test", status="bounced")
        _insert_contact(connection, id=2, user_id=1)  # a reply that did not ask: not listed
        _insert_email(connection, contact_id=2, email="r@example.test", status="ok")
        _insert_campaign(connection, id=1)
        for enrollment_id, contact_id in ((1, 1), (2, 2)):
            _insert_enrollment(connection, id=enrollment_id, campaign_id=1, contact_id=contact_id)
            _insert_message(
                connection,
                id=enrollment_id,
                enrollment_id=enrollment_id,
                contact_id=contact_id,
                direction="in",
                status="received",
            )
        connection.execute(text("UPDATE enrollments SET status = 'completed'"))
        connection.execute(text("UPDATE messages SET asks_unsubscribe = true WHERE id = 1"))
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        rows = connection.execute(
            text("SELECT email, reason, bounced, contact_id FROM do_not_send_addresses")
        ).all()
    assert sorted((r.email, r.reason, bool(r.bounced), r.contact_id) for r in rows) == [
        ("old@example.test", "opted_out", True, 1),
        ("u@example.test", "opted_out", False, 1),
    ]


def test_0029_breaks_a_tie_between_equal_reasons_by_the_lowest_contact_id(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0028")
    with migration_engine.begin() as connection:
        _seed_users(connection, 1)
        for contact_id in (9, 4, 7):
            _insert_contact(connection, id=contact_id, user_id=1)
            _insert_email(
                connection, contact_id=contact_id, email="Same@example.test", status="bounced"
            )
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        rows = connection.execute(
            text("SELECT email, reason, contact_id FROM do_not_send_addresses")
        ).all()
    assert [tuple(r) for r in rows] == [("same@example.test", "bounced", 4)]


# --- a job change needs an earlier enrichment (0030, #323) ----------------------------------

_EARLIER = "2026-09-01 08:00:00.000000"
_VISIT = "2026-10-01 09:00:00.000000"
_AFTER_VISIT = "2026-10-01 09:00:01.000000"


def _insert_position(
    connection: Connection,
    *,
    contact_id: int,
    created_at: str,
    title: str | None = "Engineer",
    company: str | None = "Old Co",
    source: str = "sync",
    ended_on: str | None = None,
    user_id: int = 1,
) -> None:
    connection.execute(
        text(
            "INSERT INTO contact_positions (user_id, contact_id, title, company, ended_on,"
            " is_current, source, observed_at, created_at, updated_at)"
            " VALUES (:user_id, :contact_id, :title, :company, :ended_on, true, :source, :t,"
            " :created, :t)"
        ),
        {
            "user_id": user_id,
            "ended_on": ended_on,
            "contact_id": contact_id,
            "title": title,
            "company": company,
            "source": source,
            "created": created_at,
            "t": _VISIT,
        },
    )


def _insert_flagged_snapshot(
    connection: Connection,
    *,
    id: int,
    contact_id: int,
    source: str = "sync",
    title: str | None = "Engineer",
    company: str | None = "Old Co",
    position_changed: bool = True,
    user_id: int = 1,
) -> None:
    connection.execute(
        text(
            "INSERT INTO contact_snapshots (id, user_id, contact_id, current_title,"
            " current_company, position_changed, source, observed_at, created_at, updated_at)"
            " VALUES (:id, :user_id, :contact_id, :title, :company, :changed, :source, :t, :c,"
            " :c)"
        ),
        {
            "id": id,
            "user_id": user_id,
            "contact_id": contact_id,
            "title": title,
            "company": company,
            "changed": position_changed,
            "source": source,
            "t": _VISIT,
            "c": _AFTER_VISIT,
        },
    )


def _seed_job_changes(connection: Connection) -> None:
    """One contact per case; the snapshot id says which one, and a comment why."""
    _seed_users(connection, 1, 2)
    for contact_id in range(1, 15):
        _insert_contact(connection, id=contact_id, user_id=1)
    for contact_id in (20, 21):
        _insert_contact(connection, id=contact_id, user_id=2)
    # 1: an earlier enrichment recorded the replaced position: a real job change.
    _insert_position(connection, contact_id=1, created_at=_EARLIER)
    _insert_flagged_snapshot(connection, id=1, contact_id=1)
    # 2: a first enrichment over an archive job: its positions are the visit's own.
    _insert_position(connection, contact_id=2, created_at=_AFTER_VISIT, title="Lead")
    _insert_position(connection, contact_id=2, created_at=_AFTER_VISIT)  # the old job, listed
    _insert_flagged_snapshot(connection, id=2, contact_id=2)
    # 3: a first enrichment with no position at all from before.
    _insert_flagged_snapshot(connection, id=3, contact_id=3)
    # 4: an earlier enrichment, but of another position than the one replaced.
    _insert_position(connection, contact_id=4, created_at=_EARLIER, title="X", company="Y")
    _insert_flagged_snapshot(connection, id=4, contact_id=4)
    # 5: an earlier enrichment recorded the company only; the title was imported.
    _insert_position(connection, contact_id=5, created_at=_EARLIER, title=None)
    _insert_flagged_snapshot(connection, id=5, contact_id=5)
    # 6: a person's edit or an import never notices a job change.
    _insert_position(connection, contact_id=6, created_at=_EARLIER)
    _insert_flagged_snapshot(connection, id=6, contact_id=6, source="manual")
    # 7: a headline-only change stays false.
    _insert_position(connection, contact_id=7, created_at=_EARLIER)
    _insert_flagged_snapshot(connection, id=7, contact_id=7, position_changed=False)
    # 8: another contact's earlier enrichment is no evidence for this one.
    _insert_flagged_snapshot(connection, id=8, contact_id=8)
    # 9: the review's repro. The archive wrote Engineer at Old Co; visit 1 found
    # that job only as an ended one (its current job had no title or company);
    # visit 2 replaced the archive's values. Visit 1 never recorded them as current.
    _insert_position(connection, contact_id=9, created_at=_EARLIER, ended_on="2023-03-01")
    _insert_flagged_snapshot(connection, id=9, contact_id=9)
    # 10: no title on either side is no match; the companies differ.
    _insert_position(connection, contact_id=10, created_at=_EARLIER, title=None, company="Else")
    _insert_flagged_snapshot(connection, id=10, contact_id=10, title=None)
    # 11: a position created at the very instant of the visit is the visit's own.
    _insert_position(connection, contact_id=11, created_at=_VISIT)
    _insert_flagged_snapshot(connection, id=11, contact_id=11)
    # 12: an earlier position an import wrote is no enrichment.
    _insert_position(connection, contact_id=12, created_at=_EARLIER, source="archive")
    _insert_flagged_snapshot(connection, id=12, contact_id=12)
    # 13: values match trimmed and case-folded, as positions match.
    _insert_position(connection, contact_id=13, created_at=_EARLIER, title=" engineer ")
    _insert_flagged_snapshot(connection, id=13, contact_id=13, company="OLD CO")
    # 14: a job ending in the month it was recorded was still current then.
    _insert_position(connection, contact_id=14, created_at=_EARLIER, ended_on="2026-09-01")
    _insert_flagged_snapshot(connection, id=14, contact_id=14)
    # A second user: one real change, one first enrichment.
    _insert_position(connection, contact_id=20, created_at=_EARLIER, user_id=2)
    _insert_flagged_snapshot(connection, id=20, contact_id=20, user_id=2)
    _insert_flagged_snapshot(connection, id=21, contact_id=21, user_id=2)


def _flags(connection: Connection) -> dict[int, bool]:
    rows = connection.execute(text("SELECT id, position_changed FROM contact_snapshots")).all()
    return {row.id: bool(row.position_changed) for row in rows}


_CORRECTED = {
    **{1: True, 2: False, 3: False, 4: False, 5: True, 6: False, 7: False, 8: False},
    **{9: False, 10: False, 11: False, 12: False, 13: True, 14: True},
    **{20: True, 21: False},
}


def test_0030_keeps_a_job_change_only_against_an_earlier_enrichment(
    migration_engine: Engine,
) -> None:
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        _seed_job_changes(connection)
    migrations.upgrade(migration_engine, "0030")
    with migration_engine.begin() as connection:
        assert _flags(connection) == _CORRECTED


def test_0030_is_idempotent(migration_engine: Engine) -> None:
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        _seed_job_changes(connection)
    migrations.upgrade(migration_engine, "0030")
    migrations.downgrade(migration_engine, "0029")
    migrations.upgrade(migration_engine, "0030")
    with migration_engine.begin() as connection:
        assert _flags(connection) == _CORRECTED


def test_0030_downgrades_without_restoring_the_cleared_flags(migration_engine: Engine) -> None:
    """Lossy on purpose: fewer job changes under older code is harmless (#323)."""
    migrations.upgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        _seed_job_changes(connection)
    migrations.upgrade(migration_engine, "0030")
    migrations.downgrade(migration_engine, "0029")
    with migration_engine.begin() as connection:
        assert _revision(connection) == "0029"
        assert _flags(connection) == _CORRECTED
    migrations.upgrade(migration_engine, "0030")
    with migration_engine.begin() as connection:
        assert _revision(connection) == "0030"


def _revision(connection: Connection) -> str:
    return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())
