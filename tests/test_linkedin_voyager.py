"""netkeeper.linkedin.voyager: headers, endpoint constants, and Voyager parsers.

The fixtures under ``tests/fixtures/voyager`` are hand-written and entirely
invented, as every fixture in this repository is (CLAUDE.md): fake names,
fake URNs, fake companies, no real email addresses or phone numbers, and none
of it copied from ``~/code/netkeeper-private`` or fetched from linkedin.com —
see the "Capturing and sanitizing fixtures" section of
``netkeeper/linkedin/voyager.py``'s module docstring for the procedure they
follow.

This item's "done when" (issue #98) is that every parser has fixture tests
and that an unknown shape raises :class:`RouteChanged`, never ``KeyError``.
The malformed-input tests below are written to fail loudly if a parser ever
goes back to a bare ``dict`` subscript: each one was checked by hand against
a deliberately unguarded version of its parser and confirmed to fail with
``KeyError`` there, exactly as the review standard in CLAUDE.md asks for.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from netkeeper.linkedin.voyager import (
    CONNECTIONS_DEFAULT_COUNT,
    CONNECTIONS_ENDPOINT,
    CONTACT_INFO_ENDPOINT,
    CONVERSATIONS_ENDPOINT,
    PROFILE_ENDPOINT,
    RESTLI_PROTOCOL_VERSION,
    ConnectionsPageResult,
    ContactInfo,
    ConversationsPageResult,
    ProfileDetails,
    RouteChanged,
    VoyagerFetch,
    VoyagerRequest,
    VoyagerResponse,
    build_headers,
    connections_query,
    contact_info_path,
    conversations_query,
    parse_connections_page,
    parse_contact_info,
    parse_conversations_page,
    parse_profile_details,
    profile_query,
    strip_jsessionid,
)

FIXTURES = Path(__file__).parent / "fixtures" / "voyager"
# What ADR 0005 keeps out of the extractor (mirrors test_linkedin_archive.py).
FORBIDDEN_IMPORTS = ("netkeeper.models", "netkeeper.crm", "netkeeper.db", "sqlalchemy")


def _body(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- the boundary -------------------------------------------------------------


def test_voyager_loads_no_models_and_no_session() -> None:
    """ADR 0005: nothing under ``linkedin/`` imports the ORM or opens a session.

    Run in a subprocess for the same reason test_linkedin_archive.py does:
    this test module has the whole package imported already, so checking
    ``sys.modules`` in-process would see every one of these regardless of
    what the module under test did.
    """
    script = (
        "import sys, json\n"
        "import netkeeper.linkedin.voyager\n"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent.parent,
    )
    loaded = json.loads(result.stdout)
    leaked = [
        name
        for name in loaded
        for forbidden in FORBIDDEN_IMPORTS
        if name == forbidden or name.startswith(f"{forbidden}.")
    ]
    assert leaked == [], f"netkeeper/linkedin/voyager.py pulled in {leaked}"


# --- header builder -------------------------------------------------------------


class TestStripJessionid:
    def test_strips_surrounding_quotes(self) -> None:
        assert strip_jsessionid('"ajax:1234567890123456789"') == "ajax:1234567890123456789"

    def test_passes_through_an_unquoted_value(self) -> None:
        assert strip_jsessionid("ajax:1234567890123456789") == "ajax:1234567890123456789"

    def test_strips_surrounding_whitespace_too(self) -> None:
        assert strip_jsessionid('  "ajax:abc"  ') == "ajax:abc"

    def test_a_lone_quote_character_is_left_alone(self) -> None:
        # Too short to be "quoted": stripping it would just discard the value.
        assert strip_jsessionid('"') == '"'


class TestBuildHeaders:
    def test_csrf_token_is_the_stripped_jsessionid(self) -> None:
        headers = build_headers('"ajax:1234567890123456789"')
        assert headers["csrf-token"] == "ajax:1234567890123456789"
        assert '"' not in headers["csrf-token"]

    def test_restli_protocol_version_is_sent(self) -> None:
        headers = build_headers('"ajax:abc"')
        assert headers["x-restli-protocol-version"] == RESTLI_PROTOCOL_VERSION == "2.0.0"

    def test_accept_and_li_lang_headers_are_present(self) -> None:
        headers = build_headers('"ajax:abc"')
        assert headers["accept"] == "application/vnd.linkedin.normalized+json+2.1"
        assert headers["x-li-lang"] == "en_US"

    def test_empty_jsessionid_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            build_headers("")

    def test_whitespace_only_jsessionid_is_refused(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            build_headers("   ")

    def test_extra_headers_are_merged_in(self) -> None:
        headers = build_headers('"ajax:abc"', extra={"x-li-track": '{"osName":"web"}'})
        assert headers["x-li-track"] == '{"osName":"web"}'
        assert headers["csrf-token"] == "ajax:abc"

    def test_extra_overrides_a_default_header(self) -> None:
        headers = build_headers('"ajax:abc"', extra={"accept": "application/json"})
        assert headers["accept"] == "application/json"


# --- query builders -------------------------------------------------------------


class TestConnectionsQuery:
    def test_shape(self) -> None:
        query = connections_query(start=40, count=40)
        assert query["start"] == "40"
        assert query["count"] == "40"
        assert query["decorationId"]

    def test_default_count(self) -> None:
        query = connections_query(start=0)
        assert query["count"] == str(CONNECTIONS_DEFAULT_COUNT)

    def test_negative_start_refused(self) -> None:
        with pytest.raises(ValueError, match="start"):
            connections_query(start=-1)

    def test_non_positive_count_refused(self) -> None:
        with pytest.raises(ValueError, match="count"):
            connections_query(start=0, count=0)


class TestContactInfoPath:
    def test_formats_the_public_id_into_the_path(self) -> None:
        assert contact_info_path("jamie-fake-rivera-1a2b3c4d") == (
            "/voyager/api/identity/profiles/jamie-fake-rivera-1a2b3c4d/profileContactInfo"
        )

    def test_empty_public_id_refused(self) -> None:
        with pytest.raises(ValueError, match="public_id"):
            contact_info_path("")


class TestProfileQuery:
    def test_shape(self) -> None:
        query = profile_query("jamie-fake-rivera-1a2b3c4d")
        assert query["q"] == "memberIdentity"
        assert query["memberIdentity"] == "jamie-fake-rivera-1a2b3c4d"
        assert query["decorationId"]

    def test_empty_public_id_refused(self) -> None:
        with pytest.raises(ValueError, match="public_id"):
            profile_query("")


class TestConversationsQuery:
    def test_shape(self) -> None:
        query = conversations_query(count=20)
        assert query["count"] == "20"
        assert query["decorationId"]

    def test_non_positive_count_refused(self) -> None:
        with pytest.raises(ValueError, match="count"):
            conversations_query(count=0)


# --- the fetch-helper interface -------------------------------------------------


def test_voyager_request_is_a_plain_value_type() -> None:
    request = VoyagerRequest(path="/voyager/api/foo", query={"a": "b"}, headers={"x": "y"})
    assert request.path == "/voyager/api/foo"
    assert request.query == {"a": "b"}


def test_voyager_response_carries_status_body_and_final_url() -> None:
    response = VoyagerResponse(
        status=200, body="{}", final_url="https://www.linkedin.com/voyager/api/foo"
    )
    assert response.status == 200
    assert response.body == "{}"


def test_an_async_callable_satisfies_voyagerfetch() -> None:
    """The protocol P2-01's provider implements: an async callable, request in, response out.

    This is the "small follow-up" wiring point: P2-01 needs a class or
    function whose ``__call__``/definition matches this shape. Nothing else
    about its implementation matters to this module.
    """

    class FakeProvider:
        async def __call__(self, request: VoyagerRequest) -> VoyagerResponse:
            return VoyagerResponse(status=200, body="{}", final_url="https://example.test")

    provider = FakeProvider()
    assert isinstance(provider, VoyagerFetch)

    class NotAProvider:
        pass

    assert not isinstance(NotAProvider(), VoyagerFetch)


# --- connections page -------------------------------------------------------------


class TestParseConnectionsPage:
    def test_fixture(self) -> None:
        result = parse_connections_page(_body("connections_page.json"))
        assert isinstance(result, ConnectionsPageResult)
        assert result.start == 0
        assert result.count == 40
        assert result.total == 2
        assert len(result.connections) == 2

        first, second = result.connections
        assert first.urn == "urn:li:fsd_profile:ACoAAFAKE0000001"
        assert first.public_id == "jamie-fake-rivera-1a2b3c4d"
        assert first.first_name == "Jamie"
        assert first.last_name == "Rivera"
        assert first.headline == "Product designer at Fictional Robotics Co"
        assert first.connected_at == datetime.fromtimestamp(1690000000000 / 1000, tz=UTC)

        assert second.public_id == "alex-fake-chen-5e6f7g8h"
        assert second.connected_at == datetime.fromtimestamp(1691000000000 / 1000, tz=UTC)

    def test_empty_page_is_a_valid_shape_not_route_changed(self) -> None:
        result = parse_connections_page(_body("connections_page_empty.json"))
        assert result.connections == ()
        assert result.start == 40
        assert result.total == 2

    def test_missing_created_at_leaves_connected_at_none(self) -> None:
        body = json.dumps(
            {
                "data": {
                    "elements": [{"*connectedMemberResolutionResult": "urn:li:fsd_profile:X"}],
                    "paging": {"start": 0, "count": 40, "total": 1},
                },
                "included": [
                    {
                        "entityUrn": "urn:li:fsd_profile:X",
                        "publicIdentifier": "x",
                        "firstName": "X",
                        "lastName": "Y",
                    }
                ],
            }
        )
        result = parse_connections_page(body)
        assert result.connections[0].connected_at is None
        assert result.connections[0].headline is None

    # --- malformed input: every one of these must raise RouteChanged, never KeyError ---

    def test_empty_response_body(self) -> None:
        with pytest.raises(RouteChanged) as exc:
            parse_connections_page("")
        assert exc.value.endpoint == CONNECTIONS_ENDPOINT

    def test_not_json_at_all(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page("<html>please sign in</html>")

    def test_valid_json_but_a_completely_different_document(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(json.dumps({"error": "not found", "status": 404}))

    def test_top_level_is_a_list_not_an_object(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(json.dumps([1, 2, 3]))

    def test_missing_data_key(self) -> None:
        with pytest.raises(RouteChanged, match="data"):
            parse_connections_page(json.dumps({"included": []}))

    def test_data_is_null(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(json.dumps({"data": None, "included": []}))

    def test_missing_included_key(self) -> None:
        with pytest.raises(RouteChanged, match="included"):
            parse_connections_page(
                json.dumps(
                    {"data": {"elements": [], "paging": {"start": 0, "count": 40, "total": 0}}}
                )
            )

    def test_elements_is_a_string_not_a_list(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {
                            "elements": "nope",
                            "paging": {"start": 0, "count": 40, "total": 0},
                        },
                        "included": [],
                    }
                )
            )

    def test_missing_paging_key(self) -> None:
        with pytest.raises(RouteChanged, match="paging"):
            parse_connections_page(json.dumps({"data": {"elements": []}, "included": []}))

    def test_paging_total_missing(self) -> None:
        with pytest.raises(RouteChanged, match="total"):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {"elements": [], "paging": {"start": 0, "count": 40}},
                        "included": [],
                    }
                )
            )

    def test_paging_count_is_a_bool_not_an_int(self) -> None:
        # bool is a subclass of int in Python; this must not silently pass as 1/0.
        with pytest.raises(RouteChanged):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {"elements": [], "paging": {"start": 0, "count": True, "total": 0}},
                        "included": [],
                    }
                )
            )

    def test_element_missing_the_resolution_reference(self) -> None:
        with pytest.raises(RouteChanged, match="connectedMemberResolutionResult"):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [{"createdAt": 1}],
                            "paging": {"start": 0, "count": 40, "total": 1},
                        },
                        "included": [],
                    }
                )
            )

    def test_element_references_an_entity_not_in_included(self) -> None:
        with pytest.raises(RouteChanged, match="urn:li:fsd_profile:MISSING"):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {"*connectedMemberResolutionResult": "urn:li:fsd_profile:MISSING"}
                            ],
                            "paging": {"start": 0, "count": 40, "total": 1},
                        },
                        "included": [],
                    }
                )
            )

    def test_included_entity_missing_public_identifier(self) -> None:
        with pytest.raises(RouteChanged, match="publicIdentifier"):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {"*connectedMemberResolutionResult": "urn:li:fsd_profile:X"}
                            ],
                            "paging": {"start": 0, "count": 40, "total": 1},
                        },
                        "included": [
                            {"entityUrn": "urn:li:fsd_profile:X", "firstName": "X", "lastName": "Y"}
                        ],
                    }
                )
            )

    def test_included_entity_first_name_is_null(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {"*connectedMemberResolutionResult": "urn:li:fsd_profile:X"}
                            ],
                            "paging": {"start": 0, "count": 40, "total": 1},
                        },
                        "included": [
                            {
                                "entityUrn": "urn:li:fsd_profile:X",
                                "publicIdentifier": "x",
                                "firstName": None,
                                "lastName": "Y",
                            }
                        ],
                    }
                )
            )

    def test_included_entry_is_a_string_not_an_object(self) -> None:
        with pytest.raises(RouteChanged):
            parse_connections_page(
                json.dumps(
                    {
                        "data": {"elements": [], "paging": {"start": 0, "count": 40, "total": 0}},
                        "included": ["not an object"],
                    }
                )
            )


# --- contact info -------------------------------------------------------------


class TestParseContactInfo:
    def test_fixture(self) -> None:
        info = parse_contact_info(_body("contact_info.json"))
        assert isinstance(info, ContactInfo)
        assert info.email == "jamie.fake.rivera@example-mail.test"
        assert info.phones == ("+1-555-0101",)
        assert info.websites == ("https://jamie-fake-rivera.example.test",)
        assert info.twitter_handles == ("jamiefakerivera",)

    def test_minimal_fixture_with_nothing_shared(self) -> None:
        info = parse_contact_info(_body("contact_info_minimal.json"))
        assert info.email is None
        assert info.phones == ()
        assert info.websites == ()
        assert info.twitter_handles == ()

    def test_empty_response_body(self) -> None:
        with pytest.raises(RouteChanged) as exc:
            parse_contact_info("")
        assert exc.value.endpoint == CONTACT_INFO_ENDPOINT

    def test_not_json_at_all(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info("<html>please sign in</html>")

    def test_valid_json_but_a_completely_different_document(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info(json.dumps(["a", "list", "not", "an", "object"]))

    def test_email_is_a_number_not_a_string(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info(json.dumps({"emailAddress": 12345}))

    def test_phone_numbers_is_a_string_not_a_list(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info(json.dumps({"phoneNumbers": "+1-555-0101"}))

    def test_phone_numbers_entry_missing_number_key(self) -> None:
        with pytest.raises(RouteChanged, match="number"):
            parse_contact_info(json.dumps({"phoneNumbers": [{"type": "MOBILE"}]}))

    def test_phone_numbers_entry_number_is_wrong_type(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info(json.dumps({"phoneNumbers": [{"number": 5550101}]}))

    def test_phone_numbers_entry_is_a_string_not_an_object(self) -> None:
        with pytest.raises(RouteChanged):
            parse_contact_info(json.dumps({"phoneNumbers": ["+1-555-0101"]}))


# --- profile details -------------------------------------------------------------


class TestParseProfileDetails:
    def test_fixture(self) -> None:
        details = parse_profile_details(_body("profile_details.json"))
        assert isinstance(details, ProfileDetails)
        assert details.urn == "urn:li:fsd_profile:ACoAAFAKE0000001"
        assert details.public_id == "jamie-fake-rivera-1a2b3c4d"
        assert details.first_name == "Jamie"
        assert details.last_name == "Rivera"
        assert details.location == "Faketown, State of Example"

        assert len(details.positions) == 2
        current, prior = details.positions
        assert current.title == "Product Designer"
        assert current.company == "Fictional Robotics Co"
        assert current.start_year == 2022
        assert current.start_month == 3
        assert current.end_year is None
        assert current.end_month is None

        assert prior.company == "Prior Example Studio"
        assert prior.end_year == 2022
        assert prior.end_month == 2

        assert len(details.education) == 1
        school = details.education[0]
        assert school.school == "Fictional State University"
        assert school.degree == "B.A."
        assert school.field_of_study == "Design"
        assert school.start_year == 2015
        assert school.end_year == 2019

    def test_unrecognized_included_type_is_ignored_not_rejected(self) -> None:
        # The fixture's Skill entity has no dateRange at all; if this parser
        # tried to read positions/education out of it the way it does a
        # Position or Education entity, it would raise. It must not even try.
        details = parse_profile_details(_body("profile_details.json"))
        assert all(p.title != "Prototyping" for p in details.positions)

    def test_empty_response_body(self) -> None:
        with pytest.raises(RouteChanged) as exc:
            parse_profile_details("")
        assert exc.value.endpoint == PROFILE_ENDPOINT

    def test_valid_json_but_a_completely_different_document(self) -> None:
        with pytest.raises(RouteChanged):
            parse_profile_details(json.dumps({"status": 404, "message": "not found"}))

    def test_missing_data_key(self) -> None:
        with pytest.raises(RouteChanged, match="data"):
            parse_profile_details(json.dumps({"included": []}))

    def test_data_missing_entity_urn(self) -> None:
        with pytest.raises(RouteChanged, match="entityUrn"):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [],
                    }
                )
            )

    def test_included_entity_missing_type(self) -> None:
        with pytest.raises(RouteChanged, match="\\$type"):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "entityUrn": "urn:li:fsd_profile:X",
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [{"title": "Something"}],
                    }
                )
            )

    def test_position_missing_title(self) -> None:
        with pytest.raises(RouteChanged, match="title"):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "entityUrn": "urn:li:fsd_profile:X",
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [
                            {
                                "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                                "companyName": "Acme",
                                "dateRange": {"start": {"year": 2020}},
                            }
                        ],
                    }
                )
            )

    def test_position_missing_date_range(self) -> None:
        with pytest.raises(RouteChanged, match="dateRange"):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "entityUrn": "urn:li:fsd_profile:X",
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [
                            {
                                "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                                "title": "Engineer",
                                "companyName": "Acme",
                            }
                        ],
                    }
                )
            )

    def test_date_range_start_is_a_list_not_an_object(self) -> None:
        with pytest.raises(RouteChanged):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "entityUrn": "urn:li:fsd_profile:X",
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [
                            {
                                "$type": "com.linkedin.voyager.dash.identity.profile.Position",
                                "title": "Engineer",
                                "companyName": "Acme",
                                "dateRange": {"start": [2020, 1]},
                            }
                        ],
                    }
                )
            )

    def test_education_missing_school_name(self) -> None:
        with pytest.raises(RouteChanged, match="schoolName"):
            parse_profile_details(
                json.dumps(
                    {
                        "data": {
                            "entityUrn": "urn:li:fsd_profile:X",
                            "publicIdentifier": "x",
                            "firstName": "X",
                            "lastName": "Y",
                        },
                        "included": [
                            {
                                "$type": "com.linkedin.voyager.dash.identity.profile.Education",
                                "dateRange": {},
                            }
                        ],
                    }
                )
            )


# --- conversations page -------------------------------------------------------------


class TestParseConversationsPage:
    def test_fixture(self) -> None:
        result = parse_conversations_page(_body("conversations_page.json"))
        assert isinstance(result, ConversationsPageResult)
        assert result.start == 0
        assert result.count == 20
        assert result.total == 2
        assert len(result.conversations) == 2

        one_to_one, group = result.conversations
        assert one_to_one.urn == "urn:li:fsd_conversation:2-FAKECONVO0001"
        assert one_to_one.unread is False
        assert len(one_to_one.participants) == 1
        assert one_to_one.participants[0].first_name == "Jamie"
        assert one_to_one.last_message_text == "Great catching up last week!"
        assert one_to_one.last_message_sender_urn == "urn:li:fsd_profile:ACoAAFAKE0000001"
        assert one_to_one.last_activity_at == datetime.fromtimestamp(1695000000000 / 1000, tz=UTC)

        assert group.unread is True
        assert len(group.participants) == 2
        assert group.last_message_text is None
        assert group.last_message_sender_urn is None

    def test_empty_response_body(self) -> None:
        with pytest.raises(RouteChanged) as exc:
            parse_conversations_page("")
        assert exc.value.endpoint == CONVERSATIONS_ENDPOINT

    def test_valid_json_but_a_completely_different_document(self) -> None:
        with pytest.raises(RouteChanged):
            parse_conversations_page(json.dumps(42))

    def test_missing_data_key(self) -> None:
        with pytest.raises(RouteChanged, match="data"):
            parse_conversations_page(json.dumps({}))

    def test_element_missing_entity_urn(self) -> None:
        with pytest.raises(RouteChanged, match="entityUrn"):
            parse_conversations_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {"lastActivityAt": 1, "unread": False, "participants": []}
                            ],
                            "paging": {"start": 0, "count": 20, "total": 1},
                        }
                    }
                )
            )

    def test_unread_is_a_string_not_a_bool(self) -> None:
        with pytest.raises(RouteChanged):
            parse_conversations_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {
                                    "entityUrn": "urn:li:fsd_conversation:1",
                                    "lastActivityAt": 1695000000000,
                                    "unread": "false",
                                    "participants": [],
                                }
                            ],
                            "paging": {"start": 0, "count": 20, "total": 1},
                        }
                    }
                )
            )

    def test_participants_entry_missing_first_name(self) -> None:
        with pytest.raises(RouteChanged, match="firstName"):
            parse_conversations_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {
                                    "entityUrn": "urn:li:fsd_conversation:1",
                                    "lastActivityAt": 1695000000000,
                                    "unread": False,
                                    "participants": [
                                        {"entityUrn": "urn:li:fsd_profile:X", "lastName": "Y"}
                                    ],
                                }
                            ],
                            "paging": {"start": 0, "count": 20, "total": 1},
                        }
                    }
                )
            )

    def test_last_message_body_missing_text(self) -> None:
        with pytest.raises(RouteChanged, match="text"):
            parse_conversations_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {
                                    "entityUrn": "urn:li:fsd_conversation:1",
                                    "lastActivityAt": 1695000000000,
                                    "unread": False,
                                    "participants": [],
                                    "lastMessage": {"body": {}},
                                }
                            ],
                            "paging": {"start": 0, "count": 20, "total": 1},
                        }
                    }
                )
            )

    def test_last_activity_at_out_of_range_for_a_timestamp(self) -> None:
        with pytest.raises(RouteChanged):
            parse_conversations_page(
                json.dumps(
                    {
                        "data": {
                            "elements": [
                                {
                                    "entityUrn": "urn:li:fsd_conversation:1",
                                    "lastActivityAt": 99999999999999999999,
                                    "unread": False,
                                    "participants": [],
                                }
                            ],
                            "paging": {"start": 0, "count": 20, "total": 1},
                        }
                    }
                )
            )
