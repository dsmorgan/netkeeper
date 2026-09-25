"""netkeeper.linkedin.flagship_profile: a profile and its overlay, from flagship-web (#190).

The payloads are :mod:`flagship_pages`' hand-built ones, with :mod:`voyager_pages`'
invented people. The rules under test: whose profile it is is read from the page's
own payloads and must be one id; the captured shapes are read strictly (a shape that
moved is ``RouteChanged``, never a guess); the shapes the capture never showed fail
soft (a value that does not read is left out).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date

import pytest
from flagship_pages import (
    Role,
    RoleGroup,
    School,
    Website,
    contact_info_payload,
    experience_payload,
    profile_id,
    profile_payload,
)
from voyager_pages import PEOPLE

from netkeeper.linkedin import flagship_profile
from netkeeper.linkedin.flagship_profile import (
    parse_contact_info,
    parse_navigation_request,
    parse_profile,
    parse_profile_urn,
    profile_slug,
    same_slug,
)
from netkeeper.linkedin.flight import parse_flight
from netkeeper.linkedin.voyager import (
    ContactInfo,
    EducationEntry,
    PositionEntry,
    RouteChanged,
)

PRIYA, MATEO, HANA = PEOPLE[0], PEOPLE[1], PEOPLE[2]
ROLES = (
    Role("Staff Data Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present · 3 yrs"),
    Role("Data Engineer", "Placeholder Partners", None, "Jan 2019 - Jul 2021 · 2 yrs 7 mos"),
)
LOCATION = "Faketown, State of Example, Exampleland"


def _profile(**kwargs: object) -> bytes:
    options: dict[str, object] = {"location": LOCATION, "roles": ROLES}
    options.update(kwargs)
    return profile_payload(PRIYA, **options)  # type: ignore[arg-type]


# --- constants the shape note pins ---------------------------------------------------------


def test_the_captured_anchors_are_the_shape_notes() -> None:
    assert flagship_profile.PROFILE_PAGE_PREFIX == "/in/"
    assert flagship_profile.PROFILE_SCREEN_PREFIX == "/flagship-web/in/"
    assert flagship_profile.COMPONENT_PATH == "/flagship-web/rsc-action/actions/component"
    assert flagship_profile.TOP_CARD_VIEW == "profile-top-card"
    assert flagship_profile.EXPERIENCE_VIEW == "profile-card-experience"
    assert flagship_profile.CONTACT_INFO_LABEL == "Contact info"
    assert flagship_profile.SECTION_EMAIL == "contact-email"
    assert flagship_profile.SECTION_WEBSITE == "contact-website"
    assert flagship_profile.SECTION_PROFILE == "contact-your-profile"
    assert flagship_profile.SECTION_INSTANT_MESSAGE == "contact-instant-message"
    assert frozenset({"/safety/go", "/redir/redirect"}) == flagship_profile.REDIRECT_PATHS
    assert flagship_profile.REDIRECT_PARAM == "url"


# --- the profile -------------------------------------------------------------------------


def test_a_profile_reads_whole() -> None:
    details = parse_profile(_profile(), slug=PRIYA.slug)
    assert details.urn == PRIYA.urn
    assert details.public_id == PRIYA.slug
    assert (details.first_name, details.last_name) == ("Priya", "Okafor")
    assert details.headline == PRIYA.headline
    assert details.location == LOCATION
    assert details.positions == (
        PositionEntry("Staff Data Engineer", "Fictional Robotics Co", 2021, 8, None, None),
        PositionEntry("Data Engineer", "Placeholder Partners", 2019, 1, 2021, 7),
    )
    assert details.education == ()


@pytest.mark.parametrize("identity", ["message", "bare", "viewee"])
def test_the_members_id_reads_from_each_place_it_can_sit(identity: str) -> None:
    details = parse_profile(_profile(identity=identity), slug=PRIYA.slug)
    assert details.urn == PRIYA.urn


def test_a_profile_that_names_no_id_is_refused() -> None:
    with pytest.raises(RouteChanged, match="0 profile ids"):
        parse_profile(_profile(identity="none"), slug=PRIYA.slug)


def test_other_peoples_ids_beside_their_own_slugs_are_not_the_members() -> None:
    """The "People also viewed" rail carries other ids beside other slugs: ignored."""
    details = parse_profile(_profile(also_viewed=PEOPLE[3:7]), slug=PRIYA.slug)
    assert details.urn == PRIYA.urn


def test_shared_connections_inside_the_top_card_are_not_the_member() -> None:
    """M2 (#193 review): a mutual connection's ``{profileUrn, vanityName: other}`` sits in
    the top card itself. Only an id beside this profile's slug, or beside no slug at all,
    is the member's."""
    details = parse_profile(_profile(mutuals=PEOPLE[3:5]), slug=PRIYA.slug)
    assert details.urn == PRIYA.urn
    with pytest.raises(RouteChanged, match="0 profile ids"):
        parse_profile(_profile(identity="none", mutuals=PEOPLE[3:5]), slug=PRIYA.slug)


def test_the_rail_alone_never_lends_the_profile_an_id() -> None:
    with pytest.raises(RouteChanged, match="0 profile ids"):
        parse_profile(_profile(identity="none", also_viewed=PEOPLE[3:7]), slug=PRIYA.slug)


def test_two_ids_for_this_slug_are_refused() -> None:
    """Somebody else's card on the page claiming this slug under another id."""
    impostor = replace(PEOPLE[4], public_id=PRIYA.slug)
    with pytest.raises(RouteChanged, match="2 profile ids"):
        parse_profile(_profile(also_viewed=[impostor]), slug=PRIYA.slug)


def test_a_page_that_names_another_id_reads_as_that_id() -> None:
    """The parser reports the id the page names; the job and the core compare it."""
    details = parse_profile(_profile(profile_id_override="ACoAAFAKE9999999"), slug=PRIYA.slug)
    assert details.urn == "urn:li:fsd_profile:ACoAAFAKE9999999"


@pytest.mark.parametrize("override", ["not an id", "urn:li:member:12345", "x"])
def test_an_id_not_in_the_captured_shape_is_no_id(override: str) -> None:
    with pytest.raises(RouteChanged, match="0 profile ids"):
        parse_profile(_profile(profile_id_override=override), slug=PRIYA.slug)


def test_the_profile_must_be_the_tabs() -> None:
    """A screen for another slug (a stale answer, a redirect) is refused."""
    with pytest.raises(RouteChanged, match="another profile"):
        parse_profile(_profile(), slug=MATEO.slug)


def test_slugs_compare_as_linkedin_routes_them() -> None:
    details = parse_profile(_profile(), slug=PRIYA.slug.upper())
    assert details.public_id == PRIYA.slug
    assert same_slug("priya-FAKE", "Priya-fake") and same_slug("a%2Db", "a-b")
    assert not same_slug("priya", "priya2")


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"contact_links": 0}, "0 Contact info links"),
        ({"contact_slug": "someone-else-fake"}, "another profile"),
        ({"top_cards": 2}, "2 profile-top-card"),
        ({"top_cards": 0}, "0 profile-top-card"),
    ],
)
def test_a_top_card_that_moved_is_refused(options: dict[str, object], match: str) -> None:
    with pytest.raises(RouteChanged, match=match):
        parse_profile(_profile(**options), slug=PRIYA.slug)


def test_two_copies_of_the_one_contact_info_link_are_one_link() -> None:
    details = parse_profile(_profile(contact_links=2), slug=PRIYA.slug)
    assert details.urn == PRIYA.urn


def test_a_given_name_with_a_control_character_is_refused() -> None:
    person = replace(PRIYA, first="Pri\x07ya")
    with pytest.raises(RouteChanged, match="givenName"):
        parse_profile(profile_payload(person, location=None), slug=person.slug)


def test_a_payload_that_is_not_flight_is_refused() -> None:
    with pytest.raises(RouteChanged):
        parse_profile(b"<html>not a profile</html>", slug=PRIYA.slug)


# --- the top card's text runs --------------------------------------------------------------


def test_no_location_leaves_it_unknown_and_keeps_the_headline() -> None:
    details = parse_profile(_profile(location=None), slug=PRIYA.slug)
    assert details.headline == PRIYA.headline and details.location is None


def test_an_empty_headline_is_unknown() -> None:
    details = parse_profile(profile_payload(HANA, location=LOCATION), slug=HANA.slug)
    assert details.headline is None and details.location == LOCATION


def test_more_runs_than_the_capture_showed_leave_the_location_unknown() -> None:
    """The capture saw short runs (a company, a school) among them; which is which is
    unverified, so the headline is the first run and the location is not guessed."""
    details = parse_profile(
        _profile(extra_top_runs=["Fictional Robotics Co", "Fictional State University"]),
        slug=PRIYA.slug,
    )
    assert details.headline == PRIYA.headline and details.location is None
    no_location = parse_profile(
        _profile(location=None, extra_top_runs=["Fictional Robotics Co"]), slug=PRIYA.slug
    )
    assert no_location.location is None  # never the company the experience card lists


def test_a_top_card_without_its_degree_leaves_both_unknown() -> None:
    body = _profile(degree="Following")
    details = parse_profile(body, slug=PRIYA.slug)
    assert details.headline is None and details.location is None
    assert details.urn == PRIYA.urn  # the identity still reads: it does not need the runs


def test_a_run_with_a_control_character_is_unknown() -> None:
    person = replace(PRIYA, headline="line one\u2028line two")
    details = parse_profile(profile_payload(person, location=LOCATION), slug=person.slug)
    assert details.headline is None and details.location == LOCATION


# --- experience ----------------------------------------------------------------------------


def test_experience_can_arrive_in_a_lazy_card() -> None:
    body = _profile(experience_inline=False)
    assert parse_profile(body, slug=PRIYA.slug).positions == ()
    details = parse_profile(body, [experience_payload(ROLES)], slug=PRIYA.slug)
    assert [p.title for p in details.positions] == ["Staff Data Engineer", "Data Engineer"]


def test_the_same_role_inline_and_lazy_is_one_role() -> None:
    details = parse_profile(_profile(), [experience_payload(ROLES)], slug=PRIYA.slug)
    assert len(details.positions) == 2


def test_a_lazy_card_that_is_not_flight_is_skipped_not_the_profile() -> None:
    details = parse_profile(
        _profile(experience_inline=False),
        [b"<html>oops</html>", experience_payload(ROLES)],
        slug=PRIYA.slug,
    )
    assert len(details.positions) == 2


def test_grouped_roles_take_the_groups_company() -> None:
    """**Invented** layout: roles under one company header."""
    group = RoleGroup(
        "Acme Testing Group",
        "Full-time · 5 yrs",
        (("Head of Design", "Jun 2023 - Present · 2 yrs"), ("Designer", "2020 - 2023 · 3 yrs")),
    )
    details = parse_profile(_profile(roles=(), groups=[group]), slug=PRIYA.slug)
    assert details.positions == (
        PositionEntry("Head of Design", "Acme Testing Group", 2023, 6, None, None),
        PositionEntry("Designer", "Acme Testing Group", 2020, None, 2023, None),
    )


@pytest.mark.parametrize(
    "dates",
    [
        "sometime",  # no range at all
        "Smarch 2020 - Present",  # a month that is not one
        "2020 to 2021",  # another phrasing
    ],
)
def test_a_role_that_does_not_read_is_skipped(dates: str) -> None:
    roles = (Role("Odd Role", "Odd Co", None, dates), ROLES[0])
    details = parse_profile(_profile(roles=roles), slug=PRIYA.slug)
    assert [p.title for p in details.positions] == ["Staff Data Engineer"]


def test_a_role_with_an_employment_type_but_no_company_has_none() -> None:
    roles = (Role("Consultant", "Self-employed", None, "2022 - Present"),)
    details = parse_profile(_profile(roles=roles), slug=PRIYA.slug)
    assert details.positions == (PositionEntry("Consultant", None, 2022, None, None, None),)


def test_an_en_dash_reads_like_a_hyphen() -> None:
    roles = (Role("Analyst", "Acme Testing Group", None, "Jan 2019 \u2013 Jul 2021"),)
    details = parse_profile(_profile(roles=roles), slug=PRIYA.slug)
    assert details.positions == (PositionEntry("Analyst", "Acme Testing Group", 2019, 1, 2021, 7),)


# --- education (invented) ------------------------------------------------------------------


def test_education_reads_by_analogy_and_fails_soft() -> None:
    schools = [
        School("Fictional State University", "B.S., Statistics", "2012 - 2016"),
        School("Sample Community College", None, None),
    ]
    details = parse_profile(_profile(schools=schools), slug=PRIYA.slug)
    assert details.education == (
        EducationEntry("Fictional State University", "B.S.", "Statistics", 2012, 2016),
        EducationEntry("Sample Community College", None, None, None, None),
    )


# --- the overlay ---------------------------------------------------------------------------


def test_the_overlay_reads_whole() -> None:
    body = contact_info_payload(
        PRIYA,
        emails=["priya.fake@example.test", "priya.other@example.test"],
        websites=[
            Website("https://priya-fake.example.test", "(Personal)"),
            Website("https://github.com/priya-fake"),
        ],
        connected_since="September 3, 2024",
    )
    assert parse_contact_info(body, slug=PRIYA.slug) == ContactInfo(
        emails=("priya.fake@example.test", "priya.other@example.test"),
        websites=("https://priya-fake.example.test", "https://github.com/priya-fake"),
        connected_on=date(2024, 9, 3),
    )


def test_the_invented_sections_read_by_analogy() -> None:
    body = contact_info_payload(
        PRIYA,
        phones=["+1 555 0101"],
        twitter=["priyafake"],
        birthday="March 3",
        address="1 Example Way, Faketown",
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.phones == ("+1 555 0101",)
    assert info.twitter_handles == ("priyafake",)
    assert info.birthday == "March 3"
    assert info.address == "1 Example Way, Faketown"


def test_a_person_who_shares_nothing_reads_as_nothing() -> None:
    assert parse_contact_info(contact_info_payload(HANA), slug=HANA.slug) == ContactInfo()


def test_an_overlay_for_another_profile_is_refused() -> None:
    body = contact_info_payload(PRIYA, emails=["priya.fake@example.test"])
    with pytest.raises(RouteChanged, match="another profile"):
        parse_contact_info(body, slug=MATEO.slug)


def test_an_overlay_that_does_not_say_whose_it_is_is_refused() -> None:
    body = contact_info_payload(PRIYA, emails=["priya.fake@example.test"], profile_section=False)
    with pytest.raises(RouteChanged, match="whose profile"):
        parse_contact_info(body, slug=PRIYA.slug)


def test_an_overlay_with_no_sections_is_refused() -> None:
    body = b'0:["$","div",null,{"children":["Contact info"]}]\n'
    with pytest.raises(RouteChanged, match="no contact sections"):
        parse_contact_info(body, slug=PRIYA.slug)


@pytest.mark.parametrize(
    "urls",
    [["https://example.test/not-mail"], ["mailto:not-an-address"], ["mailto:"]],
)
def test_an_email_section_in_another_shape_is_refused(urls: list[str]) -> None:
    body = contact_info_payload(PRIYA, email_urls=urls)
    with pytest.raises(RouteChanged, match="email"):
        parse_contact_info(body, slug=PRIYA.slug)


@pytest.mark.parametrize(
    "urls",
    [
        ["https://www.linkedin.com/safety/go/?nope=1"],  # the wrapper, without a site
        ["https://www.linkedin.com/safety/go/?url=a&url=b"],  # two sites in one link
        ["https://www.linkedin.com/redir/redirect?nope=1"],  # the wrapper, without a site
        ["https://www.linkedin.com/redir/redirect?url=a&url=b"],  # two sites in one link
        ["https://www.linkedin.com/redir/redirect?url=two%20words"],
    ],
)
def test_a_website_section_in_another_shape_is_refused(urls: list[str]) -> None:
    body = contact_info_payload(PRIYA, website_urls=urls)
    with pytest.raises(RouteChanged, match="website"):
        parse_contact_info(body, slug=PRIYA.slug)


def test_a_website_outside_the_wrapper_is_read_as_it_is() -> None:
    body = contact_info_payload(PRIYA, website_urls=["https://priya-fake.example.test/"])
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.websites == ("https://priya-fake.example.test/",)


def test_the_invented_sections_leave_out_what_does_not_read() -> None:
    body = contact_info_payload(
        PRIYA, phones=["call me maybe"], twitter=["https://not a handle"], birthday=""
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.phones == () and info.twitter_handles == () and info.birthday is None


def test_a_section_nobody_knows_is_ignored() -> None:
    body = contact_info_payload(
        PRIYA, emails=["priya.fake@example.test"], extra_sections=["contact-carrier-pigeon"]
    )
    assert parse_contact_info(body, slug=PRIYA.slug).emails == ("priya.fake@example.test",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Oct 3, 2023", date(2023, 10, 3)),
        ("3 October 2023", date(2023, 10, 3)),
        ("February 30, 2023", None),
        ("sometime last year", None),
    ],
)
def test_connected_since_fails_soft(text: str, expected: date | None) -> None:
    body = contact_info_payload(PRIYA, connected_since=text)
    assert parse_contact_info(body, slug=PRIYA.slug).connected_on == expected


# --- where an answer is from ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "slug"),
    [
        ("/in/priya-fake/", "priya-fake"),
        ("/in/priya-fake", "priya-fake"),
        ("/in/pr%C3%ADya/", "príya"),
        ("/in/", None),
        ("/in/priya/overlay/contact-info/", None),
        ("/feed/", None),
        ("/in/%0A/", None),
    ],
)
def test_a_profile_path_names_one_slug(path: str, slug: str | None) -> None:
    assert profile_slug(path) == slug


def test_the_navigation_request_is_read_and_never_raises() -> None:
    body = json.dumps(
        {
            "clientArguments": {
                "payload": {"vanityName": PRIYA.slug, "givenName": "Priya"},
                "screenId": "com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay",
            },
            "isModal": True,
        }
    )
    request = parse_navigation_request(body)
    assert request.vanity_name == PRIYA.slug
    assert request.screen_id is not None and request.screen_id.endswith("ContactDetailsOverlay")
    for broken in (None, "", "not json", "[]", '{"clientArguments": 3}'):
        assert parse_navigation_request(broken).screen_id is None


def test_the_fixture_ids_are_invented() -> None:
    assert profile_id(PRIYA).startswith("ACoAAFAKE")


def test_the_contact_info_links_url_must_open_this_profiles_overlay() -> None:
    body = _profile(contact_url=f"/in/{PRIYA.slug}/details/experience/")
    with pytest.raises(RouteChanged, match="does not open this profile's overlay"):
        parse_profile(body, slug=PRIYA.slug)


@pytest.mark.parametrize(
    "urn", ["urn:li:fs_profile:ACoAAFAKE0000101", "urn:li:member:ACoAAFAKE0000101"]
)
def test_only_the_fsd_profile_scheme_is_an_id(urn: str) -> None:
    with pytest.raises(RouteChanged, match="0 profile ids"):
        parse_profile(_profile(raw_urn=urn), slug=PRIYA.slug)


def test_one_extra_run_leaves_the_location_unknown_too() -> None:
    details = parse_profile(_profile(extra_top_runs=["Some Short Run"]), slug=PRIYA.slug)
    assert details.headline == PRIYA.headline and details.location is None


def test_a_linkedin_link_with_a_url_parameter_is_skipped_not_guessed() -> None:
    """A wrapper this reader does not know: which parameter is the site is unknown."""
    body = contact_info_payload(
        PRIYA,
        emails=["priya.fake@example.test"],
        website_urls=["https://www.linkedin.com/feed/?url=https://x.example.test"],
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.websites == () and info.emails == ("priya.fake@example.test",)


# --- #203: what the first live run showed --------------------------------------------------


def test_the_captured_wrapper_is_unwrapped() -> None:
    body = contact_info_payload(
        PRIYA,
        websites=[Website("https://priya-fake.example.test/"), Website("http://blog.example.test")],
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.websites == ("https://priya-fake.example.test/", "http://blog.example.test")


def test_the_older_wrapper_still_unwraps() -> None:
    body = contact_info_payload(
        PRIYA,
        website_urls=["https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fa.example.test"],
    )
    assert parse_contact_info(body, slug=PRIYA.slug).websites == ("https://a.example.test",)


def test_a_linkedin_website_is_a_site_and_the_members_own_profile_is_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """People list LinkedIn pages as websites; LinkedIn does not wrap its own links."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    company = "https://www.linkedin.com/company/fictional-robotics-co/"
    newsletter = "https://www.linkedin.com/newsletters/fake-notes-0000000000000000000/"
    body = contact_info_payload(
        PRIYA,
        emails=["priya.fake@example.test"],
        website_urls=[
            f"https://www.linkedin.com/in/{PRIYA.slug}/",
            f"https://linkedin.com/in/{PRIYA.slug.upper()}",
            company,
            "https://www.linkedin.com/safety/go/?url="
            f"https%3A%2F%2Fwww.linkedin.com%2Fin%2F{PRIYA.slug}%2F&urlhash=FAKE",
            newsletter,
            f"https://www.linkedin.com/in/{MATEO.slug}/",
            "https://priya-fake.example.test/",
        ],
        connected_since="Oct 3, 2023",
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info.websites == (
        company,
        newsletter,
        f"https://www.linkedin.com/in/{MATEO.slug}/",
        "https://priya-fake.example.test/",
    )
    assert info.emails == ("priya.fake@example.test",)
    assert info.connected_on == date(2023, 10, 3)
    counted = [r for r in caplog.records if "website link(s) to LinkedIn" in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in counted] == [
        (logging.INFO, "enrichment: skipped 3 website link(s) to LinkedIn itself")
    ]
    assert PRIYA.slug not in caplog.text


def test_one_skipped_linkedin_website_is_counted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    body = contact_info_payload(PRIYA, website_urls=[f"https://www.linkedin.com/in/{PRIYA.slug}"])
    assert parse_contact_info(body, slug=PRIYA.slug).websites == ()
    assert "skipped 1 website link(s) to LinkedIn itself" in caplog.text


def test_an_element_is_never_read_as_runs() -> None:
    """A ``p`` or a text component whose ``children`` is one element, not a list of
    them, holds no run of its own: the element's ``$`` and tag are not text."""
    strong = ["$", "strong", None, {"children": ["Staff Data Engineer"]}]
    item = [
        "$",
        "li",
        None,
        {
            "children": [
                ["$", "p", None, {"children": strong}],
                ["$", "$L1", None, {"textProps": {"children": strong}}],
            ]
        },
    ]
    payload = parse_flight(b"0:" + json.dumps(item).encode() + b"\n", endpoint="test")
    runs, nested = flagship_profile._item_runs(payload, payload.rows["0"], endpoint="test")
    assert (runs, nested) == ([], [])


def test_no_linkedin_website_logs_no_count(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    body = contact_info_payload(PRIYA, websites=[Website("https://priya-fake.example.test/")])
    parse_contact_info(body, slug=PRIYA.slug)
    assert "website link" not in caplog.text


def test_the_instant_message_section_is_ignored_quietly(caplog: pytest.LogCaptureFixture) -> None:
    """The CRM has no field for a messaging handle (spec 8.1): known, and not read."""
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    body = contact_info_payload(
        PRIYA,
        emails=["priya.fake@example.test"],
        extra_sections=["contact-instant-message", "contact-carrier-pigeon"],
    )
    info = parse_contact_info(body, slug=PRIYA.slug)
    assert info == ContactInfo(emails=("priya.fake@example.test",))
    by_level = {r.getMessage(): r.levelno for r in caplog.records}
    assert by_level == {
        "enrichment: the overlay has a messaging section; not stored": logging.DEBUG,
        "enrichment: the overlay has a section this reader does not know:"
        " contact-carrier-pigeon": logging.INFO,
    }


def test_the_degree_rendered_twice_is_not_the_headline() -> None:
    """The captured top card renders the degree twice, one run after the other."""
    for runs in (1, 2, 3):
        details = parse_profile(_profile(degree_runs=runs), slug=PRIYA.slug)
        assert (details.headline, details.location) == (PRIYA.headline, LOCATION)


def test_the_top_card_is_read_once_per_profile(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    body = _profile(extra_top_runs=["Some Short Run"])
    assert parse_profile_urn(body, slug=PRIYA.slug) == PRIYA.urn
    assert caplog.records == []  # the id alone reads nothing it would log
    parse_profile(body, slug=PRIYA.slug)
    assert [r.getMessage() for r in caplog.records] == [
        "enrichment: the top card has 3 runs before its link; location unknown"
    ]


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"top_cards": 0}, "0 profile-top-card"),
        ({"contact_links": 0}, "0 Contact info links"),
        ({"identity": "none"}, "0 profile ids"),
    ],
)
def test_the_id_alone_is_refused_as_the_profile_is(options: dict[str, object], match: str) -> None:
    with pytest.raises(RouteChanged, match=match):
        parse_profile_urn(_profile(**options), slug=PRIYA.slug)
    with pytest.raises(RouteChanged, match="another profile"):
        parse_profile_urn(_profile(), slug=MATEO.slug)


def test_a_title_in_a_plain_paragraph_reads() -> None:
    """The captured entry: the title in a ``p``, then the company line and the dates as
    text runs, inside a link to the company's page."""
    roles = (
        Role("Staff Data Engineer", "Fictional Robotics Co", "Full-time", "Aug 2021 - Present"),
        Role("Data Engineer", "Placeholder Partners", None, "Jan 2019 - Jul 2021", "Remote"),
        Role("Analyst", None, "Part-time", "2017 - 2018 · 1 yr", "Faketown, Exampleland"),
        Role("Intern", None, None, "Jun 2016 - Aug 2016 · 3 mos", "Faketown, Exampleland"),
    )
    details = parse_profile(_profile(roles=roles), slug=PRIYA.slug)
    assert details.positions == (
        PositionEntry("Staff Data Engineer", "Fictional Robotics Co", 2021, 8, None, None),
        PositionEntry("Data Engineer", "Placeholder Partners", 2019, 1, 2021, 7),
        PositionEntry("Analyst", None, 2017, None, 2018, None),
        PositionEntry("Intern", None, 2016, 6, 2016, 8),
    )


@pytest.mark.parametrize("lazy", [False, True])
def test_titles_as_text_runs_still_read(lazy: bool) -> None:
    if lazy:
        body = _profile(experience_inline=False)
        details = parse_profile(
            body, [experience_payload(ROLES, legacy_titles=True)], slug=PRIYA.slug
        )
    else:
        details = parse_profile(_profile(legacy_titles=True), slug=PRIYA.slug)
    assert [(p.title, p.company) for p in details.positions] == [
        ("Staff Data Engineer", "Fictional Robotics Co"),
        ("Data Engineer", "Placeholder Partners"),
    ]


def test_every_captured_entry_reads_and_none_is_skipped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="netkeeper")
    details = parse_profile(_profile(), slug=PRIYA.slug)
    assert len(details.positions) == 2
    assert "did not read" not in caplog.text
