"""CLI counterparts of import, export, and contacts stats (P1-16, issue #25, #90).

Each command drives the same service functions its API counterpart does, so
most tests assert on the exact fixture-derived counts the service layer's own
tests use (``tests/test_archive_import.py``) or compare the CLI's output
against a direct call to the service function on the same database.
``netkeeper contacts stats`` shares ``netkeeper.crm.triage.progress()``'s
live-rows baseline (``archived_at IS NULL AND merged_into_id IS NULL``) for its
four ``ContactMet`` counts and ``total``, the same numbers ``GET /triage/next``
et al. surface as ``TriageProgressOut`` and ``GET /contacts/stats`` now surfaces
too (P1-25); a dedicated test below pins all three together so they cannot
quietly drift apart.
"""

from __future__ import annotations

import json
import re
import threading
import zipfile
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import factories
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.crm import import_runs, triage
from netkeeper.crm.contacts import _has_child, _has_tag, add_email, contact_stats, merge_contacts
from netkeeper.crm.exports import export_stream
from netkeeper.crm.filters import FilterTree
from netkeeper.crm.tags import create_tag, tag_contact
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Contact,
    ContactEmail,
    ContactMet,
    ContactPhone,
    Interaction,
    TagSource,
    User,
)
from netkeeper.scoping import install_scope_guard, scoped, scoped_count
from netkeeper.services.users import ensure_local_user
from netkeeper.web.app import create_app

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
    # The rules run with the import, and the import seeds the default set first
    # (#64), so this database tags without the server ever having started: the
    # sample's one engineer, its designer, and its advisor.
    assert "auto-tag rules: 7 contacts examined, 3 tags added, 0 removed" in result.output
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


def _fresh_cli_database(tmp_path: Path, dirname: str) -> str:
    """A migrated database with the local user, at its own path, and its URL.

    Not the ``cli_db`` fixture: that one monkeypatches ``$NETKEEPER_DATABASE_URL``
    for the whole test, which ``database_url()`` prefers over any path given to
    it (CLAUDE.md) — and the ``client``/``running_app`` fixtures' own database
    is built from a path the same way (``bare_engine``), so the two would
    collide onto one database instead of being the independent ones a
    CLI-versus-API comparison needs. Pass the returned URL as ``env=`` on just
    the ``CliRunner().invoke`` call instead, which scopes the override to the
    CLI's own process-wide state for exactly as long as that call runs.
    """
    cli_dir = tmp_path / dirname
    cli_dir.mkdir()
    cli_url = database_url(cli_dir)
    cli_engine = make_engine(cli_url)
    migrations.upgrade(cli_engine)
    cli_factory = make_session_factory(cli_engine)
    install_scope_guard(cli_factory)
    with session_scope(cli_factory, write=True) as session:
        ensure_local_user(session)
    cli_engine.dispose()
    return cli_url


async def test_the_cli_and_the_api_report_the_same_counts_for_the_same_archive(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """P1-20's own "done when": the same zip, imported independently by each,
    into two separate databases, agrees on every number the CLI prints.
    """
    cli_url = _fresh_cli_database(tmp_path, "cli-db")

    zipped = tmp_path / "export.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        for source in sorted(FIXTURES_ARCHIVE.iterdir()):
            zf.write(source, arcname=source.name)

    cli_result = CliRunner().invoke(
        cli, ["import", "archive", str(zipped)], env={"NETKEEPER_DATABASE_URL": cli_url}
    )
    assert cli_result.exit_code == 0, cli_result.output

    response = await client.post(
        "/api/v1/imports/archive",
        headers={"X-Netkeeper-Client": "1"},
        files={"file": ("export.zip", zipped.read_bytes(), "application/zip")},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    connections = re.search(
        r"connections: (\d+) rows, (\d+) created, (\d+) updated, (\d+) skipped, "
        r"(\d+) needs review",
        cli_result.output,
    )
    assert connections is not None, cli_result.output
    assert [int(value) for value in connections.groups()] == [
        body["connections"]["rows"],
        body["connections"]["created"],
        body["connections"]["updated"],
        body["connections"]["skipped"],
        body["connections"]["needs_review"],
    ]

    messages = re.search(
        r"messages: (\d+) rows in (\d+) conversations \((\d+) attributed, "
        r"(\d+) no counterparty, (\d+) group, (\d+) not a contact\); (\d+) interactions added",
        cli_result.output,
    )
    assert messages is not None, cli_result.output
    assert [int(value) for value in messages.groups()] == [
        body["messages"]["rows"],
        body["messages"]["conversations"],
        body["messages"]["attributed"],
        body["messages"]["no_counterpart"],
        body["messages"]["group_threads"],
        body["messages"]["unknown_contact"],
        body["messages"]["added"],
    ]

    invitations = re.search(r"invitations: (\d+) rows; (\d+) interactions added", cli_result.output)
    assert invitations is not None, cli_result.output
    assert [int(value) for value in invitations.groups()] == [
        body["invitations"]["rows"],
        body["invitations"]["added"],
    ]


def _key(contact: Contact) -> str:
    """A contact's profile slug, or its name for the one fixture row with none."""
    return contact.li_public_id or contact.first_name


def _contact_snapshot(session: Session, user: User) -> dict[str, tuple[object, ...]]:
    return {
        _key(contact): (
            contact.first_name,
            contact.last_name,
            contact.current_company,
            contact.current_title,
            contact.connected_on,
        )
        for contact in session.scalars(scoped(user, Contact))
    }


def _email_snapshot(session: Session, user: User) -> set[tuple[str, str]]:
    names = {contact.id: _key(contact) for contact in session.scalars(scoped(user, Contact))}
    return {
        (names[email.contact_id], email.email)
        for email in session.scalars(scoped(user, ContactEmail))
    }


def _interaction_snapshot(session: Session, user: User) -> Counter[tuple[str, str, datetime]]:
    """A multiset, not a set: the fixture has two rows the archive genuinely
    writes as two separate interactions (same contact, kind, and instant —
    see ``crm/archive.py``'s "a file that says it twice writes it twice"), so
    collapsing duplicates here would hide exactly the case that matters most.
    """
    names = {contact.id: _key(contact) for contact in session.scalars(scoped(user, Contact))}
    return Counter(
        (names[row.contact_id], row.kind.value, row.at)
        for row in session.scalars(scoped(user, Interaction))
    )


async def test_the_cli_and_the_api_write_the_same_rows_for_the_same_archive(
    client: httpx.AsyncClient, running_app: FastAPI, tmp_path: Path
) -> None:
    """Counts agreeing is not the same as the two writing the same rows: every
    contact, email, and interaction the CLI wrote for the same archive exists
    in the API's database too, and nothing else does. Keyed on a profile slug
    (or a name, for the one fixture row with none) — what an archive import
    itself determines identity from — rather than either database's own
    surrogate ids, which have no reason to agree across two separate runs, and
    excluding anything wall-clock (``created_at``/``updated_at``): everything
    compared here comes from the archive's own rows, so it is deterministic.
    """
    cli_url = _fresh_cli_database(tmp_path, "cli-db-rows")

    zipped = tmp_path / "export-rows.zip"
    with zipfile.ZipFile(zipped, "w") as zf:
        for source in sorted(FIXTURES_ARCHIVE.iterdir()):
            zf.write(source, arcname=source.name)

    cli_result = CliRunner().invoke(
        cli, ["import", "archive", str(zipped)], env={"NETKEEPER_DATABASE_URL": cli_url}
    )
    assert cli_result.exit_code == 0, cli_result.output

    response = await client.post(
        "/api/v1/imports/archive",
        headers={"X-Netkeeper-Client": "1"},
        files={"file": ("export-rows.zip", zipped.read_bytes(), "application/zip")},
    )
    assert response.status_code == 201, response.text

    cli_engine = make_engine(cli_url)
    cli_factory = make_session_factory(cli_engine)
    install_scope_guard(cli_factory)
    with session_scope(cli_factory) as session:
        cli_user = ensure_local_user(session)
        cli_contacts = _contact_snapshot(session, cli_user)
        cli_emails = _email_snapshot(session, cli_user)
        cli_interactions = _interaction_snapshot(session, cli_user)
    cli_engine.dispose()

    api_factory: sessionmaker[Session] = running_app.state.session_factory
    with session_scope(api_factory) as session:
        api_user = session.scalars(select(User)).one()
        api_contacts = _contact_snapshot(session, api_user)
        api_emails = _email_snapshot(session, api_user)
        api_interactions = _interaction_snapshot(session, api_user)

    assert api_contacts == cli_contacts
    assert len(cli_contacts) == 7
    assert api_emails == cli_emails
    assert api_interactions == cli_interactions
    assert sum(cli_interactions.values()) == 10


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
        "(rows 3); decide them in the app (import run 1), or finish this "
        "run with `netkeeper import resume 1 --on-candidate new` (or `--on-candidate "
        "skip`) -- re-running `import csv` would read the file into a second draft and "
        "leave run 1 behind as an orphan"
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


# --- import resume, runs, rm (#90) -------------------------------------------


def test_cli_import_resume_finishes_the_named_run_instead_of_a_second_one(
    cli_db: sessionmaker[Session],
) -> None:
    """The regression the refusal message names: re-running `import csv` would have read
    the file into a second draft next to the one the refusal was about. `import resume`
    finishes that exact run instead, so there is only ever the one draft.
    """
    _seed_candidate(cli_db)
    refused = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert refused.exit_code == 1
    run_id = _run_id(refused.output, "import")

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id), "--on-candidate", "new"])
    assert result.exit_code == 0, result.output
    assert (
        f"committed run {run_id}: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # Only run 1 exists: resume did not create a second draft.
        assert import_runs.list_runs(session, user)[1] == 1
        assert import_runs.get_run(session, user, run_id).status.value == "committed"
        # The seeded contact, plus a *new* Thaddeus rather than a merge: 4 total.
        assert session.scalar(scoped_count(user, Contact)) == 4


def test_cli_import_resume_without_on_candidate_refuses_again_naming_the_same_run(
    cli_db: sessionmaker[Session],
) -> None:
    """The refusal path runs a full `commit()` and relies on its rollback to leave nothing
    behind: `commit()` applies the two unambiguous rows before it finds row 3 still
    undecided and raises, so this snapshots contacts and rows before and after to pin that
    `session_scope`'s rollback really undoes that -- not only that the draft is still
    counted as one draft.
    """
    _seed_candidate(cli_db)
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    with cli_db() as session:
        user = ensure_local_user(session)
        before_contacts = sorted(
            (c.id, c.first_name, c.last_name, c.current_company)
            for c in session.scalars(scoped(user, Contact))
        )
        before_run = import_runs.get_run(session, user, run_id)
        assert before_run.status.value == "draft"
        before_rows = [
            (row.row_number, row.resolution.value, row.contact_id) for row in before_run.rows
        ]

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id)])
    assert result.exit_code == 1
    assert f"import run {run_id}" in result.output
    assert f"netkeeper import resume {run_id} --on-candidate new" in result.output

    with cli_db() as session:
        user = ensure_local_user(session)
        assert import_runs.list_runs(session, user)[1] == 1  # still just the one draft
        after_contacts = sorted(
            (c.id, c.first_name, c.last_name, c.current_company)
            for c in session.scalars(scoped(user, Contact))
        )
        after_run = import_runs.get_run(session, user, run_id)
        assert after_run.status.value == "draft"
        after_rows = [
            (row.row_number, row.resolution.value, row.contact_id) for row in after_run.rows
        ]
        assert after_contacts == before_contacts
        assert after_rows == before_rows


def test_cli_import_resume_on_candidate_skip_matches_what_commit_would_do(
    cli_db: sessionmaker[Session],
) -> None:
    """The one of the three `--on-candidate` paths that was already correct before this fix:
    `skip_undecided=True` passes no decisions and lets `commit()` re-resolve on its own.
    """
    _seed_candidate(cli_db)
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id), "--on-candidate", "skip"])
    assert result.exit_code == 0, result.output
    assert (
        f"committed run {run_id}: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 2 created, 0 candidate(s), 1 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # The seeded Thaddeus, plus the two unambiguous rows; the CSV's Thaddeus
        # was left out, not merged into the seeded contact.
        assert session.scalar(scoped_count(user, Contact)) == 3


# --- resume against a database that moved since the draft (#90 review finding 1) --------


def _delete_seeded_candidate(factory: sessionmaker[Session]) -> None:
    """Remove the contact `_seed_candidate` made, as if the ambiguity resolved itself
    between the draft and the resume -- a merge, an edit, or a manual delete.
    """
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session)
        thaddeus = session.scalars(
            scoped(user, Contact).where(
                Contact.first_name == "Thaddeus", Contact.last_name == "Ravensworth"
            )
        ).one()
        session.delete(thaddeus)


def test_cli_import_resume_reresolves_against_the_database_as_it_is_now(
    cli_db: sessionmaker[Session],
) -> None:
    """`import resume` exists because time passes between the draft and the finish --
    unlike `import csv`, where the read and the commit are the same instant. Here the
    colliding contact is deleted after the draft is read, so the row that was a
    candidate at draft time is a plain new contact by the time resume runs; resume must
    ask `commit()` what is true now, not replay what the draft recorded.
    """
    _seed_candidate(cli_db)
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    _delete_seeded_candidate(cli_db)

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id)])
    assert result.exit_code == 0, result.output
    assert (
        f"committed run {run_id}: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # The seeded contact was deleted; all 3 rows land fresh.
        assert session.scalar(scoped_count(user, Contact)) == 3


def test_cli_import_resume_on_candidate_new_does_not_touch_a_row_that_now_matches(
    cli_db: sessionmaker[Session],
) -> None:
    """The bug this pins: the old code decided `--on-candidate new` from the draft's
    stale, stored resolution, so it handed `identity.apply()` a `CreateNew()` decision for
    a row that, by resume time, had already stopped being a candidate. `apply()` refuses
    a decision on anything but a `Candidate` resolution, so the row was silently skipped
    ("a decision applies to a Candidate resolution only") instead of imported. Deriving
    the decisions from what `commit()` itself reports as still undecided closes that.
    """
    _seed_candidate(cli_db)
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    _delete_seeded_candidate(cli_db)

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id), "--on-candidate", "new"])
    assert result.exit_code == 0, result.output
    assert (
        f"committed run {run_id}: 3 rows from 'nine-column-sample.csv' (nine-column); "
        "0 matched, 3 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 3


def test_cli_import_resume_reresolves_a_row_into_an_outright_match(
    cli_db: sessionmaker[Session], tmp_path: Path
) -> None:
    """The other way the database can move between the draft and the resume: not the
    collision disappearing (the tests above), but the row's own identity showing up on the
    contact it was ambiguous with -- an email added to it in the app. A silent duplicate
    here would be the failure that actually matters: a real person's record failing to
    link, with no error at all to notice. `identity.resolve()` checks email before falling
    back to the name+company candidate rule, so once the address is there the row is a
    plain match, not a candidate and not a new contact.
    """
    with session_scope(cli_db, write=True) as session:
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

    content = (
        "LinkedIn Profile URL,Email Address,First Name,Last Name,CityState,"
        "Current Company,Current Job Title,Phone Number\n"
        ',thaddeus@wobblegong.example,Thaddeus,Ravensworth,"Cinder Flats, Farland",'
        "Wobblegong Analytics,Junior Wobbler,\n"
    )
    path = tmp_path / "thaddeus.csv"
    path.write_text(content)
    draft = CliRunner().invoke(cli, ["import", "csv", str(path), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")
    with cli_db() as session:
        user = ensure_local_user(session)
        # Ambiguous by name and company alone; the seeded contact has no email yet.
        assert import_runs.get_run(session, user, run_id).candidate_count == 1

    # Someone adds the missing address to the existing contact in the app.
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        thaddeus = session.scalars(
            scoped(user, Contact).where(
                Contact.first_name == "Thaddeus", Contact.last_name == "Ravensworth"
            )
        ).one()
        add_email(session, user, thaddeus.id, "thaddeus@wobblegong.example")

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id)])
    assert result.exit_code == 0, result.output
    assert (
        f"committed run {run_id}: 1 rows from 'thaddeus.csv' (nine-column); "
        "1 matched, 0 created, 0 candidate(s), 0 skipped"
    ) in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # Still one contact: the row matched the existing one, it did not duplicate it.
        assert session.scalar(scoped_count(user, Contact)) == 1


def test_cli_import_resume_of_a_committed_run_is_a_clean_error(
    cli_db: sessionmaker[Session],
) -> None:
    committed = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert committed.exit_code == 0, committed.output
    run_id = _run_id(committed.output, "committed")

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "only a draft run can be committed" in result.output


def test_cli_import_resume_of_an_unknown_run_reports_a_clean_error(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["import", "resume", "999"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "no import run 999" in result.output


def test_cli_import_runs_lists_newest_first_and_narrows_by_status(
    cli_db: sessionmaker[Session],
) -> None:
    committed = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert committed.exit_code == 0, committed.output
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    draft_id = _run_id(draft.output, "draft")

    result = CliRunner().invoke(cli, ["import", "runs"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["ID", "FILENAME", "STATUS", "ROWS"]
    ids = [line.split()[0] for line in lines[1:]]
    assert ids == [str(draft_id), str(draft_id - 1)]  # newest first

    drafts_only = CliRunner().invoke(cli, ["import", "runs", "--status", "draft"])
    assert drafts_only.exit_code == 0, drafts_only.output
    data_lines = drafts_only.stdout.splitlines()[1:]
    assert [line.split()[0] for line in data_lines] == [str(draft_id)]


def test_cli_import_runs_with_none_reports_that_clearly(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["import", "runs"])
    assert result.exit_code == 0, result.output
    assert "no import runs" in result.output


def test_cli_import_rm_deletes_a_draft(cli_db: sessionmaker[Session]) -> None:
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    run_id = _run_id(draft.output, "draft")

    result = CliRunner().invoke(cli, ["import", "rm", str(run_id)])
    assert result.exit_code == 0, result.output
    assert f"deleted import run {run_id}" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert import_runs.list_runs(session, user)[1] == 0


def test_cli_import_rm_of_a_committed_run_is_refused(cli_db: sessionmaker[Session]) -> None:
    committed = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    run_id = _run_id(committed.output, "committed")

    result = CliRunner().invoke(cli, ["import", "rm", str(run_id)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "only a draft run can be deleted" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert import_runs.get_run(session, user, run_id).status.value == "committed"


def test_cli_import_rm_of_an_unknown_run_reports_a_clean_error(
    cli_db: sessionmaker[Session],
) -> None:
    result = CliRunner().invoke(cli, ["import", "rm", "999"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "no import run 999" in result.output


def _write_lock_race(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> Any:
    """Invoke ``argv`` while another writer holds the SQLite write lock; the CliRunner
    result comes back once the lock is released.

    A short busy_timeout on the *contending* connection, so this proves the failure is
    reported cleanly without waiting out the real 5-second default: every import command
    opens a brand new engine at call time, after this patch takes effect, so it is the one
    that picks up the shorter wait; the lock-holder thread below does not need to.
    """
    monkeypatch.setattr("netkeeper.db.SQLITE_BUSY_TIMEOUT_MS", 50)
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def hold_the_write_lock() -> None:
        with session_scope(cli_db, write=True) as session:
            ensure_local_user(session)  # the write lock is taken at BEGIN IMMEDIATE, here
            holder_ready.set()
            release_holder.wait(timeout=5)

    holder = threading.Thread(target=hold_the_write_lock)
    holder.start()
    try:
        assert holder_ready.wait(timeout=5), "the lock-holding thread never started"
        return CliRunner().invoke(cli, argv)
    finally:
        release_holder.set()
        holder.join(timeout=5)


def test_cli_import_rm_reports_a_clean_error_on_a_write_lock_race(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rm` is destructive; losing a concurrent write-lock race must not surface as a raw
    traceback, the same guarantee `csv` and `resume` get below (`_reporting_lock_races`,
    netkeeper/cli.py) -- all three are writers, not read-mostly.
    """
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    result = _write_lock_race(cli_db, monkeypatch, ["import", "rm", str(run_id)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "locked" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        # Refused, not half-deleted: the draft is still there to try again.
        assert import_runs.get_run(session, user, run_id).status.value == "draft"


def test_cli_import_csv_reports_a_clean_error_on_a_write_lock_race(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _write_lock_race(cli_db, monkeypatch, ["import", "csv", str(NINE_COLUMN_SAMPLE)])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "locked" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert session.scalar(scoped_count(user, Contact)) == 0


def test_cli_import_resume_reports_a_clean_error_on_a_write_lock_race(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resume` opens a writer session at least once and, on a retried
    ``--on-candidate new``, twice -- the most exposed of the three to this race.
    """
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    result = _write_lock_race(cli_db, monkeypatch, ["import", "resume", str(run_id)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "locked" in result.output
    with cli_db() as session:
        user = ensure_local_user(session)
        assert import_runs.get_run(session, user, run_id).status.value == "draft"


def test_cli_import_resume_on_candidate_new_refuses_cleanly_when_the_retry_is_still_undecided(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this pins: the retry commit() -- deciding exactly the rows the first
    attempt found undecided -- sat inside the `except UndecidedCandidates` block handling
    that first attempt. If the database moved again in between and the retry itself found a
    row still undecided (a narrow race: its own issue, since the real fix is giving
    commit() the bulk policy so one transaction does both), the old code let that second
    UndecidedCandidates escape uncaught -- a raw traceback. Mocking commit() to keep
    finding a row undecided on the second call reproduces that without a genuine
    two-process race.
    """
    _seed_candidate(cli_db)
    draft = CliRunner().invoke(cli, ["import", "csv", str(NINE_COLUMN_SAMPLE), "--dry-run"])
    assert draft.exit_code == 0, draft.output
    run_id = _run_id(draft.output, "draft")

    real_commit = import_runs.commit
    calls = 0

    def flaky_commit(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise import_runs.UndecidedCandidates([3])
        return real_commit(*args, **kwargs)

    monkeypatch.setattr(import_runs, "commit", flaky_commit)

    result = CliRunner().invoke(cli, ["import", "resume", str(run_id), "--on-candidate", "new"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert f"import run {run_id}" in result.output
    assert calls == 2  # the first attempt, then the retry that is still undecided


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


def test_cli_export_says_why_a_predicate_is_unavailable(cli_db: sessionmaker[Session]) -> None:
    """The CLI half of #95: it parses, the compiler refuses it, and nobody sees a traceback."""
    with session_scope(cli_db, write=True) as session:
        factories.make_contact(session, ensure_local_user(session))
    tree = json.dumps({"where": {"op": "enrolled_in", "campaign_id": 1}})
    result = CliRunner().invoke(cli, ["export", "--filter", tree])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "P3-04" in result.output


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
        tagged_by_rule = factories.make_contact(session, user)
        rule_tag = create_tag(session, user, "director")
        tag_contact(session, user, tagged_by_rule.id, rule_tag.id, source=TagSource.RULE)
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
    assert rows["tagged by rule"] == str(expected.tagged_by_rule)
    assert footer == (
        f"total ({expected.total}) counts live contacts only: not archived, not merged away. "
        "archived and merged away are separate counts of what total leaves out."
    )
    # And the numbers actually mean what they say: 11 contacts made, 2 outside
    # the live set (archived, merged away), so total is 9; met is the plain
    # MET contact plus the survivor (also MET) — the archived MET and the
    # loser's own MET, both outside the live set, add nothing.
    assert expected.total == 9
    assert expected.met == 2
    assert expected.not_met == 1
    assert expected.skipped == 1
    assert expected.untriaged == 5
    assert expected.met + expected.not_met + expected.skipped + expected.untriaged == expected.total
    assert expected.archived == 1
    assert expected.merged_away == 1
    assert expected.with_email == 1
    assert expected.with_phone == 1
    # Both the manually tagged and the rule-tagged contact carry some tag;
    # only the second is rule-sourced.
    assert expected.tagged == 2
    assert expected.tagged_by_rule == 1


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


def test_contact_stats_child_subqueries_name_the_user_in_their_own_where(
    cli_db: sessionmaker[Session],
) -> None:
    """The runtime scope guard only inspects the outer statement (netkeeper.scoping), so
    the ``user_id`` term inside each correlated ``EXISTS`` in ``_has_child``/``_has_tag`` is
    the only thing keeping ``with_email``, ``with_phone``, ``tagged``, and ``tagged_by_rule``
    to one user -- defense in depth, the same convention ``tests/test_filters.py``'s
    ``test_compiled_statements_are_scoped_and_every_subquery_names_the_user`` pins for the
    filter language.

    Reading the compiled SQL, not counting rows, is what catches that term being dropped: a
    correlated ``EXISTS`` against a table already narrowed to one contact by ``contact_id``
    returns the same *result* with or without its own ``user_id`` clause, because a child
    row's ``contact_id`` already determines which user it belongs to -- proven by hand:
    giving a second user their own contacts, emails, phones, and tags of both sources (as
    the test above does) still passes with that clause deleted. Only the query's shape can
    catch it. ``tagged_by_rule`` gets the same check on its extra ``source`` clause, since
    without it the query is just ``tagged`` again under another name.
    """
    with cli_db() as session:
        user = ensure_local_user(session)
        for clause, table in (
            (_has_child(user, ContactEmail), "contact_emails"),
            (_has_child(user, ContactPhone), "contact_phones"),
            (_has_tag(user), "contact_tags"),
            (_has_tag(user, source=TagSource.RULE), "contact_tags"),
        ):
            statement = scoped_count(user, Contact).where(clause)
            sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
            assert f"{table}.user_id = {user.id}" in sql, table
        rule_sql = str(
            scoped_count(user, Contact)
            .where(_has_tag(user, source=TagSource.RULE))
            .compile(compile_kwargs={"literal_binds": True})
        )
        assert "contact_tags.source = 'rule'" in rule_sql


def test_cli_contacts_stats_with_no_contacts_is_all_zero(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    *data_lines, footer = lines[1:]
    assert all(line.split()[-1] == "0" for line in data_lines)
    assert "total (0) counts live contacts only" in footer


async def test_get_contact_stats_agrees_with_the_cli_and_triage_progress(
    cli_db: sessionmaker[Session],
) -> None:
    """The parity item P1-25 exists for: ``GET /contacts/stats``, ``netkeeper contacts
    stats``, and ``netkeeper.crm.triage.progress()`` must report the same numbers,
    because they once did not (#85 finding 2, #90). Each reaches the same underlying
    counts through its own path -- the router, the CLI's own engine, and a direct
    call -- so a drift in any one of the three fails this test.
    """
    with session_scope(cli_db, write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(session, user, met=ContactMet.MET)
        factories.make_contact(session, user, met=ContactMet.NOT_MET)
        factories.make_contact(session, user, met=ContactMet.SKIP)
        factories.make_contact(session, user, met=ContactMet.UNKNOWN)
        archived = factories.make_contact(session, user, met=ContactMet.MET)
        archived.archived_at = archived.created_at
        # Two tagged contacts, one by hand and one by rule, so `tagged` (any
        # source) and `tagged_by_rule` (rule only) are genuinely different
        # numbers -- a fixture where every tag happened to be rule-sourced
        # would pass even if the endpoint served `tagged`'s value under
        # `tagged_by_rule`'s name.
        by_hand = factories.make_contact(session, user)
        by_rule = factories.make_contact(session, user)
        manual_tag = create_tag(session, user, "seeded-manual")
        rule_tag = create_tag(session, user, "seeded-rule")
        tag_contact(session, user, by_hand.id, manual_tag.id, source=TagSource.MANUAL)
        tag_contact(session, user, by_rule.id, rule_tag.id, source=TagSource.RULE)

    cli_result = CliRunner().invoke(cli, ["contacts", "stats"])
    assert cli_result.exit_code == 0, cli_result.output
    lines = cli_result.stdout.splitlines()
    *data_lines, _footer = lines[1:]
    cli_rows = {" ".join(line.split()[:-1]): int(line.split()[-1]) for line in data_lines}

    with cli_db() as session:
        user = ensure_local_user(session)
        progress = triage.progress(session, user)

    # A second engine on the same file NETKEEPER_DATABASE_URL names, exactly as
    # `netkeeper serve` would open one: the app's own database, not a copy of it.
    engine = make_engine(database_url())
    try:
        app = create_app(Settings(), engine=engine)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1"
            ) as client:
                response = await client.get("/api/v1/contacts/stats")
    finally:
        engine.dispose()
    assert response.status_code == 200, response.text
    api = response.json()

    assert api["total"] == cli_rows["total"] == progress.total
    assert api["met"] == cli_rows["met"] == progress.by_state[ContactMet.MET]
    assert api["not_met"] == cli_rows["not met"] == progress.by_state[ContactMet.NOT_MET]
    assert api["skipped"] == cli_rows["skipped"] == progress.by_state[ContactMet.SKIP]
    assert api["untriaged"] == cli_rows["untriaged"] == progress.by_state[ContactMet.UNKNOWN]
    # Concretely, not just "whatever the other two say": 7 contacts made, 1
    # outside the live set (archived), so total is 6 and met is 1.
    assert api["total"] == 6
    assert api["met"] == 1
    assert api["archived"] == cli_rows["archived"] == 1
    # tagged_by_rule is the field a dashboard renders (#90 follow-up review): a
    # drift here must fail this test, not only the cross-user one. Both tagged
    # contacts count in `tagged`; only the rule-sourced one counts here too.
    assert api["tagged"] == cli_rows["tagged"] == 2
    assert api["tagged_by_rule"] == cli_rows["tagged by rule"] == 1


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
    for name in ("archive", "csv", "rollback", "resume", "runs", "rm"):
        assert name in plain
