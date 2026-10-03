"""``netkeeper.crm.exports`` (spec 10.6): CSV, JSON, and vCard over a filtered stream.

The nine-column round trip (P1-11's "done when") is the load-bearing test in
here: :func:`test_nine_column_round_trips_through_the_real_importer`. It drives
``netkeeper.crm.import_runs.create_run``/``commit`` — the actual importer, not a
private reimplementation of the same eight columns — because a stand-in reader
only proves the export function is the inverse of itself; it proves nothing
about whether the file the real importer sees comes back the same. See the
module docstring of ``netkeeper.crm.exports`` for what does and does not survive
that round trip and why (the "First Name" asymmetry is deliberate, not a bug).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import factories
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from netkeeper.campaigns.templates import contact_fields
from netkeeper.crm import contacts as contact_service
from netkeeper.crm import do_not_send, import_runs
from netkeeper.crm.exports import (
    FORMULA_TRIGGERS,
    FULL_FIELDS,
    LINKEDIN_ARCHIVE,
    NINE_COLUMN,
    _fold,
    _vcard_escape,
    _vcard_escape_uri,
    export_stream,
    filename_for,
)
from netkeeper.crm.filters import FilterTree, SortKey, parse_filter
from netkeeper.crm.importer import ImportField, get_preset
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Base,
    Contact,
    ContactLink,
    ContactSnapshot,
    DoNotSendReason,
    EmailStatus,
    LinkKind,
    User,
)
from netkeeper.scoping import get_scoped, install_scope_guard

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
    # csv.writer emits "\r\n" line endings; a plain "\n".join would only pass by
    # accident when the fixture has exactly one data row (there was no second
    # row here to catch it).
    assert headerless == with_header.split("\r\n", 1)[1]


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


def test_nine_column_round_trips_through_the_real_importer(
    session: Session, tmp_path: Path
) -> None:
    """Export, run the real nine-column importer into a clean database, export again: identical.

    Both fixture contacts here are fully identified (a name in every row), so
    neither is the "nothing identifying" case; that one gets its own test below,
    since asserting it *out* of this file is the point of this test, not an
    incidental side effect of it.
    """
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        li_public_id="wren-oakhollow",
        preferred_name="Wren",
        last_name="Oakhollow",
        emails=["wren.oakhollow@example.test"],
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
        preferred_name="Ibby",
        last_name="Thistlewood",
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
        with session_scope(factory2, write=True) as session2:
            user2 = factories.make_user(session2)
            run = import_runs.create_run(
                session2,
                user2,
                filename="export.csv",
                content=first_export,
                preset_name="nine-column",
            )
            import_runs.commit(session2, user2, run.id)
            second_export = _run(session2, user2, preset="nine-column", output_format="csv")
    finally:
        engine2.dispose()

    assert second_export == first_export, "IDENTICAL: False"


def test_nine_column_export_mapping_agrees_with_the_importer_except_first_name() -> None:
    """The export's column-to-field mapping must match ``importer.PRESETS["nine-column"]``.

    Except "First Name": that one is deliberately asymmetric (module docstring
    of ``netkeeper.crm.exports``) — the export reads ``preferred_name``, the
    importer writes ``first_name``, on purpose, so a mail merge addresses people
    by the name you actually call them. Every *other* column must agree, or the
    round trip breaks silently the next time either side changes independently.
    """
    preset = get_preset("nine-column")
    mapping = preset.mapping_for([column.header for column in NINE_COLUMN])
    expected_field_by_json_key = {
        "linkedin_profile_url": ImportField.LI_URL,
        "email_address": ImportField.EMAIL,
        "last_name": ImportField.LAST_NAME,
        "city_state": ImportField.LOCATION,
        "current_company": ImportField.CURRENT_COMPANY,
        "current_job_title": ImportField.CURRENT_TITLE,
        "phone_number": ImportField.PHONE,
    }
    for column in NINE_COLUMN:
        if column.json_key == "first_name":
            continue  # the deliberate asymmetry: preferred_name out, first_name in
        assert mapping[column.header] == expected_field_by_json_key[column.json_key], column.header
    assert mapping["First Name"] is ImportField.FIRST_NAME  # confirms the asymmetry itself


def test_nine_column_export_skips_a_row_with_nothing_identifying(session: Session) -> None:
    """A contact with no LinkedIn identity, no name, no email, and no phone is not exported.

    The real importer refuses exactly this row ("the row identifies nobody");
    company, title, and location alone are not enough. Exporting it anyway
    would silently drop it on its own round trip.
    """
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        li_urn=None,
        li_public_id=None,
        first_name="",
        last_name="",
        preferred_name="",
        location="Austin, TX",
        current_company="Acme Corp",
        current_title="Engineer",
    )
    text = _run(session, user, preset="nine-column", output_format="csv")
    assert text.splitlines()[1:] == []  # header only, no data row

    rows = json.loads(_run(session, user, preset="nine-column", output_format="json"))
    assert rows == []


def test_nine_column_export_drops_an_unparseable_phone(session: Session) -> None:
    """A phone value with no digits at all is exported as no phone, not as that text."""
    user = factories.make_user(session)
    factories.make_contact(session, user, phones=["ask reception"])
    rows = json.loads(_run(session, user, preset="nine-column", output_format="json"))
    assert rows[0]["phone_number"] is None


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


def test_full_json_includes_children(session: Session) -> None:
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


_INTERNAL_FIELDS = (
    "id",
    "user_id",
    "contact_id",
    "field_sources",
    "synced_values",
    "enrich_priority",
    "li_missing_count",
    "merged_into_id",
    "created_at",
    "updated_at",
    "li_urn",
)
# Values planted in those fields, distinct enough that finding one anywhere in
# the file can only mean the field leaked.
_PLANTED_USER_ID = 424201
_PLANTED_CONTACT_ID = 424202
_PLANTED_VALUES = (
    str(_PLANTED_USER_ID),
    str(_PLANTED_CONTACT_ID),
    "PLANTEDURN",
    "PLANTEDSOURCE",
    "PLANTEDSYNCED",
    "93717",  # enrich_priority
    "82431",  # li_missing_count
    "2001-02-03",  # created_at
    "2002-03-04",  # updated_at
)


def _field_names(text: str, output_format: str) -> set[str]:
    """Every field name ``text`` carries, normalized to snake_case."""
    names: set[str] = set()
    if output_format == "json":

        def walk(value: object) -> None:
            if isinstance(value, dict):
                names.update(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(json.loads(text))
    elif output_format == "csv":
        header = next(csv.reader(io.StringIO(text)))
        names.update(header)
    else:
        for line in text.split("\r\n"):
            if line and not line.startswith(" "):
                names.add(line.split(":", 1)[0].split(";", 1)[0])
    return {name.strip().lower().replace(" ", "_").replace("-", "_") for name in names}


@pytest.mark.parametrize(
    ("preset", "output_format"),
    [
        *(
            (preset, output_format)
            for preset in ("nine-column", "linkedin-archive", "full", "campaign-audience")
            for output_format in ("csv", "json", "vcard")
        ),
        ("macos-contacts", "vcard"),  # vCard only (#249)
    ],
)
def test_no_preset_or_format_leaks_internal_fields(
    session: Session, preset: str, output_format: str
) -> None:
    """All 13 preset/format combinations, by field name and by planted value (#77)."""
    user = factories.make_user(session, id=_PLANTED_USER_ID)
    contact = factories.make_contact(
        session,
        user,
        id=_PLANTED_CONTACT_ID,
        emails=["a@example.test"],
        phones=["+15551234567"],
        positions=[{"title": "Engineer", "company": "Acme"}],
        notes="Met at a conference",
        li_urn="urn:li:fsd_profile/PLANTEDURN",
        field_sources={"headline": "PLANTEDSOURCE"},
        synced_values={
            "headline": {
                "value": "PLANTEDSYNCED",
                "source": "sync",
                "observed_at": "2020-01-01T00:00:00+00:00",
            }
        },
        enrich_priority=93717,
        li_missing_count=82431,
        created_at=datetime(2001, 2, 3, tzinfo=UTC),
        updated_at=datetime(2002, 3, 4, tzinfo=UTC),
    )
    session.add(ContactSnapshot(user_id=user.id, contact_id=contact.id, headline="Before"))
    session.commit()

    text = _run(session, user, preset=preset, output_format=output_format)
    assert "a@example.test" in text  # the contact is in the file, so absence below means something

    leaked_names = _field_names(text, output_format) & set(_INTERNAL_FIELDS)
    assert not leaked_names, f"{sorted(leaked_names)} leaked into {preset}/{output_format}"
    leaked_values = [value for value in _PLANTED_VALUES if value in text]
    assert not leaked_values, f"{leaked_values} leaked into {preset}/{output_format}"


def test_full_csv_flattens_children(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, emails=["one@example.test", "two@example.test"], phones=[]
    )
    text = _run(session, user, preset="full", output_format="csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert "one@example.test" in rows[0]["emails"]
    assert "two@example.test" in rows[0]["emails"]


# --- spreadsheet_safe: opt-in formula quoting for CSV (#76) --------------------

# The four values #76 verified pass straight through today, plus the two
# whitespace triggers and the phone number the issue says must not be mangled
# by default.
_FORMULA_LOOKING = ("=1+1", "+41 name", "-2+3", "@SUM(A1)", "\tTAB", "\rCR")


def test_formula_triggers_are_pinned() -> None:
    """Safety-relevant constant: one test pins its literal value (CLAUDE.md)."""
    assert FORMULA_TRIGGERS == ("=", "+", "-", "@", "\t", "\r")


def test_csv_passes_formula_looking_values_through_by_default(session: Session) -> None:
    """Off by default: the round trip is P1-11's deliverable, so the bytes stay the bytes."""
    user = factories.make_user(session)
    for value in _FORMULA_LOOKING:
        factories.make_contact(session, user, current_company=value, phones=["+15551234567"])
    text = _run(session, user, preset="nine-column", output_format="csv")
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    assert [row["Current Company"] for row in rows] == list(_FORMULA_LOOKING)
    assert {row["Phone Number"] for row in rows} == {"+15551234567"}


@pytest.mark.parametrize("value", _FORMULA_LOOKING)
def test_spreadsheet_safe_quotes_every_formula_trigger(session: Session, value: str) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company=value)
    text = _run(session, user, preset="nine-column", output_format="csv", spreadsheet_safe=True)
    (row,) = csv.DictReader(io.StringIO(text, newline=""))
    assert row["Current Company"] == "'" + value


@pytest.mark.parametrize("value", [" =1+1", "\n=1+1", "  @SUM(A1)", "\n\t-2+3", " \r+41 name"])
def test_spreadsheet_safe_quotes_a_trigger_behind_leading_whitespace(
    session: Session, value: str
) -> None:
    """Importers that trim leading whitespace still reach the ``=`` (#215 review)."""
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company=value)
    text = _run(session, user, preset="nine-column", output_format="csv", spreadsheet_safe=True)
    (row,) = csv.DictReader(io.StringIO(text, newline=""))
    assert row["Current Company"] == "'" + value


@pytest.mark.parametrize("value", ["  Acme Corp", "\nAcme", "   "])
def test_spreadsheet_safe_leaves_ordinary_text_behind_whitespace_alone(
    session: Session, value: str
) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company=value, emails=["a@example.test"])
    text = _run(session, user, preset="nine-column", output_format="csv", spreadsheet_safe=True)
    (row,) = csv.DictReader(io.StringIO(text, newline=""))
    assert row["Current Company"] == value


def test_spreadsheet_safe_leaves_ordinary_and_empty_cells_and_the_header_alone(
    session: Session,
) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, current_company="Acme Corp", current_title="", location="Austin, TX"
    )
    plain = _run(session, user, preset="nine-column", output_format="csv")
    safe = _run(session, user, preset="nine-column", output_format="csv", spreadsheet_safe=True)
    assert safe == plain


def test_spreadsheet_safe_quotes_a_leading_plus_phone(session: Session) -> None:
    """The cost #76 names: ``+1…`` gains a quote, which is why this is opt-in."""
    user = factories.make_user(session)
    factories.make_contact(session, user, phones=["+15551234567"])
    text = _run(session, user, preset="nine-column", output_format="csv", spreadsheet_safe=True)
    (row,) = csv.DictReader(io.StringIO(text))
    assert row["Phone Number"] == "'+15551234567"


@pytest.mark.parametrize("preset", ["linkedin-archive", "full", "campaign-audience"])
def test_spreadsheet_safe_applies_to_every_csv_preset(session: Session, preset: str) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, emails=["a@example.test"], current_company="=HYPERLINK(1)"
    )
    text = _run(session, user, preset=preset, output_format="csv", spreadsheet_safe=True)
    assert "'=HYPERLINK(1)" in text
    assert ",=HYPERLINK" not in text


@pytest.mark.parametrize("output_format", ["json", "vcard"])
def test_spreadsheet_safe_is_ignored_outside_csv(session: Session, output_format: str) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, current_company="=1+1")
    plain = _run(session, user, preset="nine-column", output_format=output_format)
    safe = _run(
        session, user, preset="nine-column", output_format=output_format, spreadsheet_safe=True
    )
    assert safe == plain
    assert "'=1+1" not in safe


# --- campaign-audience: the merge fields plus the recipient email --------------


def test_campaign_audience_computes_connection_age_and_position_change(
    session: Session,
) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        emails=["reach@example.test"],
        connected_on=date(2020, 6, 1),
        positions=[{"title": "Lead", "started_on": date(2025, 3, 1)}],
    )
    session.commit()

    text = _run(session, user, preset="campaign-audience", output_format="json", now=NOW)
    (row,) = json.loads(text)
    assert row["email"] == "reach@example.test"
    assert row["connected_year"] == "2020"
    assert row["years_since_connected"] == "6"  # connected 2020-06-01, "now" is 2026-09-21
    assert row["last_position_change"] == "2025-03-01"


def test_campaign_audience_position_change_ignores_a_newer_headline_only_snapshot(
    session: Session,
) -> None:
    """A headline edit is not a job change: the column reads the positions' dates,
    as the merge field does (spec 11.1, #333), not the newest snapshot."""
    user = factories.make_user(session)
    contact = factories.make_contact(
        session,
        user,
        emails=["reach@example.test"],
        positions=[{"title": "Lead", "started_on": date(2025, 3, 1)}],
    )
    session.add(
        ContactSnapshot(
            user_id=user.id,
            contact_id=contact.id,
            headline="Speaker, writer, occasional sailor",
            observed_at=datetime(2026, 8, 1, tzinfo=UTC),
        )
    )
    session.commit()

    text = _run(session, user, preset="campaign-audience", output_format="json", now=NOW)
    (row,) = json.loads(text)
    assert row["last_position_change"] == "2025-03-01"

    csv_text = _run(session, user, preset="campaign-audience", output_format="csv", now=NOW)
    (csv_row,) = csv.DictReader(io.StringIO(csv_text))
    assert csv_row["Last Position Change"] == "2025-03-01"


_POSITION_CASES: dict[str, list[dict[str, object]]] = {
    "no positions": [],
    "undated position": [{"title": "Engineer"}],
    "future-dated start": [
        {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2026, 8, 31)},
        {"title": "Lead", "started_on": date(2026, 12, 1)},
    ],
    "future-dated end": [
        {"title": "Engineer", "started_on": date(2021, 6, 1), "ended_on": date(2027, 1, 31)},
    ],
    "ended position": [
        {"title": "Advisor", "started_on": date(2019, 4, 1)},
        {"title": "VP", "started_on": date(2021, 2, 1), "ended_on": date(2026, 8, 15)},
    ],
}


@pytest.mark.parametrize("case", list(_POSITION_CASES))
def test_campaign_audience_position_change_equals_the_merge_field(
    session: Session, case: str
) -> None:
    """The column and the ``last_position_change`` merge field agree (#333), with a
    newer snapshot present so a snapshot-based reading would disagree."""
    user = factories.make_user(session)
    contact = factories.make_contact(
        session, user, emails=["reach@example.test"], positions=_POSITION_CASES[case]
    )
    session.add(
        ContactSnapshot(
            user_id=user.id,
            contact_id=contact.id,
            headline="New headline",
            observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        )
    )
    session.commit()

    merge_value = contact_fields(contact, NOW.date())["last_position_change"]
    assert merge_value is None or isinstance(merge_value, date)
    text = _run(session, user, preset="campaign-audience", output_format="json", now=NOW)
    (row,) = json.loads(text)
    assert row["last_position_change"] == (None if merge_value is None else merge_value.isoformat())


def test_campaign_audience_export_does_not_query_per_contact(session: Session) -> None:
    """The position change reads eager-loaded positions: forty contacts cost the
    same number of queries as three, all inside one batch."""
    user = factories.make_user(session)
    user_id = user.id

    def add_contacts(count: int) -> None:
        for n in range(count):
            factories.make_contact(
                session,
                user,
                emails=[f"reach{n}-{count}@example.test"],
                positions=[
                    {"title": "Lead", "started_on": date(2024, 1, 1)},
                    {
                        "title": "Engineer",
                        "started_on": date(2019, 1, 1),
                        "ended_on": date(2023, 12, 1),
                    },
                ],
            )
        session.commit()

    def count_queries() -> tuple[int, int]:
        session.expire_all()  # nothing already loaded may hide a per-row lazy load
        exporter = session.get(User, user_id)
        assert exporter is not None
        statements: list[str] = []

        def record(*args: Any) -> None:
            statements.append(args[2])

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record)
        try:
            text = _run(
                session, exporter, preset="campaign-audience", output_format="json", now=NOW
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
        rows = json.loads(text)
        assert all(row["last_position_change"] == "2024-01-01" for row in rows)
        return len(rows), len(statements)

    add_contacts(3)
    few_rows, few_queries = count_queries()
    add_contacts(37)
    many_rows, many_queries = count_queries()
    assert (few_rows, many_rows) == (3, 40)
    assert many_queries == few_queries


def test_campaign_audience_never_exports_a_do_not_contact_person(session: Session) -> None:
    """Producing a mail-merge file is a send path by proxy (spec F15): honor do_not_contact.

    Held out regardless of the caller's own filter, not only when nobody asked
    for do-not-contact people specifically — the preset itself must never
    produce this row.
    """
    user = factories.make_user(session)
    factories.make_contact(session, user, emails=["reach@example.test"], do_not_contact=False)
    factories.make_contact(
        session,
        user,
        emails=["leave-me-alone@example.test"],
        do_not_contact=True,
        do_not_contact_reason="asked to stop",
    )
    text = _run(session, user, preset="campaign-audience", output_format="json")
    rows = json.loads(text)
    assert [row["email"] for row in rows] == ["reach@example.test"]

    # Other presets are unaffected: do-not-contact is a fact about the contact,
    # not something every export hides.
    full_text = _run(session, user, preset="full", output_format="json")
    full_rows = json.loads(full_text)
    assert {row["do_not_contact"] for row in full_rows} == {False, True}


def test_campaign_audience_never_exports_a_contact_waiting_for_review(session: Session) -> None:
    """#184: a contact read off a connections-page card is nobody to reach yet."""
    user = factories.make_user(session)
    factories.make_contact(session, user, emails=["reach@example.test"])
    factories.make_contact(
        session,
        user,
        emails=["card@example.test"],
        needs_review_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    rows = json.loads(_run(session, user, preset="campaign-audience", output_format="json"))
    assert [row["email"] for row in rows] == ["reach@example.test"]

    full_rows = json.loads(_run(session, user, preset="full", output_format="json"))
    assert len(full_rows) == 2  # other presets still export it


@pytest.mark.parametrize(
    "preset", ["nine-column", "linkedin-archive", "full", "campaign-audience", "macos-contacts"]
)
def test_every_vcard_preset_leaves_out_a_contact_waiting_for_review(
    session: Session, preset: str
) -> None:
    """#254, David's decision: a vCard goes into an address book, and a card read
    off a connections page does not belong there until it is confirmed."""
    user = factories.make_user(session)
    factories.make_contact(
        session, user, first_name="Confirmed", last_name="Person", emails=["kept@example.test"]
    )
    factories.make_contact(
        session,
        user,
        first_name="Waiting",
        last_name="Card",
        emails=["card@example.test"],
        needs_review_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    text = _run(session, user, preset=preset, output_format="vcard")
    assert text.count("BEGIN:VCARD") == 1
    assert "kept@example.test" in text and "Confirmed" in text
    assert "card@example.test" not in text and "Waiting" not in text


@pytest.mark.parametrize("preset", ["nine-column", "linkedin-archive", "full"])
@pytest.mark.parametrize("output_format", ["csv", "json"])
def test_csv_and_json_still_carry_a_contact_waiting_for_review(
    session: Session, preset: str, output_format: str
) -> None:
    """Only vCard was decided on #254; the other formats are unchanged outside
    campaign-audience."""
    user = factories.make_user(session)
    factories.make_contact(
        session,
        user,
        emails=["card@example.test"],
        needs_review_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert "card@example.test" in _run(session, user, preset=preset, output_format=output_format)


def test_campaign_audience_skips_a_bounced_primary_for_the_next_address(
    session: Session,
) -> None:
    """Spec 11.9: "channel address present and not bounced" (#77)."""
    user = factories.make_user(session)
    contact = factories.make_contact(
        session, user, emails=["bounced@example.test", "works@example.test"]
    )
    contact.emails[0].status = EmailStatus.BOUNCED
    session.commit()

    (row,) = json.loads(_run(session, user, preset="campaign-audience", output_format="json"))
    assert row["email"] == "works@example.test"
    text = _run(session, user, preset="campaign-audience", output_format="csv")
    assert "bounced@example.test" not in text


def test_campaign_audience_keeps_a_contact_whose_only_address_bounced_without_it(
    session: Session,
) -> None:
    """The row stays, email empty, like a contact with no email: LinkedIn still reaches them."""
    user = factories.make_user(session)
    contact = factories.make_contact(session, user, emails=["bounced@example.test"])
    contact.emails[0].status = EmailStatus.BOUNCED
    session.commit()

    for output_format in ("csv", "json", "vcard"):
        text = _run(session, user, preset="campaign-audience", output_format=output_format)
        assert "bounced@example.test" not in text, output_format
    (row,) = json.loads(_run(session, user, preset="campaign-audience", output_format="json"))
    assert row["email"] is None
    assert row["linkedin_profile_url"] == contact.li_url


def test_campaign_audience_skips_an_address_bounced_on_a_deleted_contact(
    session_factory: sessionmaker[Session],
) -> None:
    """#238 review probe (a): A bounced, so its address is listed; A is deleted. B holds the
    same address as ok, and must not be exported with it."""
    with session_scope(session_factory, write=True) as writer:
        user = factories.make_user(writer)
        a = factories.make_contact(writer, user, emails=["shared@example.test"])
        contact_service.update_email(writer, user, a.id, a.emails[0].id, {"status": "bounced"})
        writer.delete(a)
        b = factories.make_contact(
            writer, user, emails=["shared@example.test", "other@example.test"]
        )
        c = factories.make_contact(writer, user, emails=["SHARED@example.test"])
        b_id, c_id, user_id = b.id, c.id, user.id
    with session_scope(session_factory) as reader:
        found = reader.get(User, user_id)
        assert found is not None
        user = found
        rows = json.loads(_run(reader, user, preset="campaign-audience", output_format="json"))
        by_url = {r["linkedin_profile_url"]: r["email"] for r in rows}
        urls = {
            contact_id: get_scoped(reader, user, Contact, contact_id).li_url  # type: ignore[union-attr]
            for contact_id in (b_id, c_id)
        }
        assert by_url == {urls[b_id]: "other@example.test", urls[c_id]: None}
        text = _run(reader, user, preset="campaign-audience", output_format="csv")
        assert "shared@example.test" not in text.lower()


def test_campaign_audience_drops_a_contact_holding_an_opted_out_address(
    session_factory: sessionmaker[Session],
) -> None:
    """#238 review probe (b): the address opted out, the contact holding it never got
    ``do_not_contact``. The row is dropped, as a do-not-contact one is. A ``+tag`` address
    is someone else."""
    with session_scope(session_factory, write=True) as writer:
        user = factories.make_user(writer)
        held = factories.make_contact(
            writer, user, emails=["work@example.test", "home@example.test"]
        )
        tagged = factories.make_contact(writer, user, emails=["home+nk1@example.test"])
        do_not_send.add(writer, user, "home@example.test", DoNotSendReason.OPTED_OUT)
        assert not held.do_not_contact
        tagged_id, user_id = tagged.id, user.id
    with session_scope(session_factory) as reader:
        found = reader.get(User, user_id)
        assert found is not None
        user = found
        rows = json.loads(_run(reader, user, preset="campaign-audience", output_format="json"))
        assert [r["email"] for r in rows] == ["home+nk1@example.test"]
        kept = get_scoped(reader, user, Contact, tagged_id)
        assert kept is not None and rows[0]["linkedin_profile_url"] == kept.li_url
        # The re-importable presets are a copy of the data, not a send list.
        nine = _run(reader, user, preset="nine-column", output_format="csv")
        assert "work@example.test" in nine


def test_campaign_audience_still_exports_an_invalid_address(session: Session) -> None:
    """An ``invalid`` status set with no list entry (one a person removed, or set before the
    list existed) still exports; a listed one does not (#215, #238)."""
    user = factories.make_user(session)
    contact = factories.make_contact(session, user, emails=["invalid@example.test"])
    contact.emails[0].status = EmailStatus.INVALID
    session.commit()

    (row,) = json.loads(_run(session, user, preset="campaign-audience", output_format="json"))
    assert row["email"] == "invalid@example.test"


def test_re_importable_presets_still_carry_a_bounced_primary(session: Session) -> None:
    """Only the send-list preset filters bounces; nine-column is a copy of the data (#215)."""
    user = factories.make_user(session)
    contact = factories.make_contact(session, user, emails=["bounced@example.test"])
    contact.emails[0].status = EmailStatus.BOUNCED
    session.commit()

    (row,) = json.loads(_run(session, user, preset="nine-column", output_format="json"))
    assert row["email_address"] == "bounced@example.test"


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


def test_vcard_escape_turns_a_bare_carriage_return_into_one_newline() -> None:
    """A bare ``\r`` (old Mac line endings) is a line break too; ``\r\n`` is one
    break, not two. The 3.0 and 4.0 exports share this (#254)."""
    assert _vcard_escape("one\rtwo\r\nthree\nfour") == "one\\ntwo\\nthree\\nfour"
    assert "\r" not in _vcard_escape("\r\r\n\r")


def test_vcard_fold_backs_off_a_character_straddling_the_first_fold() -> None:
    """A 3-octet character across octet 75 moves whole to the next physical line."""
    line = "NOTE:" + "a" * 69 + "€" + "b" * 5  # the € is octets 74-76
    assert _fold(line) == "NOTE:" + "a" * 69 + "\r\n €" + "b" * 5


def test_vcard_fold_backs_off_past_every_continuation_octet() -> None:
    """A 4-octet character whose lead octet is two before the fold point."""
    line = "NOTE:" + "a" * 68 + "😀" + "b" * 5  # the emoji is octets 73-76
    assert _fold(line) == "NOTE:" + "a" * 68 + "\r\n 😀" + "b" * 5


def test_vcard_fold_backs_off_on_a_continuation_line_too() -> None:
    """Continuation lines hold 74 octets after their space; the check applies there as well."""
    line = "NOTE:" + "a" * 143 + "€" + "b" * 5  # the € is octets 148-150
    assert _fold(line) == "NOTE:" + "a" * 70 + "\r\n " + "a" * 73 + "\r\n €" + "b" * 5


def test_vcard_fold_backs_off_three_continuation_octets() -> None:
    """A 4-octet character whose lead octet is three before the fold point: a
    back-off capped at two steps would split it (#257 review)."""
    line = "NOTE:" + "a" * 67 + "😀" + "b" * 5  # the emoji is octets 73-76
    assert _fold(line) == "NOTE:" + "a" * 67 + "\r\n 😀" + "b" * 5


def test_vcard_fold_recognizes_a_continuation_octet_above_0x9f() -> None:
    """``ü``'s second octet is 0xBC; a ``0xE0`` mask would read it as a lead octet
    and split the character (#257 review)."""
    line = "NOTE:" + "a" * 69 + "ü" + "b" * 5  # the ü is octets 75-76
    assert "ü".encode()[1] == 0xBC
    assert _fold(line) == "NOTE:" + "a" * 69 + "\r\n ü" + "b" * 5


def test_full_vcard_contains_begin_end_and_crlf_line_endings(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(
        session, user, emails=["a@example.test"], notes="Line one,\nline two; done"
    )
    text = _run(session, user, preset="full", output_format="vcard")
    assert text.startswith("BEGIN:VCARD\r\nVERSION:4.0\r\n")
    assert text.rstrip("\r\n").endswith("END:VCARD")
    assert "NOTE:Line one\\,\\nline two\\; done" in text


def test_vcard_uri_escape_percent_encodes_comma_and_semicolon() -> None:
    """``URL`` is a URI: ``,``/``;`` become ``%2C``/``%3B``, never ``\\,`` (#77, #215)."""
    assert (
        _vcard_escape_uri("https://example.test/p?q=a,b;c") == "https://example.test/p?q=a%2Cb%3Bc"
    )
    assert _vcard_escape_uri("https://example.test/a\\b") == "https://example.test/a\\\\b"
    assert _vcard_escape_uri("https://example.test/\r\nx") == "https://example.test/\\nx"
    # Already-encoded text is left alone rather than double-encoded.
    assert _vcard_escape_uri("https://example.test/?q=a%2Cb") == "https://example.test/?q=a%2Cb"


@pytest.mark.parametrize("preset", ["nine-column", "full"])
def test_vcard_url_with_comma_and_semicolon_is_percent_encoded(
    session: Session, preset: str
) -> None:
    """vobject cuts a URL at a bare ``,``; a strict parser reads ``\\,`` literally (#215)."""
    user = factories.make_user(session)
    contact = factories.make_contact(session, user, li_url="https://example.test/in/a,b;c")
    contact.links.append(
        ContactLink(user_id=user.id, url="https://example.test/p?q=a,b;c", kind=LinkKind.WEBSITE)
    )
    session.commit()
    text = _run(session, user, preset=preset, output_format="vcard")
    url_lines = [line for line in text.split("\r\n") if line.startswith("URL:")]
    assert "URL:https://example.test/in/a%2Cb%3Bc" in url_lines
    if preset == "full":
        assert "URL:https://example.test/p?q=a%2Cb%3Bc" in url_lines
    for line in url_lines:
        value = line.removeprefix("URL:")
        assert "," not in value and ";" not in value and "\\" not in value, line


def test_full_vcard_fn_keeps_the_surname_with_a_preferred_name(session: Session) -> None:
    """FN is the canonical display name; it was ``preferred_name`` alone (#77)."""
    user = factories.make_user(session)
    factories.make_contact(
        session, user, first_name="Zoe", preferred_name="Zoë", last_name="Müller-Łukasz"
    )
    text = _run(session, user, preset="full", output_format="vcard")
    assert "FN:Zoë Müller-Łukasz\r\n" in text
    assert "N:Müller-Łukasz;Zoe;;;\r\n" in text


def test_full_vcard_fn_falls_back_to_first_name_and_then_to_unknown(session: Session) -> None:
    user = factories.make_user(session)
    factories.make_contact(session, user, first_name="Ada", preferred_name=None, last_name="")
    factories.make_contact(session, user, first_name="", preferred_name=None, last_name="")
    text = _run(session, user, preset="full", output_format="vcard")
    assert "FN:Ada\r\n" in text
    assert "FN:Unknown\r\n" in text


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
