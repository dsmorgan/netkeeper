"""``netkeeper.crm.exports`` (spec 10.6): CSV, JSON, and vCard over a filtered stream.

The nine-column round trip (P1-11's "done when") is the load-bearing test in
here: :func:`test_nine_column_round_trips_through_its_own_reader`. P1-04's real
nine-column importer had not merged when this was written, so that test reads
its own export back with a small stand-in reader rather than the real importer;
see the TODO on ``_read_nine_column_csv`` for issue #13, which should replace it.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import factories
import pytest
from sqlalchemy.orm import Session

from netkeeper.crm.exports import (
    FULL_FIELDS,
    LINKEDIN_ARCHIVE,
    NINE_COLUMN,
    _fold,
    _vcard_escape,
    export_stream,
    filename_for,
)
from netkeeper.crm.filters import FilterTree, SortKey, parse_filter
from netkeeper.db import database_url, make_engine, make_session_factory
from netkeeper.models import (
    Base,
    Contact,
    ContactEmail,
    ContactPhone,
    ContactSnapshot,
    ContactSource,
    User,
)
from netkeeper.scoping import install_scope_guard

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _run(session: Session, user: User, **kwargs: object) -> str:
    """``export_stream`` collected into one string; convenient for small fixtures."""
    kwargs.setdefault("headerless", False)
    kwargs.setdefault("tree", FilterTree())
    kwargs.setdefault("sort", [])
    kwargs.setdefault("now", NOW)
    stream: Iterator[str] = export_stream(session, user, **kwargs)  # type: ignore[arg-type]
    return "".join(stream)


# --- nine-column: shape, headerless, and the round trip ------------------------


def test_nine_column_header_matches_appendix_a_in_order() -> None:
    assert [column.header for column in NINE_COLUMN] == [
        "LinkedIn Profile URL",
        "Email Address",
        "First Name",
        "Last Name",
        "CityState",
        "Current Company",
        "Current Job Title",
        "Phone Number",
    ]


def test_nine_column_csv_has_the_header_row_by_default(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, emails=["one@example.test"])
    text = _run(session, user, preset="nine-column", output_format="csv", headerless=False)
    first_line = text.splitlines()[0]
    assert (
        first_line == "LinkedIn Profile URL,Email Address,First Name,Last Name,CityState,"
        "Current Company,Current Job Title,Phone Number"
    )


def test_nine_column_headerless_drops_the_header_row(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company="Acme Corp")
    with_header = _run(session, user, preset="nine-column", output_format="csv", headerless=False)
    headerless = _run(session, user, preset="nine-column", output_format="csv", headerless=True)
    assert headerless == "\n".join(with_header.splitlines()[1:]) + "\r\n"


def test_nine_column_json_uses_snake_case_keys(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        emails=["person@example.test"],
        location="Austin, TX",
        current_company="Acme Corp",
        current_title="Staff Engineer",
    )
    text = _run(session, user, preset="nine-column", output_format="json")
    (row,) = json.loads(text)
    assert set(row) == {
        "linkedin_profile_url",
        "email_address",
        "first_name",
        "last_name",
        "city_state",
        "current_company",
        "current_job_title",
        "phone_number",
    }
    assert row["email_address"] == "person@example.test"
    assert row["city_state"] == "Austin, TX"


def _read_nine_column_csv(text: str) -> list[dict[str, str]]:
    """A minimal reader for the nine-column CSV shape, for the round-trip test below.

    TODO(#13): P1-04's real nine-column importer was not merged when this was
    written. Once it lands, wire the round-trip test to it directly (import the
    export through the real importer into a clean database) and delete this
    stand-in, which only reads back the same eight columns a second time.
    """
    return list(csv.DictReader(io.StringIO(text)))


def _contact_from_nine_column_row(row: dict[str, str], user: User) -> Contact:
    """The reader's half of the round trip: rebuild a contact from one CSV row.

    Mirrors the column mapping in Appendix A: "First Name" is ``preferred_name``,
    not ``first_name`` (nine-column's own convention; contrast ``linkedin-archive``,
    which uses the LinkedIn-sourced ``first_name``).
    """
    contact = Contact(
        user_id=user.id,
        li_url=row["LinkedIn Profile URL"] or None,
        first_name="",
        last_name=row["Last Name"],
        preferred_name=row["First Name"],
        location=row["CityState"] or None,
        current_company=row["Current Company"] or None,
        current_title=row["Current Job Title"] or None,
        source=ContactSource.CSV,
    )
    if row["Email Address"]:
        contact.emails.append(
            ContactEmail(user_id=user.id, email=row["Email Address"], is_primary=True)
        )
    if row["Phone Number"]:
        contact.phones.append(
            ContactPhone(user_id=user.id, raw=row["Phone Number"], is_primary=True)
        )
    return contact


def test_nine_column_round_trips_through_its_own_reader(session: Session, tmp_path: Path) -> None:
    """Export, read back into a clean database, export again: the two exports match exactly."""
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        li_public_id="pat-morgan",
        preferred_name="Pat",
        last_name="Morgan",
        emails=["pat.morgan@example.test"],
        phones=["+15550001111"],
        location="Denver, CO",
        current_company="Northwind Traders",
        current_title="Staff Engineer",
    )
    factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id=None,
        preferred_name="Robin",
        last_name="Chen",
        location=None,
        current_company=None,
        current_title=None,
    )
    session.commit()

    first_export = _run(session, user, preset="nine-column", output_format="csv")

    # A second, independent database: the "clean database" the round trip needs.
    engine2 = make_engine(database_url(tmp_path / "reimport"))
    Base.metadata.create_all(engine2)
    factory2 = make_session_factory(engine2)
    install_scope_guard(factory2)
    try:
        with factory2() as session2:
            user2 = factories.make_user(session2)
            for row in _read_nine_column_csv(first_export):
                session2.add(_contact_from_nine_column_row(row, user2))
            session2.commit()
            second_export = _run(session2, user2, preset="nine-column", output_format="csv")
    finally:
        engine2.dispose()

    assert second_export == first_export


# --- linkedin-archive -----------------------------------------------------------


def test_linkedin_archive_uses_linkedins_own_columns() -> None:
    assert [column.header for column in LINKEDIN_ARCHIVE] == [
        "First Name",
        "Last Name",
        "URL",
        "Email Address",
        "Company",
        "Position",
        "Connected On",
    ]


def test_linkedin_archive_uses_the_raw_first_name_not_preferred_name(session: Session) -> None:
    """Unlike nine-column, this preset mirrors LinkedIn's own file: raw first_name."""
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        first_name="Robin",
        preferred_name="Rob",
        connected_on=date(2023, 1, 5),
    )
    text = _run(session, user, preset="linkedin-archive", output_format="csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert rows[0]["First Name"] == "Robin"
    assert rows[0]["Connected On"] == "05 Jan 2023"


# --- full: everything, including children, minus internal bookkeeping ---------


def test_full_json_includes_children_and_excludes_internal_fields(session: Session) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        emails=["a@example.test", "b@example.test"],
        phones=["+15551234567"],
        positions=[{"title": "Engineer", "company": "Acme"}],
        notes="Met at a conference",
    )
    session.add(ContactSnapshot(user_id=user.id, contact_id=contact.id, headline="Before"))
    session.commit()

    text = _run(session, user, preset="full", output_format="json")
    (row,) = json.loads(text)

    assert set(row) == set(FULL_FIELDS)
    assert len(row["emails"]) == 2
    assert row["emails"][0]["is_primary"] is True
    assert len(row["positions"]) == 1
    assert row["notes"] == "Met at a conference"

    # No database ids, foreign keys, or sync-internal bookkeeping anywhere in the row.
    dumped = json.dumps(row)
    for leaked in ('"id"', "user_id", "contact_id", "field_sources", "synced_values"):
        assert leaked not in dumped, f"{leaked!r} leaked into the full export"


def test_full_csv_flattens_children(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, emails=["one@example.test", "two@example.test"], phones=[]
    )
    text = _run(session, user, preset="full", output_format="csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert "one@example.test" in rows[0]["emails"]
    assert "two@example.test" in rows[0]["emails"]


# --- campaign-audience: the merge fields plus the recipient email --------------


def test_campaign_audience_computes_connection_age_and_position_change(
    session: Session,
) -> None:
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        emails=["reach@example.test"],
        connected_on=date(2020, 6, 1),
    )
    session.add(
        ContactSnapshot(
            user_id=user.id,
            contact_id=contact.id,
            headline="New role",
            observed_at=datetime(2025, 3, 1, tzinfo=UTC),
        )
    )
    session.commit()

    text = _run(session, user, preset="campaign-audience", output_format="json", now=NOW)
    (row,) = json.loads(text)
    assert row["email"] == "reach@example.test"
    assert row["connected_year"] == "2020"
    assert row["years_since_connected"] == "6"  # connected 2020-06-01, "now" is 2026-09-21
    assert row["last_position_change"] == "2025-03-01"


# --- vCard 4.0: escaping and 75-octet line folding -----------------------------


def test_vcard_escapes_comma_semicolon_and_newline_together() -> None:
    value = "Sales, Marketing; notes:\nfollow up in Q2"
    escaped = _vcard_escape(value)
    assert escaped == "Sales\\, Marketing\\; notes:\\nfollow up in Q2"
    # And backslashes introduced by escaping are not themselves re-escaped oddly.
    assert "\\\\," not in escaped


def test_vcard_fold_wraps_at_75_octets_with_a_leading_space_continuation() -> None:
    line = "NOTE:" + ("x" * 200)
    folded = _fold(line)
    physical_lines = folded.split("\r\n")
    assert len(physical_lines) > 1
    assert all(len(part.encode("utf-8")) <= 75 for part in physical_lines)
    assert all(part.startswith(" ") for part in physical_lines[1:])
    assert "".join(p[1:] if i else p for i, p in enumerate(physical_lines)) == line


def test_vcard_fold_does_not_split_a_multibyte_character() -> None:
    line = "NOTE:" + ("é" * 100)  # 2 octets each in UTF-8
    folded = _fold(line)
    for part in folded.split("\r\n"):
        text = part[1:] if part.startswith(" ") else part
        text.encode("utf-8").decode("utf-8")  # raises if a codepoint was split


def test_full_vcard_contains_begin_end_and_crlf_line_endings(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, emails=["a@example.test"], notes="Line one,\nline two; done"
    )
    text = _run(session, user, preset="full", output_format="vcard")
    assert text.startswith("BEGIN:VCARD\r\nVERSION:4.0\r\n")
    assert text.rstrip("\r\n").endswith("END:VCARD")
    assert "NOTE:Line one\\,\\nline two\\; done" in text


# --- filenames, filters, and sort ------------------------------------------------


def test_filename_for_each_format() -> None:
    assert filename_for("nine-column", "csv") == "contacts-nine-column.csv"
    assert filename_for("full", "json") == "contacts-full.json"
    assert filename_for("campaign-audience", "vcard") == "contacts-campaign-audience.vcf"


def test_export_respects_the_filter(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company="Acme Corp")
    factories.make_contact(session, user, current_company="Globex")
    tree = parse_filter({"where": {"op": "eq", "field": "current_company", "value": "Acme Corp"}})
    text = _run(session, user, preset="nine-column", output_format="json", tree=tree)
    rows = json.loads(text)
    assert len(rows) == 1
    assert rows[0]["current_company"] == "Acme Corp"


def test_export_respects_sort(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, last_name="Zephyr")
    factories.make_contact(session, user, last_name="Alpha")
    sort = [SortKey(field="last_name", direction="asc")]
    text = _run(session, user, preset="nine-column", output_format="json", sort=sort)
    rows = json.loads(text)
    assert [row["last_name"] for row in rows] == ["Alpha", "Zephyr"]


def test_export_batches_across_the_page_boundary(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result set larger than one batch is still returned whole, in order."""
    monkeypatch.setattr("netkeeper.crm.exports._BATCH_SIZE", 2)
    user = factories.make_user(session)
    for _ in range(5):
        factories.make_contact(session, user)
    text = _run(session, user, preset="nine-column", output_format="json")
    rows = json.loads(text)
    assert len(rows) == 5
    assert [row["last_name"] for row in rows] == [f"Last{n}" for n in range(1, 6)]
