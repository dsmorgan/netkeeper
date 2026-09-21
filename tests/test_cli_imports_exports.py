"""CLI counterparts of import, export, and contacts stats (P1-16, issue #25).

Each command drives the same service functions its API counterpart does, so
most tests assert on the exact fixture-derived counts the service layer's own
tests use (``tests/test_archive_import.py``) or compare the CLI's output
against a direct call to the service function on the same database.
``netkeeper contacts stats`` has no ``GET /contacts/stats`` route of its own,
but it is not exempt from this: it shares ``netkeeper.crm.triage.progress()``'s
live-rows baseline (``archived_at IS NULL AND merged_into_id IS NULL``) for its
four ``ContactMet`` counts and ``total``, the same numbers ``GET /triage/next``
et al. surface as ``TriageProgressOut``, and a dedicated test below pins the
two together so they cannot quietly drift apart.
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
from netkeeper.crm import import_runs, triage
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
    """``text`` with escape codes and Rich panel wrapping stripped.

    Rich forces color when it detects CI (stripped here too), and it word-wraps
    a usage-error panel to 80 columns, which can break a literal phrase across
    two lines depending on how long the path in it is (a real failure: on CI's
    ~67-column path "does not exist" broke between "does" and "not exist",
    passing locally only because tmp_path there is longer). Collapsing
    whitespace and dropping the panel's box-drawing border undoes the wrap
    without weakening what a test asserts.
    """
    return " ".join(_ANSI.sub("", text).replace("│", " ").split())


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
    assert "does not exist" in _plain(result.output)


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
    # The exact error line, not just "import run 1" anywhere in the output: the
    # service's own INFO log line ("import run 1 for user 1: ...") also contains
    # that phrase and would satisfy a looser assertion without proving the CLI's
    # own message threads draft.id through correctly.
    assert (
        "error: 1 row(s) match more than one contact and have no decision "
        "(rows 3); decide them in the app (import run 1), "
        "or re-run with --on-candidate new or --on-candidate skip"
    ) in result.output
    # _refuse_undecided runs before _commit_draft is ever called, so no commit
    # is attempted and nothing is rolled back; the draft it names comes from its
    # own, already-committed transaction (POST /imports vs. POST
    # /imports/{id}/commit), so it is really there to decide, in the app or with
    # a second CLI call.
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
    raw = out.read_bytes()
    # The real, untranslated bytes: csv.writer's line terminator is "\r\n", and
    # export_cmd's `newline=""` on the open() call is what keeps it that way
    # instead of collapsing to "\n". Path.read_text() would hide a regression
    # here (it applies universal-newline translation on read), which is exactly
    # why this asserts on read_bytes() instead.
    assert b"\r\n" in raw
    content = raw.decode("utf-8")
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
        # met=MET on purpose: archived and merged-away are excluded from the
        # live-rows baseline, so neither should add to `met` even though both
        # carry it. This is the regression the API review caught: a merge
        # copies `met` onto the survivor while the loser keeps its own, so
        # counting every row (not just live ones) double-counted one person.
        archived = factories.make_contact(session, user, met=ContactMet.MET)
        archived.archived_at = archived.created_at
        with_email = factories.make_contact(session, user, emails=["b@example.test"])
        with_phone = factories.make_contact(session, user, phones=["+15550100001"])
        tagged = factories.make_contact(session, user)
        tag = create_tag(session, user, "vp")
        tag_contact(session, user, tagged.id, tag.id)
        # Both MET, so identity.MET_RANK ties and the merge leaves survivor.met
        # as it already was: MET either way, so the loser's own MET is what
        # would have added a spurious second "met" for one person pre-fix.
        loser = factories.make_contact(session, user, met=ContactMet.MET)
        survivor = factories.make_contact(session, user, met=ContactMet.MET)
        merge_contacts(session, user, survivor.id, loser.id)
        _ = (untriaged, with_email, with_phone)

    with cli_db() as session:
        user = ensure_local_user(session)
        expected = contact_stats(session, user)

    result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["METRIC", "COUNT"]
    *data_lines, footer = lines[1:]
    rows = {" ".join(line.split()[:-1]): line.split()[-1] for line in data_lines}
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
    assert footer == (
        f"total ({expected.total}) counts live contacts only: not archived, not merged away. "
        "archived and merged away are separate counts of what total leaves out."
    )
    # And the numbers actually mean what they say: 10 contacts made, 2 outside
    # the live set (archived, merged away), so total is 8; met is the plain
    # MET contact plus the survivor (also MET) — the archived MET and the
    # loser's own MET, both outside the live set, add nothing.
    assert expected.total == 8
    assert expected.met == 2
    assert expected.not_met == 1
    assert expected.skipped == 1
    assert expected.untriaged == 4
    assert expected.met + expected.not_met + expected.skipped + expected.untriaged == expected.total
    assert expected.archived == 1
    assert expected.merged_away == 1
    assert expected.with_email == 1
    assert expected.with_phone == 1
    assert expected.tagged == 1


def test_contact_stats_agrees_with_triage_progress_on_the_four_states(
    cli_db: sessionmaker[Session],
) -> None:
    """The regression the API review asked for: these two can never quietly drift.

    ``netkeeper contacts stats`` has a real API counterpart after all —
    ``netkeeper.crm.triage.progress()``, surfaced through ``GET /triage/next``
    et al. — and it counts live contacts only. This pins ``contact_stats()`` to
    the same four counts on the same seed ``triage.progress()`` would see.
    """
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, met=ContactMet.MET)
        factories.make_contact(session, user, met=ContactMet.NOT_MET)
        factories.make_contact(session, user, met=ContactMet.SKIP)
        factories.make_contact(session, user, met=ContactMet.UNKNOWN)
        archived = factories.make_contact(session, user, met=ContactMet.MET)
        archived.archived_at = archived.created_at
        # Both survivor and loser are MET (identity.MET_RANK ties: the merge
        # leaves survivor.met as it already was, MET). The pre-fix bug counted
        # every row unconditionally, so the loser's own MET would have added a
        # third "met" here on top of the plain MET contact and the survivor;
        # met == 2, not 3, is what proves the loser is excluded.
        loser = factories.make_contact(session, user, met=ContactMet.MET)
        survivor = factories.make_contact(session, user, met=ContactMet.MET)
        merge_contacts(session, user, survivor.id, loser.id)

    with cli_db() as session:
        user = ensure_local_user(session)
        stats = contact_stats(session, user)
        progress = triage.progress(session, user)

    assert stats.total == progress.total
    assert stats.met == progress.by_state[ContactMet.MET]
    assert stats.not_met == progress.by_state[ContactMet.NOT_MET]
    assert stats.skipped == progress.by_state[ContactMet.SKIP]
    assert stats.untriaged == progress.by_state[ContactMet.UNKNOWN]
    # Concretely, not just "whatever progress() says": 7 contacts made, 2
    # outside the live set (the archived one and the merged-away loser), so 5
    # live; met is the plain MET contact plus the survivor, not the loser too.
    assert stats.total == 5
    assert stats.met == 2
    assert stats.not_met == 1


def test_cli_contacts_stats_does_not_leak_across_users(cli_db: sessionmaker[Session]) -> None:
    """The EXISTS subqueries in contact_stats() carry a user_id term of their own

    (defense in depth, per netkeeper.crm.filters._child_rows), because the scope
    guard only inspects the outer statement and cannot see into a correlated
    subquery. This is what proves that term does something: the same counts
    before and after a second user gets contacts, emails, phones, and a tag.
    """
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, met=ContactMet.MET, emails=["mine@example.test"])
        mine = factories.make_contact(session, user, phones=["+15550100004"])
        tag = create_tag(session, user, "vp")
        tag_contact(session, user, mine.id, tag.id)

    with cli_db() as session:
        user = ensure_local_user(session)
        before = contact_stats(session, user)

    with session_scope(cli_db, write=True) as session:
        other = factories.make_user(session)
        factories.make_contact(session, other, met=ContactMet.MET, emails=["theirs@example.test"])
        theirs = factories.make_contact(session, other, phones=["+15550100005"])
        other_tag = create_tag(session, other, "director")
        tag_contact(session, other, theirs.id, other_tag.id)

    with cli_db() as session:
        user = ensure_local_user(session)
        after = contact_stats(session, user)

    assert after == before


def test_cli_contacts_stats_with_no_contacts_is_all_zero(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    *data_lines, footer = lines[1:]
    assert all(line.split()[-1] == "0" for line in data_lines)
    assert "total (0) counts live contacts only" in footer


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
