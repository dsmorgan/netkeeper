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
from collections.abc import Iterator
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


def test_ci_runs_the_postgresql_params() -> None:
    """Without the URL the PostgreSQL params skip silently, so CI must always set it."""
    if not os.environ.get("GITHUB_ACTIONS"):
        pytest.skip("only meaningful on GitHub Actions")
    assert os.environ.get(PG_ENV), f"CI must set {PG_ENV}; see .github/workflows/ci.yml"
