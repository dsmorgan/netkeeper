"""CLI counterparts of import, export, and contacts stats (P1-16, issue #25).

Each command drives the same service functions the API (or, for ``contacts
stats``, which has no API counterpart yet, the ORM) would use, so most tests
assert on the exact fixture-derived counts the service layer's own tests use
(``tests/test_archive_import.py``) or compare the CLI's output against a
direct call to the service function on the same database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import factories
import pytest
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.crm import import_runs
from netkeeper.crm.contacts import contact_stats, merge_contacts
from netkeeper.crm.exports import export_stream
from netkeeper.crm.filters import FilterTree
from netkeeper.crm.tags import create_tag, tag_contact
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import Contact, ContactMet, Interaction
from netkeeper.scoping import install_scope_guard, scoped, scoped_count
from netkeeper.services.users import ensure_local_user

FIXTURES_ARCHIVE = Path(__file__).parent / "fixtures" / "archive"
FIXTURES_CSV = Path(__file__).parent / "fixtures" / "csv"
NINE_COLUMN_SAMPLE = FIXTURES_CSV / "nine-column-sample.csv"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _plain(text: str) -> str:
    """``text`` with escape codes stripped: Rich forces color when it detects CI."""
    return _ANSI.sub("", text)


def _run_id(output: str, label: str) -> int:
    match = re.search(rf"{label} run (\d+)", output)
    assert match is not None, output
    return int(match.group(1))


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, pointed to by ``NETKEEPER_DATABASE_URL``."""
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session)
    yield factory
    engine.dispose()


# --- import archive -----------------------------------------------------


def test_cli_import_archive_reports_the_known_counts(cli_db: sessionmaker[Session]) -> None:
    """The counts printed are the sample's, the same ones test_archive_import.py asserts."""
    result = CliRunner().invoke(cli, ["import", "archive", str(FIXTURES_ARCHIVE)])
    assert result.exit_code == 0, result.output
    assert "connections: 9 rows, 7 created, 1 updated, 1 skipped, 0 needs review" in result.output
    assert (
        "messages: 13 rows in 7 conversations (4 attributed, 1 no counterparty, "
        "1 group, 1 not a contact); 8 interactions added"
    ) in result.output
    assert "invitations: 6 rows; 2 interactions added" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 7
        assert session.scalar(scoped_count(user, Interaction)) == 10


def test_cli_import_archive_of_a_bad_file_reports_a_clean_error(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    bogus = tmp_path / "not-an-archive.csv"
    bogus.write_text("just,some,random,columns\n1,2,3,4\n")
    result = CliRunner().invoke(cli, ["import", "archive", str(bogus)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "not a LinkedIn archive" in result.output


def test_cli_import_archive_of_a_missing_path_is_a_usage_error(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    result = CliRunner().invoke(cli, ["import", "archive", str(tmp_path / "nope")])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "does not exist" in result.output


# --- import csv -----------------------------------------------------------


def test_cli_import_csv_dry_run_resolves_the_file_and_writes_nothing(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert (
        "draft run 1: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    assert "committed" not in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 0


def test_cli_import_csv_commits_and_matches_what_the_service_would_do(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert result.exit_code == 0, result.output
    assert (
        "committed run 1: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 3


def _seed_candidate(factory: sessionmaker[Session]) -> None:
    """A contact that collides, by name and company only, with the sample's third row."""
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(
            session,
            user,
            first_name="Thaddeus",
            last_name="Ravensworth",
            current_company="Wobblegong Analytics",
            li_urn=None,
            li_public_id=None,
        )


def test_cli_import_csv_refuses_undecided_candidates_and_still_leaves_a_draft(
    cli_db: sessionmaker[Session],
) -> None:
    _seed_candidate(cli_db)
    result = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "1 row(s) match more than one contact" in result.output
    assert "rows 3" in result.output
    assert "import run 1" in result.output
    assert "--on-candidate" in result.output
    # The refused commit rolled back, but the draft it names is a separate,
    # already-committed transaction (POST /imports vs. POST /imports/{id}/commit):
    # it is really there to decide, in the app or with a second CLI call.
    with cli_db() as session:
        user = ensure_local_user(session)
        run = import_runs.get_run(session, user, 1)
        assert run.status.value == "draft"
        assert run.candidate_count == 1


def test_cli_import_csv_on_candidate_skip_leaves_the_ambiguous_row_out(
    cli_db: sessionmaker[Session],
) -> None:
    _seed_candidate(cli_db)
    result = CliRunner().invoke(
        cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--on-candidate", "skip"]
    )
    assert result.exit_code == 0, result.output
    assert (
        "committed run 1: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 2 created, 0 candidate(s), 1 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # The one seeded contact, plus the two unambiguous rows; Thaddeus was
        # left out, not merged into the seeded contact.
        assert session.scalar(scoped_count(user, Contact)) == 3


def test_cli_import_csv_on_candidate_new_creates_a_separate_contact(
    cli_db: sessionmaker[Session],
) -> None:
    _seed_candidate(cli_db)
    result = CliRunner().invoke(
        cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--on-candidate", "new"]
    )
    assert result.exit_code == 0, result.output
    assert (
        "committed run 1: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # The seeded contact, plus a *new* Thaddeus rather than a merge: 4 total.
        assert session.scalar(scoped_count(user, Contact)) == 4


def test_cli_import_csv_of_an_unusable_file_reports_a_clean_error(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    result = CliRunner().invoke(cli, ["import", "csv", str(empty)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "error:" in result.output


def test_cli_import_csv_rejects_invalid_mapping_json(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(
        cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--mapping", "{not json"]
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "--mapping" in result.output


# --- import rollback --------------------------------------------------------


def test_cli_import_rollback_undoes_a_committed_run_and_reports_counts(
    cli_db: sessionmaker[Session],
) -> None:
    commit = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert commit.exit_code == 0, commit.output
    run_id = _run_id(commit.output, "committed")

    result = CliRunner().invoke(cli, ["import", "rollback", str(run_id)])
    assert result.exit_code == 0, result.output
    assert f"run {run_id}: 3 contact(s) deleted, 0 restored, 0 field(s)" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 0


def test_cli_import_rollback_of_an_unknown_run_reports_a_clean_error(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["import", "rollback", "999"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "no import run 999" in result.output


def test_cli_import_rollback_of_a_merged_run_names_the_contact_and_says_what_to_do(
    cli_db: sessionmaker[Session],
) -> None:
    commit = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert commit.exit_code == 0, commit.output
    run_id = _run_id(commit.output, "committed")

    # Merge some other contact into one the run created.
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        created = session.scalars(scoped(user, Contact)).first()
        assert created is not None
        loser = factories.make_contact(session, user)
        merge_contacts(session, user, created.id, loser.id)
        survivor_id = created.id

    result = CliRunner().invoke(cli, ["import", "rollback", str(run_id)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert f"contact(s) {survivor_id}" in result.output
    assert "Undo the merge first" in result.output


# --- export -----------------------------------------------------------------


def test_cli_export_matches_a_direct_call_to_export_stream(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, emails=["a@example.test"], phones=["+15550100000"])
        factories.make_contact(session, user)

    with cli_db() as session:
        user = ensure_local_user(session)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        expected = "".join(
            export_stream(
                session,
                user,
                preset="nine-column",
                output_format="csv",
                headerless=False,
                tree=FilterTree(),
                sort=[],
                now=now,
            )
        )

    result = CliRunner().invoke(cli, ["export", "--preset", "nine-column", "--format", "csv"])
    assert result.exit_code == 0, result.output
    # CliRunner's captured stdout is read back through universal-newlines
    # translation, collapsing the CSV writer's "\r\n" to "\n"; the exact bytes
    # are covered separately by test_cli_export_writes_to_a_file_and_reports_it,
    # which reads the file back untranslated.
    assert result.stdout.replace("\r\n", "\n") == expected.replace("\r\n", "\n")


def test_cli_export_writes_to_a_file_and_reports_it(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, emails=["a@example.test"])

    out = tmp_path / "out.csv"
    result = CliRunner().invoke(
        cli,
        ["export", "--preset", "nine-column", "--format", "csv", "--headerless", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert f"wrote {out}" in result.stdout
    content = out.read_text()
    assert content.splitlines()[0].startswith("https://www.linkedin.com/in/first1-last1/")
    assert "LinkedIn Profile URL" not in content  # --headerless


def test_cli_export_full_json_round_trips_through_json_loads(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, emails=["a@example.test"])
        factories.make_contact(session, user)

    result = CliRunner().invoke(cli, ["export", "--preset", "full", "--format", "json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert len(data) == 2
    assert data[0]["emails"][0]["email"] == "a@example.test"


def test_cli_export_rejects_invalid_filter_json(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["export", "--filter", "{not json"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "--filter" in result.output


def test_cli_export_rejects_a_filter_that_does_not_compile(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["export", "--filter", '{"where": {"op": "bogus"}}'])
    assert result.exit_code == 1
    assert "Traceback" not in result.output


# --- contacts stats ----------------------------------------------------------


def test_cli_contacts_stats_matches_a_direct_call_to_the_service(
    cli_db: sessionmaker[Session],
) -> None:
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, met=ContactMet.MET)
        factories.make_contact(session, user, met=ContactMet.NOT_MET)
        factories.make_contact(session, user, met=ContactMet.SKIP)
        untriaged = factories.make_contact(session, user, met=ContactMet.UNKNOWN)
        archived = factories.make_contact(session, user, archived_at=None)
        archived.archived_at = archived.created_at
        with_email = factories.make_contact(session, user, emails=["b@example.test"])
        with_phone = factories.make_contact(session, user, phones=["+15550100001"])
        tagged = factories.make_contact(session, user)
        tag = create_tag(session, user, "vp")
        tag_contact(session, user, tagged.id, tag.id)
        loser = factories.make_contact(session, user)
        survivor = factories.make_contact(session, user)
        merge_contacts(session, user, survivor.id, loser.id)
        _ = (untriaged, with_email, with_phone)

    with cli_db() as session:
        user = ensure_local_user(session)
        expected = contact_stats(session, user)

    result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["METRIC", "COUNT"]
    rows = {" ".join(line.split()[:-1]): line.split()[-1] for line in lines[1:]}
    assert rows["total"] == str(expected.total)
    assert rows["met"] == str(expected.met)
    assert rows["not met"] == str(expected.not_met)
    assert rows["skipped"] == str(expected.skipped)
    assert rows["untriaged"] == str(expected.untriaged)
    assert rows["archived"] == str(expected.archived)
    assert rows["merged away"] == str(expected.merged_away)
    assert rows["with email"] == str(expected.with_email)
    assert rows["with phone"] == str(expected.with_phone)
    assert rows["tagged"] == str(expected.tagged)
    # And the numbers actually mean what they say (spec: the four ContactMet
    # states, plus survivor + loser + merged contacts among the totals).
    assert expected.total == 10
    assert expected.met == 1
    assert expected.not_met == 1
    assert expected.skipped == 1
    assert expected.untriaged == expected.total - 3
    assert expected.archived == 1
    assert expected.merged_away == 1
    assert expected.with_email == 1
    assert expected.with_phone == 1
    assert expected.tagged == 1


def test_cli_contacts_stats_with_no_contacts_is_all_zero(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert all(line.split()[-1] == "0" for line in lines[1:])


# --- help --------------------------------------------------------------------


def test_help_lists_the_new_command_groups() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    plain = _plain(result.stdout)
    assert "import" in plain
    assert "export" in plain
    assert "contacts" in plain


def test_import_help_lists_archive_csv_and_rollback() -> None:
    result = CliRunner().invoke(cli, ["import", "--help"])
    assert result.exit_code == 0, result.output
    plain = _plain(result.stdout)
    for name in ("archive", "csv", "rollback"):
        assert name in plain
