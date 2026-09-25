"""netkeeper.linkedin.flagship: the connections list as flagship-web serves it (#187).

The payloads are :mod:`flagship_pages`' hand-built ones, with :mod:`voyager_pages`'
invented people. The rule under test throughout: a page is read whole or refused
whole (``RouteChanged``), so a payload the parser half understands can never put
one person's name on another person's URN.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from flagship_pages import (
    OTHER_PAGER_ID,
    CardOptions,
    Role,
    Website,
    _pagination_request_value,
    contact_info_payload,
    document_html,
    pagination_payload,
    pagination_request,
    profile_payload,
    screen_payload,
)
from voyager_pages import PEOPLE, Person

from netkeeper.linkedin import flagship
from netkeeper.linkedin.flagship import (
    ConnectionsChunk,
    parse_connections_chunk,
    parse_pagination_request,
    rehydration_payload,
    split_display_name,
)
from netkeeper.linkedin.flight import element_props, parse_flight
from netkeeper.linkedin.voyager import RouteChanged

E = "test/flagship"


def _chunk(body: bytes, start: int = 0) -> ConnectionsChunk:
    return parse_connections_chunk(body, endpoint=E, expected_start=start)


# --- the constants the capture pinned ---------------------------------------------------


def test_the_captured_constants_are_pinned() -> None:
    """Safety-relevant literals, written out (CLAUDE.md): a drift here is a new capture."""
    assert flagship.LINKEDIN_ORIGIN == "https://www.linkedin.com"
    assert flagship.CONNECTIONS_PAGE_PATH == "/mynetwork/invite-connect/connections/"
    assert flagship.CONNECTIONS_SCREEN_PATH == "/flagship-web/mynetwork/invite-connect/connections"
    assert flagship.PAGINATION_PATH == "/flagship-web/rsc-action/actions/pagination"
    assert flagship.NAVIGATION_PATH == "/flagship-web/rsc-action/actions/navigation"
    assert flagship.CONNECTIONS_PAGER_ID == "com.linkedin.sdui.pagers.mynetwork.connectionsList"
    assert flagship.SORT_NEWEST_FIRST == "sortByRecentlyAdded"
    assert flagship.TOTAL_STATE_ID == "totalConnectionsCount"
    assert flagship.NAME_STATE_ID == "profile_name_loading_state"
    assert flagship.HEADLINE_STATE_ID == "profile_headline_loading_state"
    assert flagship.CARD_KEY_PREFIX == "ConnectionCard_"
    assert flagship.FULL_PAGE == 10
    assert flagship.URN_PREFIX == "urn:li:fsd_profile:"


# --- a page of cards ------------------------------------------------------------------------


def test_the_first_screen_reads_every_card_whole() -> None:
    chunk = _chunk(screen_payload(PEOPLE, total=213))
    assert chunk.total == 213 and chunk.start == 0 and chunk.next_start == 10
    assert not chunk.ends_list
    assert [card.urn for card in chunk.cards] == [person.urn for person in PEOPLE]
    first = chunk.cards[0]
    assert (first.public_id, first.first_name, first.last_name) == (
        PEOPLE[0].slug,
        "Priya",
        "Okafor",
    )
    assert first.headline == "Data engineer at Fictional Robotics Co"
    assert first.connected_on == date(2023, 11, 14)
    assert first.connected_at is None
    no_headline = chunk.cards[2]
    assert no_headline.headline is None and no_headline.first_name == "Hana"
    no_date = chunk.cards[7]
    assert no_date.connected_on is None


def test_a_pagination_page_carries_its_start_and_no_total() -> None:
    chunk = _chunk(pagination_payload(PEOPLE[:10], start=40, next_start=50), start=40)
    assert (chunk.start, chunk.next_start, chunk.total) == (40, 50, None)
    assert len(chunk.cards) == 10


@pytest.mark.parametrize(
    ("cards", "next_start", "ends"),
    [
        (0, None, True),  # an empty answer is the end
        (3, None, True),  # a short answer asking for nothing is the end
        (10, None, False),  # a full answer asking for nothing proves nothing
        (3, 43, False),  # a short answer that asks for more is under-filled, not the end
        (10, 50, False),
    ],
)
def test_what_proves_the_end_of_the_list(cards: int, next_start: int | None, ends: bool) -> None:
    chunk = _chunk(pagination_payload(PEOPLE[:cards], start=40, next_start=next_start), start=40)
    assert chunk.ends_list is ends


def test_a_card_keyed_for_another_page_is_refused() -> None:
    body = pagination_payload(PEOPLE[:10], start=40, next_start=50)
    with pytest.raises(RouteChanged, match="keyed for a different page"):
        _chunk(body, start=30)


@pytest.mark.parametrize(
    ("options", "detail"),
    [
        (CardOptions(other_profile_id="ACoAAFAKE9999999"), "2 profile identities, not one"),
        (CardOptions(key_slug="someone-else-fake-0000"), "the slug does not match"),
        (CardOptions(key_start=20), "keyed for a different page"),
        (CardOptions(drop_name=True), "0 names, not one"),
        (CardOptions(connected_on="Connected on Smarch 3, 2024"), "connected-on date"),
        (CardOptions(connected_on="Connected on February 30, 2024"), "does not exist"),
        (CardOptions(name="Priya\nOkafor"), "control character"),
        (CardOptions(name="   "), "empty or too long"),
        (CardOptions(name="x" * 201), "empty or too long"),
    ],
)
def test_one_spoiled_card_refuses_the_whole_page(options: CardOptions, detail: str) -> None:
    """The fifth card is spoiled; the four before it are fine, and still nothing is read."""
    body = screen_payload(PEOPLE, total=10, card_options={4: options})
    with pytest.raises(RouteChanged) as caught:
        _chunk(body)
    assert detail in caught.value.detail
    assert "card 4" in caught.value.detail


def test_a_profile_id_that_is_not_the_captured_shape_is_refused() -> None:
    odd = Person(101, "Priya", "Okafor", None, urn_prefix="ACo/../x")
    with pytest.raises(RouteChanged, match="profile id"):
        _chunk(screen_payload([odd], total=1))


def test_two_cards_for_one_profile_are_refused() -> None:
    twin = Person(101, "Priya", "Okafor", None, public_id="another-fake-slug")
    with pytest.raises(RouteChanged, match="two cards for one profile id"):
        _chunk(screen_payload([PEOPLE[0], twin], total=2))


def test_an_answer_that_mentions_cards_it_cannot_read_is_not_the_end() -> None:
    """An unreadable page must not pass for an empty one: that would end the list."""
    body = pagination_payload([], start=40, next_start=None)
    spoiled = body.replace(b'"horizontal"', b'"vieweeProfileId"')
    with pytest.raises(RouteChanged, match="mentions cards"):
        _chunk(spoiled, start=40)
    renamed = screen_payload(PEOPLE[:2], total=2).replace(b"ConnectionCard_", b"PersonCard_")
    with pytest.raises(RouteChanged, match="mentions cards"):
        _chunk(renamed)


def test_a_next_page_that_does_not_move_forward_is_refused() -> None:
    body = pagination_payload(PEOPLE[:10], start=40, next_start=40)
    with pytest.raises(RouteChanged, match="does not move past"):
        _chunk(body, start=40)


def test_a_next_page_that_skips_people_is_refused() -> None:
    """Ten cards asking for the page twenty on: ten people nobody would be shown."""
    body = pagination_payload(PEOPLE[:10], start=40, next_start=60)
    with pytest.raises(RouteChanged, match="skips past"):
        _chunk(body, start=40)
    under_filled = pagination_payload(PEOPLE[:9], start=40, next_start=50)
    assert _chunk(under_filled, start=40).next_start == 50  # nine of ten, then on: fine


def test_two_next_pages_are_refused() -> None:
    body = pagination_payload(PEOPLE[:2], start=40, next_start=42)
    another = json.dumps(json.dumps(_pagination_request_value(52))).encode()
    doubled = body.replace(b'"horizontal"]', b'"horizontal",' + another + b"]")
    with pytest.raises(RouteChanged, match="more than one next page"):
        _chunk(doubled, start=40)


def test_two_totals_are_refused_and_a_negative_one_too() -> None:
    body = screen_payload(PEOPLE[:2], total=5)
    doubled = body.replace(
        b'"isPartialPage"',
        b'"extra":{"key":{"key":{"value":{"id":"totalConnectionsCount"}}},"value":{"intValue":6}},"isPartialPage"',
    )
    with pytest.raises(RouteChanged, match="more than one total"):
        _chunk(doubled)
    negative = screen_payload(PEOPLE[:2], total=-1)
    with pytest.raises(RouteChanged, match="not a count"):
        _chunk(negative)


# --- names ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "split"),
    [
        ("Jamie Rivera", ("Jamie", "Rivera")),
        ("Jamie  Q.   Rivera, PMP", ("Jamie", "Q. Rivera, PMP")),
        ("Cher", ("Cher", "")),
        (" Ana Lucia  de Souza ", ("Ana", "Lucia de Souza")),
    ],
)
def test_a_display_name_splits_at_the_first_space(name: str, split: tuple[str, str]) -> None:
    assert split_display_name(name, endpoint=E, where="card 0") == split


@pytest.mark.parametrize(
    "name", ["", "Ana\u2028Rivera", "Ana\x00", "Ana\x85Rivera", "Ana\tRivera", "Ana\nRivera"]
)
def test_a_name_that_is_not_a_name_is_refused(name: str) -> None:
    with pytest.raises(RouteChanged):
        split_display_name(name, endpoint=E, where="card 0")


# --- the first screen inside a full page load -------------------------------------------


def test_the_first_screen_is_read_out_of_the_document() -> None:
    payload = screen_payload(PEOPLE, total=10)
    extracted = rehydration_payload(document_html(payload, chunks=4))
    assert extracted == payload
    assert rehydration_payload("<html><body>Sign in</body></html>") is None


@pytest.mark.parametrize(
    "script",
    [
        "window.somethingElse = [];",
        "window.__como_rehydration__ = {not json};",
        'window.__como_rehydration__ = {"a": 1};',
        "window.__como_rehydration__ = [1, 2];",
    ],
)
def test_a_rehydration_script_of_another_shape_is_route_changed(script: str) -> None:
    document = f'<html><script id="rehydrate-data">{script}</script></html>'
    with pytest.raises(RouteChanged):
        rehydration_payload(document)


# --- the page's own request ----------------------------------------------------------


def test_the_pagination_request_is_read_for_its_start_and_sort() -> None:
    request = parse_pagination_request(pagination_request(30))
    assert request is not None and (request.start_index, request.sort) == (
        30,
        "sortByRecentlyAdded",
    )
    other = parse_pagination_request(pagination_request(30, sort="sortByFirstName"))
    assert other is not None and other.sort == "sortByFirstName"


def test_another_pagers_request_is_not_ours() -> None:
    assert parse_pagination_request(pagination_request(0, pager=OTHER_PAGER_ID)) is None


@pytest.mark.parametrize(
    "body",
    [
        None,
        "not json",
        "[1]",
        json.dumps({"pagerId": flagship.CONNECTIONS_PAGER_ID}),
        json.dumps(
            {
                "pagerId": flagship.CONNECTIONS_PAGER_ID,
                "clientArguments": {"payload": {"startIndex": True}},
            }
        ),
        json.dumps(
            {
                "pagerId": flagship.CONNECTIONS_PAGER_ID,
                "clientArguments": {"payload": {"startIndex": -10}},
            }
        ),
    ],
)
def test_a_connections_request_without_a_start_is_route_changed(body: str | None) -> None:
    with pytest.raises(RouteChanged):
        parse_pagination_request(body)


# --- the enrichment lane's fixtures: the grammar and the anchors ---------------------------


def test_the_profile_fixture_carries_the_anchors_the_shape_note_names() -> None:
    body = profile_payload(
        PEOPLE[0],
        location="Springfield, Example State",
        roles=[
            Role(
                "Data engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present · 3 yrs"
            ),
            Role("Analyst", "Acme Testing Group", None, "Jan 2019 - Jul 2021 · 2 yrs 7 mos"),
        ],
    )
    payload = parse_flight(body, endpoint=E)
    nodes = list(payload.nodes(endpoint=E))
    views = {
        props["viewTrackingSpecs"]["viewName"]  # type: ignore[index]
        for node in nodes
        if (props := element_props(node)) is not None
        and isinstance(props.get("viewTrackingSpecs"), dict)
    }
    assert {"profile-top-card", "profile-card-experience"} <= views
    screens = [
        node["value"]["content"]["screen"]
        for node in nodes
        if isinstance(node, dict) and node.get("$type") == "proto.sdui.actions.core.Navigate"
    ]
    (contact,) = [s for s in screens if s["screenId"] == flagship.CONTACT_DETAILS_SCREEN_ID]
    assert set(contact["requestedArguments"]["payload"]) == {
        "vanityName",
        "givenName",
        "familyName",
        "isVanityNameResolved",
    }
    assert "Contact info" in nodes and "Experience" in nodes


def test_the_contact_info_fixture_carries_one_section_per_kind() -> None:
    body = contact_info_payload(
        PEOPLE[1],
        emails=["mateo.lindqvist@example.test"],
        websites=[Website("https://fake-portfolio.example.test", "(Portfolio)")],
        phones=["+1 555 0100"],
        connected_since="Oct 3, 2023",
    )
    payload = parse_flight(body, endpoint=E)
    nodes = list(payload.walk(payload.rows["0"], follow=True, endpoint=E))
    views = {
        props["viewTrackingSpecs"]["viewName"]  # type: ignore[index]
        for node in nodes
        if (props := element_props(node)) is not None
        and isinstance(props.get("viewTrackingSpecs"), dict)
    }
    assert views == {"contact-your-profile", "contact-website", "contact-phone", "contact-email"}
    assert "mailto:mateo.lindqvist@example.test" in nodes
    assert "Connected since" in nodes
