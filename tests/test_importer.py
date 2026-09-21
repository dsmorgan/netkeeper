"""Reading a CSV and mapping its columns (P1-04): pure, no database in sight."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from netkeeper.crm.importer import (
    FIELD_SIZE_LIMIT,
    EmptyFile,
    ImportField,
    InvalidMapping,
    MalformedCsv,
    UnknownPreset,
    detect_preset,
    get_preset,
    map_row,
    mapping_from_json,
    normalize_header,
    parse_csv,
    parse_date,
    resolve_mapping,
)
from netkeeper.models import ContactSource

FIXTURES = Path(__file__).parent / "fixtures" / "csv"
LINKEDHELPER = (FIXTURES / "linkedhelper-sample.csv").read_text()
NINE_COLUMN = (FIXTURES / "nine-column-sample.csv").read_text()

# The LinkedIn archive's Connections.csv opens with a note, then a blank line.
ARCHIVE = (
    "Notes:\n"
    '"Your connections are exported with the fields below. Some may be empty."\n'
    "\n"
    "First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
    "Hortensia,Blennerhassett,https://www.linkedin.com/in/hortensia-b-qz/,"
    "hortensia@tarnish.example,Tarnish & Sons,Polisher,14 Mar 2026\n"
)


# --- finding the header -----------------------------------------------------


def test_the_header_is_the_first_row_with_columns_in_it() -> None:
    parsed = parse_csv(ARCHIVE)
    assert parsed.headers == (
        "First Name",
        "Last Name",
        "URL",
        "Email Address",
        "Company",
        "Position",
        "Connected On",
    )
    assert parsed.preamble_rows == 3
    assert len(parsed.rows) == 1


def test_a_header_cell_is_trimmed() -> None:
    parsed = parse_csv(LINKEDHELPER)
    assert "Location" in parsed.headers
    assert "Location " not in parsed.headers
    assert parsed.rows[0]["Location"] == "Pelican Bay, Farland"


def test_a_repeated_header_gets_a_suffix_so_every_column_has_a_key() -> None:
    parsed = parse_csv("Email,Email,Name\na@x.example,b@x.example,Perpetua\n")
    assert parsed.headers == ("Email", "Email (2)", "Name")
    assert parsed.rows[0] == {
        "Email": "a@x.example",
        "Email (2)": "b@x.example",
        "Name": "Perpetua",
    }


def test_an_empty_header_cell_is_named_after_its_position() -> None:
    parsed = parse_csv("First Name,,Last Name\nHortensia,x,Blennerhassett\n")
    assert parsed.headers == ("First Name", "column 2", "Last Name")


def test_short_and_long_rows_still_have_one_key_per_column() -> None:
    parsed = parse_csv("A,B,C\n1,2\n1,2,3,4\n")
    assert parsed.rows[0] == {"A": "1", "B": "2", "C": ""}
    assert parsed.rows[1] == {"A": "1", "B": "2", "C": "3"}
    assert parsed.dropped_cells == 1


def test_blank_lines_are_not_rows() -> None:
    parsed = parse_csv("A,B\n1,2\n\n,\n3,4\n")
    assert len(parsed.rows) == 2


def test_a_file_with_no_header_is_refused() -> None:
    with pytest.raises(EmptyFile):
        parse_csv("Notes:\n\n")


def _oversized_field() -> str:
    return 'A,Headline\n1,"' + "x" * (FIELD_SIZE_LIMIT + 1024) + '"\n'


def test_a_field_past_the_readers_limit_is_a_readable_error_not_a_crash() -> None:
    """``csv`` raises its own error for an oversized field, and it is no relation of ours."""
    with pytest.raises(MalformedCsv, match="not readable as CSV"):
        parse_csv(_oversized_field())


def test_the_field_limit_does_not_depend_on_what_another_reader_set() -> None:
    """``csv``'s cap is one process-wide global; the archive reader raises it to 16 MiB."""
    previous = csv.field_size_limit(64 * 1024 * 1024)
    try:
        with pytest.raises(MalformedCsv):
            parse_csv(_oversized_field())
        # And the reader put back what it found, so nobody else's file changes fate.
        assert csv.field_size_limit() == 64 * 1024 * 1024
    finally:
        csv.field_size_limit(previous)


def test_a_header_that_collides_with_a_suffix_still_gets_its_own_key() -> None:
    parsed = parse_csv("A,A,A (2)\n1,2,3\n")
    assert len(set(parsed.headers)) == len(parsed.headers)
    assert len(parsed.rows[0]) == 3
    assert sorted(parsed.rows[0].values()) == ["1", "2", "3"]


def test_bytes_are_read_as_utf8_then_as_windows_1252() -> None:
    assert parse_csv("﻿A,B\n1,2\n".encode()).headers == ("A", "B")
    assert parse_csv("Name,City\nFran\xe7oise,R\xfcgen\n".encode("cp1252")).rows[0] == {
        "Name": "Françoise",
        "City": "Rügen",
    }


# --- presets ----------------------------------------------------------------


def test_a_header_is_matched_however_it_is_spelled() -> None:
    assert normalize_header(" First_Name ") == normalize_header("FIRST NAME") == "firstname"


def test_each_sample_is_claimed_by_its_own_preset() -> None:
    assert detect_preset(parse_csv(LINKEDHELPER).headers) is not None
    assert detect_preset(parse_csv(LINKEDHELPER).headers).name == "linkedhelper"  # type: ignore[union-attr]
    assert detect_preset(parse_csv(NINE_COLUMN).headers).name == "nine-column"  # type: ignore[union-attr]
    assert detect_preset(parse_csv(ARCHIVE).headers).name == "linkedin-archive"  # type: ignore[union-attr]


def test_a_header_no_preset_recognizes_is_claimed_by_none() -> None:
    assert detect_preset(("Widget", "Sprocket", "Flange")) is None
    # One familiar column is not enough to claim a file.
    assert detect_preset(("First Name", "Sprocket")) is None


def test_the_nine_column_preset_matches_the_export_layout() -> None:
    mapping = get_preset("nine-column").mapping_for(parse_csv(NINE_COLUMN).headers)
    assert mapping == {
        "LinkedIn Profile URL": ImportField.LI_URL,
        "Email Address": ImportField.EMAIL,
        "First Name": ImportField.FIRST_NAME,
        "Last Name": ImportField.LAST_NAME,
        "CityState": ImportField.LOCATION,
        "Current Company": ImportField.CURRENT_COMPANY,
        "Current Job Title": ImportField.CURRENT_TITLE,
        "Phone Number": ImportField.PHONE,
    }


def test_an_unknown_preset_name_says_which_ones_exist() -> None:
    with pytest.raises(UnknownPreset, match="linkedhelper"):
        get_preset("mystery-tool")


# --- resolving a mapping ----------------------------------------------------


def test_an_override_wins_over_the_preset_and_an_empty_one_unmaps_a_column() -> None:
    headers = parse_csv(NINE_COLUMN).headers
    resolved = resolve_mapping(
        headers,
        preset=get_preset("nine-column"),
        overrides={"CityState": "headline", "Phone Number": ""},
    )
    assert resolved.mapping["CityState"] is ImportField.HEADLINE
    assert "Phone Number" not in resolved.mapping
    # Edited, so it is no longer the nine-column preset and is not filed as one.
    assert resolved.preset is None


def test_a_preset_accepted_unedited_keeps_its_name() -> None:
    headers = parse_csv(NINE_COLUMN).headers
    resolved = resolve_mapping(
        headers,
        preset=get_preset("nine-column"),
        overrides={"CityState": "location"},  # what the preset already says
    )
    assert resolved.preset == "nine-column"


def test_the_mapping_keeps_the_file_s_column_order() -> None:
    headers = parse_csv(NINE_COLUMN).headers
    resolved = resolve_mapping(headers, preset=get_preset("nine-column"))
    assert list(resolved.mapping) == [header for header in headers if header in resolved.mapping]


def test_unmapped_columns_are_reported() -> None:
    resolved = resolve_mapping(("First Name", "Sprocket"), overrides={"First Name": "first_name"})
    assert resolved.unmapped == ("Sprocket",)


def test_an_explicit_mapping_naming_a_column_the_file_lacks_is_refused() -> None:
    with pytest.raises(InvalidMapping, match="does not have"):
        resolve_mapping(("A",), overrides={"B": "first_name"})


def test_a_saved_preset_skips_a_column_this_file_does_not_have() -> None:
    """Presets are for reuse: the next file need not have every column of the last."""
    resolved = resolve_mapping(
        ("Given", "Family"),
        saved={"Given": "first_name", "Family": "last_name", "Works At": "current_company"},
    )
    assert resolved.mapping == {
        "Given": ImportField.FIRST_NAME,
        "Family": ImportField.LAST_NAME,
    }


def test_a_mapping_naming_a_field_an_import_cannot_write_is_refused() -> None:
    with pytest.raises(InvalidMapping, match="not a field"):
        resolve_mapping(("A",), overrides={"A": "notes"})


def test_a_mapping_that_maps_nothing_is_allowed_so_it_can_be_mapped_by_hand() -> None:
    # The guard lives on create_run, not here: a file whose headers no preset
    # knows has to reach the mapping screen before anyone can fix it.
    resolved = resolve_mapping(("A", "B"))
    assert resolved.mapping == {}
    assert resolved.preset is None
    assert resolved.unmapped == ("A", "B")


def test_a_stored_mapping_drops_columns_this_file_does_not_have() -> None:
    assert mapping_from_json({"A": "first_name", "B": "last_name"}, ["A"]) == {
        "A": ImportField.FIRST_NAME
    }


# --- one row ----------------------------------------------------------------

MAPPING = {
    "Profile Url": ImportField.LI_URL,
    "First Name": ImportField.FIRST_NAME,
    "Last Name": ImportField.LAST_NAME,
    "Email": ImportField.EMAIL,
    "Second Email": ImportField.EMAIL,
    "Phone": ImportField.PHONE,
    "Website": ImportField.LINK,
    "Connected At": ImportField.CONNECTED_ON,
    "Position": ImportField.CURRENT_TITLE,
    "Job Title": ImportField.CURRENT_TITLE,
}


def row(**cells: str) -> dict[str, str]:
    return {header: cells.get(header.replace(" ", "_").lower(), "") for header in MAPPING}


def test_a_row_becomes_an_incoming_contact() -> None:
    mapped = map_row(
        row(
            profile_url="https://www.linkedin.com/in/hortensia-b-qz/",
            first_name="Hortensia",
            last_name="Blennerhassett",
            email="hortensia@tarnish.example",
            second_email="h.blenner@tarnish.example",
            phone="+15550100003",
            website="https://tarnish.example/hortensia",
            connected_at="2026-03-14",
            position="Polisher",
        ),
        MAPPING,
    )
    assert mapped.problems == ()
    incoming = mapped.incoming
    assert incoming is not None
    assert incoming.source is ContactSource.CSV
    assert incoming.li_public_id == "hortensia-b-qz"
    assert incoming.current_title == "Polisher"
    assert incoming.connected_on == date(2026, 3, 14)
    assert [email.email for email in incoming.emails] == [
        "hortensia@tarnish.example",
        "h.blenner@tarnish.example",
    ]
    assert incoming.emails[0].is_primary and not incoming.emails[1].is_primary
    assert incoming.phones[0].number_e164 == "+15550100003"
    assert incoming.links[0].url == "https://tarnish.example/hortensia"


def test_the_first_column_feeding_a_scalar_field_wins() -> None:
    cells = row(first_name="Hortensia", position="Polisher", job_title="Senior Polisher")
    mapped = map_row(cells, MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.current_title == "Polisher"


def test_a_malformed_address_is_dropped_and_named() -> None:
    mapped = map_row(row(first_name="Hortensia", email="hortensia(at)tarnish.example"), MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.emails == ()
    assert mapped.problems == ("Email: 'hortensia(at)tarnish.example' is not an email address",)


def test_a_phone_number_with_no_digits_is_dropped_and_named() -> None:
    mapped = map_row(row(first_name="Hortensia", phone="ask her"), MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.phones == ()
    assert "has no digits" in (mapped.problem_text or "")


def test_a_url_that_is_not_a_linkedin_profile_is_dropped_and_named() -> None:
    mapped = map_row(row(first_name="Hortensia", profile_url="https://tarnish.example/h"), MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.li_url is None
    assert "not a LinkedIn profile URL" in (mapped.problem_text or "")


def test_a_bare_member_id_is_not_a_urn_and_is_dropped() -> None:
    """``li_urn`` is unique and is what resolution matches on first; a guess there sticks."""
    mapping = {"First Name": ImportField.FIRST_NAME, "Provider Id": ImportField.LI_URN}
    mapped = map_row({"First Name": "Hortensia", "Provider Id": "382910477"}, mapping)
    assert mapped.incoming is not None
    assert mapped.incoming.li_urn is None
    assert "is not a LinkedIn URN" in (mapped.problem_text or "")


@pytest.mark.parametrize(
    "urn", ["urn:li:fsd_profile/ACoAAAtest", "urn:li:fsd_profile:ACoAAAtest", "urn:li:person/42"]
)
def test_a_real_urn_is_kept(urn: str) -> None:
    mapping = {"Provider Id": ImportField.LI_URN, "First Name": ImportField.FIRST_NAME}
    mapped = map_row({"Provider Id": urn, "First Name": "Hortensia"}, mapping)
    assert mapped.incoming is not None
    assert mapped.incoming.li_urn == urn
    assert mapped.problems == ()


def test_a_date_in_no_readable_form_is_dropped_and_named() -> None:
    mapped = map_row(row(first_name="Hortensia", connected_at="last Michaelmas"), MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.connected_on is None
    assert "not a date" in (mapped.problem_text or "")


def test_a_row_that_identifies_nobody_is_not_importable() -> None:
    mapped = map_row(row(position="Polisher"), MAPPING)
    assert mapped.incoming is None
    assert "identifies nobody" in (mapped.problem_text or "")


def test_an_empty_cell_is_not_provided_rather_than_empty() -> None:
    mapped = map_row(row(first_name="Hortensia", position=""), MAPPING)
    assert mapped.incoming is not None
    assert mapped.incoming.current_title is None


# --- dates ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-03-14", date(2026, 3, 14)),
        ("2026-03-14T09:30:00Z", date(2026, 3, 14)),
        ("14 Mar 2026", date(2026, 3, 14)),
        ("14 March 2026", date(2026, 3, 14)),
        ("Mar 14, 2026", date(2026, 3, 14)),
        ("September 1, 2026", date(2026, 9, 1)),
        ("03/14/2026", date(2026, 3, 14)),
        ("03/14/26", date(2026, 3, 14)),
    ],
)
def test_the_spellings_these_exports_use(text: str, expected: date) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "last Michaelmas", "14 Smarch 2026", "02/30/2026", "2026"])
def test_anything_else_is_no_date(text: str) -> None:
    assert parse_date(text) is None
