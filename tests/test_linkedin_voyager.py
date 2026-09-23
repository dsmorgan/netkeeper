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

import copy
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
# ADR 0005's rule for this module is checked by the shared sweep in
# tests/boundary.py, which walks every module under netkeeper/linkedin/ in its
# own subprocess. The list it reads lives there; this module used to carry a copy.


def _body(name: str) -> str:
    return (FIXTURES / name).read_text()


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

    def test_empty_string(self) -> None:
        assert strip_jsessionid("") == ""

    def test_two_quote_characters_is_the_empty_value_quoted(self) -> None:
        # '""' is a quoted empty string, not a lone quote: it strips to "".
        assert strip_jsessionid('""') == ""

    def test_only_the_outer_quote_pair_is_stripped(self) -> None:
        # Deliberate: this strips exactly one layer of quoting (the cookie's
        # own), not every quote character in the value. A cookie value that
        # legitimately contains a quote keeps it.
        assert strip_jsessionid('"ajax:12"34"') == 'ajax:12"34'


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


# =============================================================================
# Systematic guard-coverage sweep (review on #147)
#
# The hand-written malformed-input tests above each name one field and one
# way it can go wrong; they read well, but nothing stops a future edit from
# adding a field, reading it with a bare subscript, and joining the silently
# untested set — that is exactly what happened to five guards a mutation
# sample caught (see the module-level comment below the sweep's fixtures).
#
# This sweep instead walks every dict key in each parser's own happy-path
# fixture, at every depth, and for each one generates two mutated copies:
# the key removed, and the key's value replaced with something of an
# incompatible JSON type. It asserts what "guarded" means for every one of
# them, with no field left for a human to remember to cover:
#
#   removed   -> RouteChanged, unless the field is genuinely optional or the
#                parser never reads it at all
#   wrong type -> RouteChanged, unless the parser never reads the field at
#                all (an optional field still validates its shape once it's
#                present, so "optional" is not an exemption here)
#
# ``_Sweep.optional``/``.unused`` are the only escape hatches, and they are
# closed lists of exact paths, not patterns: a path this sweep discovers in
# the fixture that is in neither set defaults to "required", so a new field
# added to a fixture without a matching guard (or an explicit decision that
# it's optional/unused) fails this test the moment it's added, rather than
# extending an untested set nobody notices growing.
# =============================================================================

PathStep = str | int
FieldPath = tuple[PathStep, ...]


def _walk_dict_paths(obj: object, prefix: FieldPath = ()) -> list[FieldPath]:
    """Every path to a dict key in ``obj``, at any depth, including inside list items.

    List indices themselves are never yielded as removable/mutable "keys" —
    only object keys are, which is what "missing field" and "wrong-shaped
    field" mean for a JSON document. Recursing into a list still finds every
    dict key inside its items.
    """
    paths: list[FieldPath] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = (*prefix, key)
            paths.append(path)
            paths.extend(_walk_dict_paths(value, path))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            paths.extend(_walk_dict_paths(value, (*prefix, index)))
    return paths


def _without_path(obj: object, path: FieldPath) -> object:
    """A deep copy of ``obj`` with the dict key at ``path`` deleted."""
    mutated: Any = copy.deepcopy(obj)
    container = mutated
    for step in path[:-1]:
        container = container[step]
    del container[path[-1]]
    return mutated


def _mismatched_value(original: object) -> object:
    """Something of a JSON type incompatible with ``original``'s.

    Order matters: ``bool`` is checked before ``int`` because
    ``isinstance(True, int)`` is ``True`` in Python, and a bool swapped for
    another bool-ish value would not actually exercise the type guard.
    """
    if isinstance(original, bool):
        return "not-a-bool"
    if isinstance(original, dict):
        return "not-a-dict"
    if isinstance(original, list):
        return {"not": "a-list"}
    if isinstance(original, str):
        return ["not", "a-string"]
    if isinstance(original, int):
        return ["not", "an-int"]
    if original is None:
        # A field the fixture already sets to null (e.g. an absent optional
        # object, spelled explicitly rather than omitted). There's no type
        # to avoid matching, so use a JSON type this module never checks for
        # (float) -- guaranteed to fail every ``_expect`` in voyager.py.
        return 12345.6789
    raise AssertionError(f"no mismatched-type case in this sweep for {type(original)}")


def _with_wrong_type(obj: object, path: FieldPath) -> object:
    """A deep copy of ``obj`` with the value at ``path`` replaced by an incompatible type."""
    mutated: Any = copy.deepcopy(obj)
    container = mutated
    for step in path[:-1]:
        container = container[step]
    container[path[-1]] = _mismatched_value(container[path[-1]])
    return mutated


def _path_id(path: FieldPath) -> str:
    return "".join(f"[{step}]" if isinstance(step, int) else f".{step}" for step in path).lstrip(
        "."
    )


@dataclass(frozen=True)
class _Sweep:
    """One parser, its happy-path fixture, and the two allowlists that make the sweep provable.

    ``optional``: paths the parser reads through ``_optional_field`` (or
    equivalent) — absent is fine, but present-and-wrong-shaped still raises.
    ``unused``: paths the parser never reads at all — LinkedIn sends them,
    nothing here looks at them, so neither removing them nor corrupting
    their type can raise. Every path in ``unused`` or ``optional`` was
    confirmed against the current parser before being added here; a path
    that is not is "required" by default, which is the point.
    """

    name: str
    fixture: str
    parser: Callable[[str], object]
    optional: frozenset[FieldPath]
    unused: frozenset[FieldPath]

    def load(self) -> object:
        return json.loads(_body(self.fixture))


_CONNECTIONS_SWEEP = _Sweep(
    name="connections",
    fixture="connections_page.json",
    parser=parse_connections_page,
    optional=frozenset(
        {
            ("data", "elements", 0, "createdAt"),
            ("data", "elements", 1, "createdAt"),
            ("included", 0, "headline"),
            ("included", 1, "headline"),
        }
    ),
    # $type rides along on every included entity but this parser only ever
    # reads entityUrn/publicIdentifier/firstName/lastName/headline off one.
    unused=frozenset(
        {
            ("included", 0, "$type"),
            ("included", 1, "$type"),
        }
    ),
)

_CONTACT_INFO_SWEEP = _Sweep(
    name="contact_info",
    fixture="contact_info.json",
    parser=parse_contact_info,
    # The three list fields are each optional as a whole (nobody has to share
    # an email, phone, website, or handle); item_key is required within an
    # item once the list itself is present.
    optional=frozenset(
        {
            ("emailAddress",),
            ("phoneNumbers",),
            ("websites",),
            ("twitterHandles",),
        }
    ),
    # Only item_key is read out of a phoneNumbers/websites entry -- "type"
    # and "category" ride along in the real payload and are never consulted.
    unused=frozenset(
        {
            ("phoneNumbers", 0, "type"),
            ("websites", 0, "category"),
            ("websites", 0, "category", "type"),
        }
    ),
)

_PROFILE_DETAILS_SWEEP = _Sweep(
    name="profile_details",
    fixture="profile_details.json",
    parser=parse_profile_details,
    optional=frozenset(
        {
            ("data", "headline"),
            ("data", "geoLocationName"),
            ("included", 0, "dateRange", "start"),
            ("included", 0, "dateRange", "start", "year"),
            ("included", 0, "dateRange", "start", "month"),
            ("included", 1, "dateRange", "start"),
            ("included", 1, "dateRange", "start", "year"),
            ("included", 1, "dateRange", "start", "month"),
            ("included", 1, "dateRange", "end"),
            ("included", 1, "dateRange", "end", "year"),
            ("included", 1, "dateRange", "end", "month"),
            ("included", 2, "degreeName"),
            ("included", 2, "fieldOfStudy"),
            ("included", 2, "dateRange", "start"),
            ("included", 2, "dateRange", "start", "year"),
            ("included", 2, "dateRange", "end"),
            ("included", 2, "dateRange", "end", "year"),
        }
    ),
    # entityUrn on an included Position/Education/Skill entity is never read
    # (only the top-card's own entityUrn, under "data", is); a Skill entity's
    # "name" is ignored entirely, since this parser only extracts positions
    # and education out of "included" (see parse_profile_details's docstring
    # on why an unrecognized $type is skipped, not rejected).
    unused=frozenset(
        {
            ("included", 0, "entityUrn"),
            ("included", 1, "entityUrn"),
            ("included", 2, "entityUrn"),
            ("included", 3, "entityUrn"),
            ("included", 3, "name"),
        }
    ),
)

_CONVERSATIONS_SWEEP = _Sweep(
    name="conversations",
    fixture="conversations_page.json",
    parser=parse_conversations_page,
    optional=frozenset(
        {
            ("data", "elements", 0, "participants", 0, "publicIdentifier"),
            ("data", "elements", 0, "lastMessage"),
            ("data", "elements", 0, "lastMessage", "body"),
            ("data", "elements", 0, "lastMessage", "sender"),
            ("data", "elements", 1, "participants", 0, "publicIdentifier"),
            ("data", "elements", 1, "participants", 1, "publicIdentifier"),
            ("data", "elements", 1, "lastMessage"),
        }
    ),
    # lastMessage carries its own entityUrn in the real payload; this parser
    # only reads lastMessage.body.text and lastMessage.sender.entityUrn.
    unused=frozenset(
        {
            ("data", "elements", 0, "lastMessage", "entityUrn"),
        }
    ),
)

_SWEEPS = (_CONNECTIONS_SWEEP, _CONTACT_INFO_SWEEP, _PROFILE_DETAILS_SWEEP, _CONVERSATIONS_SWEEP)


def _sweep_cases() -> list[tuple[_Sweep, FieldPath]]:
    cases: list[tuple[_Sweep, FieldPath]] = []
    for sweep in _SWEEPS:
        for path in _walk_dict_paths(sweep.load()):
            cases.append((sweep, path))
    return cases


_SWEEP_CASES = _sweep_cases()
_SWEEP_IDS = [f"{sweep.name}:{_path_id(path)}" for sweep, path in _SWEEP_CASES]


@pytest.mark.parametrize(("sweep", "path"), _SWEEP_CASES, ids=_SWEEP_IDS)
def test_every_field_removed_is_route_changed_or_declared_optional(
    sweep: _Sweep, path: FieldPath
) -> None:
    fixture = sweep.load()
    body = json.dumps(_without_path(fixture, path))
    if path in sweep.optional or path in sweep.unused:
        sweep.parser(body)  # must not raise
    else:
        with pytest.raises(RouteChanged):
            sweep.parser(body)


@pytest.mark.parametrize(("sweep", "path"), _SWEEP_CASES, ids=_SWEEP_IDS)
def test_every_field_wrong_type_is_route_changed_unless_declared_unused(
    sweep: _Sweep, path: FieldPath
) -> None:
    fixture = sweep.load()
    body = json.dumps(_with_wrong_type(fixture, path))
    if path in sweep.unused:
        sweep.parser(body)  # must not raise: nothing reads this field
    else:
        # Note: unlike the removal sweep, "optional" is not an exemption
        # here -- a field that's fine to omit is still validated once present.
        with pytest.raises(RouteChanged):
            sweep.parser(body)


def test_every_declared_optional_or_unused_path_actually_exists_in_its_fixture() -> None:
    """Guards the allowlists themselves against drift.

    If a fixture is ever edited and a path in ``optional``/``unused`` stops
    existing, the sweep above simply stops generating a case for it --
    silently shrinking coverage rather than failing. This walks each
    fixture once and confirms every declared path is still there.
    """
    for sweep in _SWEEPS:
        found = set(_walk_dict_paths(sweep.load()))
        stale = (sweep.optional | sweep.unused) - found
        assert stale == set(), f"{sweep.name}: declared paths no longer in the fixture: {stale}"
