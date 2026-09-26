"""A profile and its contact-info overlay, as ``flagship-web`` loads them (ADR 0006, spec 9.4).

The shape half of enrichment. :mod:`netkeeper.linkedin.page_profiles` is the browser half:
it navigates to a profile, scrolls it, clicks **Contact info** once, and hands the answers
the page loaded to the parsers here. ``docs/linkedin-flagship-web-shapes.md`` records the
shapes, structure only; the constants below are dated where the #149 capture showed them
and marked *invented* where it did not.

**Whose profile it is.** A profile answer names its member in the page's own action
payloads, the way a connections card does: a ``vieweeProfileId`` or a ``profileUrn``
beside the member's ``vanityName``. :func:`parse_profile` reads the member's id only from
payloads that also name the profile's own slug, and from payloads inside the top card
that name no slug at all (the message and follow buttons), and requires exactly one id
among them. A page that names none, or two, is :class:`~netkeeper.linkedin.voyager.RouteChanged`:
the "People also viewed" rail and the shared-connections line carry other people's ids
beside other people's slugs, and a guess among them could put one person's details on
another's contact. The core then checks that one id against the URN it holds for the
contact (``crm/apply.py``), and the job checks it before it clicks anything.

**The overlay names its member too.** The contact-info answer's first section links to
the member's own profile. :func:`parse_contact_info` requires that link and requires its
slug to be the profile's, so an overlay left over from another profile, or answered for
somebody else, is refused rather than read.

**What fails soft, and what does not.** What the capture showed (the top card, the
Contact info link and its payload, the email and website sections, the profile link in
the overlay) is read strictly: a shape that moved is ``RouteChanged``, and the job
counts the profile unreadable. A website a person listed on LinkedIn's own host is not a
shape that moved (#203): one to their own profile is skipped, and any other is kept.
What the capture did not show -- the phone, address, birthday, Twitter, and messaging
sections, education, grouped roles -- is read by analogy and fails soft: a value that
does not read is left out, never guessed and never a run stop. An experience entry that
does not read is skipped, not the profile: positions are upserted and never removed, so
a skipped entry costs nothing it could take away. The headline and location are read
from the top card's text runs by position; when the runs are not the captured layout
they are left unknown, which ``crm/apply.py`` reads as "not provided".

Pure: no browser, no database (spec 9.10). Values never reach a log or an exception:
errors describe the shape, never a name, a slug, or an address.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit

from netkeeper.linkedin.flagship import (
    CONTACT_DETAILS_SCREEN_ID,
    URN_PREFIX,
)
from netkeeper.linkedin.flight import FlightPayload, element_props, is_element, parse_flight
from netkeeper.linkedin.voyager import (
    ContactInfo,
    EducationEntry,
    PositionEntry,
    ProfileDetails,
    RouteChanged,
)

log = logging.getLogger(__name__)

# --- constants: captured 2026-09-24 (#149) unless marked invented ---------------------

#: A profile page a person opens: ``/in/<slug>/``. Navigated to, never fetched.
PROFILE_PAGE_PREFIX: Final = "/in/"

#: The profile screen's flight payload, when the client navigates to it in-app.
PROFILE_SCREEN_PREFIX: Final = "/flagship-web/in/"  # captured 2026-09-24

#: Where the page asks for a lazy card of the profile as it is scrolled.
COMPONENT_PATH: Final = "/flagship-web/rsc-action/actions/component"  # captured 2026-09-24

#: The top card: degree, headline, location, the Contact info link.
TOP_CARD_VIEW: Final = "profile-top-card"  # captured 2026-09-24

#: The experience card, inline on the screen or loaded lazily.
EXPERIENCE_VIEW: Final = "profile-card-experience"  # captured 2026-09-24

#: The education card. **Invented**: education was not in the capture; by analogy with
#: the experience card. Its reader fails soft.
EDUCATION_VIEW: Final = "profile-card-education"

#: The accessible name of the one control ADR 0006 allows a click on.
CONTACT_INFO_LABEL: Final = "Contact info"  # captured 2026-09-24

#: What the Contact info link's url adds to the profile's own path.
CONTACT_INFO_OVERLAY_SUFFIX: Final = "overlay/contact-info/"  # captured 2026-09-24

#: The overlay's sections, by ``viewTrackingSpecs.viewName``.
SECTION_PROFILE: Final = "contact-your-profile"  # captured 2026-09-24
SECTION_WEBSITE: Final = "contact-website"  # captured 2026-09-24
SECTION_EMAIL: Final = "contact-email"  # captured 2026-09-24
#: **Invented**, by analogy: the capture's profile shared none of these.
SECTION_PHONE: Final = "contact-phone"
SECTION_TWITTER: Final = "contact-twitter"
SECTION_BIRTHDAY: Final = "contact-birthday"
SECTION_ADDRESS: Final = "contact-address"
#: Seen on the first live run (#203), not in the capture: a messaging handle. The CRM has
#: no field for one (spec 8.1), so the section is recognized and read no further.
SECTION_INSTANT_MESSAGE: Final = "contact-instant-message"
SECTION_PREFIX: Final = "contact-"

#: The overlay's last line: ``Connected since`` and the day, as text.
CONNECTED_SINCE: Final = "Connected since"  # captured 2026-09-24

#: The wrappers a website link goes through on LinkedIn's host, and the parameter that
#: holds the site. The #149 capture's overlay used ``/safety/go`` (with ``isSdui``,
#: ``mt``, and ``urlhash`` beside ``url``); ``/redir/redirect`` is the older client's.
REDIRECT_PATHS: Final = frozenset({"/safety/go", "/redir/redirect"})  # captured 2026-09-24
REDIRECT_PARAM: Final = "url"

#: The schemes a website must have to be kept (#206 review).
_SITE_SCHEMES: Final = frozenset({"http", "https"})

PROFILE_ENDPOINT: Final = "flagship-web/profile"
COMPONENT_ENDPOINT: Final = "flagship-web/profile-component"
CONTACT_INFO_ENDPOINT: Final = "flagship-web/contact-info"

_NAVIGATE: Final = "proto.sdui.actions.core.Navigate"
_TO_SCREEN: Final = "proto.sdui.actions.core.NavigateToScreen"
_TO_URL: Final = "proto.sdui.actions.core.NavigateToUrl"

#: A profile id, as :mod:`netkeeper.linkedin.flagship` reads a card's.
_PROFILE_ID: Final = re.compile(r"[A-Za-z0-9_-]{8,100}")

#: The degree run that opens the top card: ``· 1st``, ``· 2nd``, ``· 3rd+``.
_DEGREE: Final = re.compile(r"·\s*(?:1st|2nd|3rd\+?)")

_MONTHS: Final[tuple[str, ...]] = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MON: Final = r"([A-Za-z]{3,9})\.?"
#: ``Aug 2021 - Present · 3 yrs 2 mos``; ``2019 - 2021``; en dash or hyphen.
_DASH: Final = "[-\u2013]"
_DATE_RANGE: Final = re.compile(
    rf"(?:{_MON}\s+)?(\d{{4}})\s*{_DASH}\s*(?:(?:{_MON}\s+)?(\d{{4}})|(Present))(?:\s*·.*)?"
)
#: ``September 3, 2024`` (en-US) or ``3 September 2024`` (en-GB).
_DAY_US: Final = re.compile(r"([A-Za-z]+)\.? (\d{1,2}), (\d{4})")
_DAY_GB: Final = re.compile(r"(\d{1,2}) ([A-Za-z]+)\.?,? (\d{4})")

#: What LinkedIn writes after a company in a role (``Acme · Full-time``) and alone
#: under a grouped company.
EMPLOYMENT_TYPES: Final = frozenset(
    {
        "Full-time",
        "Part-time",
        "Self-employed",
        "Freelance",
        "Contract",
        "Internship",
        "Apprenticeship",
        "Seasonal",
        "Temporary",
    }
)

MAX_TEXT: Final = 500
_EMAIL: Final = re.compile(r"[^@\s<>\"'()]{1,200}@[^@\s<>\"'()]{1,200}\.[^@\s<>\"'()]{1,63}")
_PHONE: Final = re.compile(r"\+?[\d][\d\s().-]{4,40}")
_HANDLE: Final = re.compile(r"@?([A-Za-z0-9_]{1,50})")
_LINE_BREAKS: Final = "\u2028\u2029"


# --- where an answer is from --------------------------------------------------------


def profile_slug(path: str) -> str | None:
    """The slug of a profile page's path (``/in/<slug>/``), decoded, or ``None``.

    One path segment after ``/in/``, and nothing after it but an optional ``/``.
    """
    if not path.startswith(PROFILE_PAGE_PREFIX):
        return None
    rest = path[len(PROFILE_PAGE_PREFIX) :]
    rest = rest[:-1] if rest.endswith("/") else rest
    if not rest or "/" in rest:
        return None
    slug = unquote(rest)
    return slug if slug.strip() and not _has_control(slug) else None


def same_slug(a: str, b: str) -> bool:
    """Whether two slugs name one profile: LinkedIn's routing folds case."""
    return unquote(a).casefold() == unquote(b).casefold()


@dataclass(frozen=True, slots=True)
class NavigationRequest:
    """What the page asked the navigation endpoint for: which screen, for which slug."""

    screen_id: str | None
    vanity_name: str | None


def parse_navigation_request(body: str | None) -> NavigationRequest:
    """The page's own ``actions/navigation`` request, read-only. Never raises.

    ``screenId`` and ``clientArguments.payload.vanityName``, as captured; either is
    ``None`` when the body does not carry it.
    """
    try:
        request = json.loads(body) if body else None
    except (ValueError, RecursionError):
        request = None
    arguments = request.get("clientArguments") if isinstance(request, dict) else None
    screen = arguments.get("screenId") if isinstance(arguments, dict) else None
    payload = arguments.get("payload") if isinstance(arguments, dict) else None
    vanity = payload.get("vanityName") if isinstance(payload, dict) else None
    return NavigationRequest(
        screen_id=screen if isinstance(screen, str) else None,
        vanity_name=vanity if isinstance(vanity, str) else None,
    )


# --- the profile ------------------------------------------------------------------------


def parse_profile(
    screen: bytes | str, components: Sequence[bytes] = (), *, slug: str
) -> ProfileDetails:
    """The profile the screen answer shows, read whole or refused.

    ``screen`` is the profile screen's flight payload (from the page's HTML or its
    screen request); ``components`` are the lazy cards the page loaded for this visit,
    in arrival order; ``slug`` is the profile the tab is on. Raises
    :class:`~netkeeper.linkedin.voyager.RouteChanged` when the top card, the Contact
    info link, or the member's one id cannot be read.
    """
    endpoint = PROFILE_ENDPOINT
    payload = parse_flight(screen, endpoint=endpoint)
    top = _one_view(payload, TOP_CARD_VIEW, endpoint=endpoint)
    vanity, first, last = _contact_link(payload, top, slug=slug, endpoint=endpoint)
    urn = _identity(payload, top, slug=slug, endpoint=endpoint)
    headline, location = _top_card_text(payload, top, endpoint=endpoint)
    positions: list[PositionEntry] = []
    education: list[EducationEntry] = []
    for answer in (payload, *_component_payloads(components)):
        for card in _views(answer, EXPERIENCE_VIEW, endpoint=endpoint):
            positions.extend(_roles(answer, card, endpoint=endpoint))
        for card in _views(answer, EDUCATION_VIEW, endpoint=endpoint):
            education.extend(_schools(answer, card, endpoint=endpoint))
    names = {p.company for p in positions if p.company} | {e.school for e in education}
    if location is not None and location in names:
        # Two runs where the second is a company or a school the page lists: a profile
        # with no location, and one of the short runs the capture saw among them.
        location = None
    return ProfileDetails(
        urn=urn,
        public_id=vanity,
        first_name=first,
        last_name=last,
        headline=headline,
        location=location,
        positions=tuple(dict.fromkeys(positions)),
        education=tuple(dict.fromkeys(education)),
    )


def parse_profile_urn(screen: bytes | str, *, slug: str) -> str:
    """The member's URN from the screen alone, checked as :func:`parse_profile` checks it.

    What the browser half reads first, before it decides which lazy cards are this
    member's. It reads nothing else, so nothing it logs repeats when the whole profile
    is read after it. Raises :class:`~netkeeper.linkedin.voyager.RouteChanged` as
    :func:`parse_profile` does for the top card, the Contact info link, and the id.
    """
    endpoint = PROFILE_ENDPOINT
    payload = parse_flight(screen, endpoint=endpoint)
    top = _one_view(payload, TOP_CARD_VIEW, endpoint=endpoint)
    _contact_link(payload, top, slug=slug, endpoint=endpoint)
    return _identity(payload, top, slug=slug, endpoint=endpoint)


def _component_payloads(components: Sequence[bytes]) -> Iterator[FlightPayload]:
    """Each lazy card's payload. A card that is not flight is skipped, not the profile:
    it is one of a dozen the page loads, most of them nothing enrichment reads."""
    for index, body in enumerate(components):
        try:
            yield parse_flight(body, endpoint=COMPONENT_ENDPOINT)
        except RouteChanged:
            log.warning("enrichment: lazy card %d of the profile is not flight; skipped", index)


def _views(payload: FlightPayload, view: str, *, endpoint: str) -> list[object]:
    """Every element whose ``viewTrackingSpecs.viewName`` is ``view``."""
    found: list[object] = []
    for node in payload.nodes(endpoint=endpoint):
        props = element_props(node)
        if props is None:
            continue
        specs = props.get("viewTrackingSpecs")
        if isinstance(specs, dict) and specs.get("viewName") == view:
            found.append(node)
    return found


def _one_view(payload: FlightPayload, view: str, *, endpoint: str) -> object:
    found = _views(payload, view, endpoint=endpoint)
    if len(found) != 1:
        raise RouteChanged(endpoint, f"{len(found)} {view} elements, not one")
    return found[0]


def _contact_link(
    payload: FlightPayload, top: object, *, slug: str, endpoint: str
) -> tuple[str, str, str]:
    """``(vanityName, givenName, familyName)`` from the top card's one Contact info link."""
    links: set[tuple[object, object, object, object]] = set()
    for node in payload.walk(top, follow=True, endpoint=endpoint):
        screen = _navigate_to_screen(node)
        if screen is None or screen.get("screenId") != CONTACT_DETAILS_SCREEN_ID:
            continue
        arguments = screen.get("requestedArguments")
        body = arguments.get("payload") if isinstance(arguments, dict) else None
        if not isinstance(body, dict):
            raise RouteChanged(endpoint, "a Contact info link without its payload")
        links.add(
            (
                screen.get("url"),
                body.get("vanityName"),
                body.get("givenName"),
                body.get("familyName"),
            )
        )
    if len(links) != 1:
        raise RouteChanged(endpoint, f"{len(links)} Contact info links in the top card, not one")
    ((url, vanity, given, family),) = links
    if not isinstance(vanity, str) or not same_slug(vanity, slug):
        raise RouteChanged(endpoint, "the Contact info link names another profile than the tab's")
    if not isinstance(url, str) or url != f"/in/{vanity}/{CONTACT_INFO_OVERLAY_SUFFIX}":
        raise RouteChanged(endpoint, "the Contact info link does not open this profile's overlay")
    first = _name(given, endpoint=endpoint, where="givenName")
    last = _name(family, endpoint=endpoint, where="familyName", empty_ok=True)
    if not first:
        raise RouteChanged(endpoint, "the Contact info link carries no given name")
    return vanity, first, last


def _navigate_to_screen(node: object) -> dict[str, object] | None:
    """The ``NavigateToScreen`` a ``Navigate`` action holds, or ``None``."""
    if not isinstance(node, dict) or node.get("$type") != _NAVIGATE:
        return None
    value = node.get("value")
    content = value.get("content") if isinstance(value, dict) else None
    screen = content.get("screen") if isinstance(content, dict) else None
    if isinstance(screen, dict) and screen.get("$type") == _TO_SCREEN:
        return screen
    return None


def _name(value: object, *, endpoint: str, where: str, empty_ok: bool = False) -> str:
    if not isinstance(value, str):
        if empty_ok and value is None:
            return ""
        raise RouteChanged(endpoint, f"a {where} that is not text")
    if _has_control(value):
        raise RouteChanged(endpoint, f"a {where} with a control character")
    normalized = " ".join(value.split())
    if len(normalized) > 200:
        raise RouteChanged(endpoint, f"a {where} that is too long to be a name")
    return normalized


def _identity(payload: FlightPayload, top: object, *, slug: str, endpoint: str) -> str:
    """The member's URN: the one id the page names for this profile, or refused.

    Read from every payload that names this profile's slug beside an id, and from
    payloads inside the top card that name an id and no slug at all. An id beside
    another slug is somebody else's (a shared connection, "People also viewed").
    """
    ids: set[str] = set()
    for node in payload.nodes(endpoint=endpoint):
        found = _id_of(node)
        if found is None:
            continue
        vanity = node.get("vanityName") if isinstance(node, dict) else None
        if isinstance(vanity, str) and same_slug(vanity, slug):
            ids.add(found)
    for node in payload.walk(top, follow=True, endpoint=endpoint):
        found = _id_of(node)
        if found is not None and isinstance(node, dict) and "vanityName" not in node:
            ids.add(found)
    if len(ids) != 1:
        raise RouteChanged(endpoint, f"{len(ids)} profile ids name this profile, not one")
    return ids.pop()


def _id_of(node: object) -> str | None:
    """The profile URN a payload names, from ``vieweeProfileId`` or ``profileUrn``."""
    if not isinstance(node, dict):
        return None
    viewee = node.get("vieweeProfileId")
    if isinstance(viewee, str) and _PROFILE_ID.fullmatch(viewee):
        return f"{URN_PREFIX}{viewee}"
    urn = node.get("profileUrn")
    if isinstance(urn, str) and urn.startswith(URN_PREFIX):
        rest = urn[len(URN_PREFIX) :]
        if _PROFILE_ID.fullmatch(rest):
            return urn
    return None


#: A text run's placeholder for the Contact info link, which sits among the runs.
_LINK: Final = object()


def _runs(payload: FlightPayload, root: object, *, endpoint: str) -> list[object]:
    """The text runs under ``root``, in page order: each ``textProps.children`` string,
    and :data:`_LINK` where a run holds the Contact info link."""
    runs: list[object] = []
    for node in payload.walk(root, follow=True, endpoint=endpoint):
        props = element_props(node)
        text = props.get("textProps") if props is not None else None
        children = text.get("children") if isinstance(text, dict) else None
        if not isinstance(children, list):
            continue
        for child in children:
            if isinstance(child, str):
                runs.append(child)
            elif is_element(child) and _is_contact_link(child):
                runs.append(_LINK)
    return runs


def _is_contact_link(node: object) -> bool:
    props = element_props(node)
    return props is not None and props.get("children") == [CONTACT_INFO_LABEL]


def _top_card_text(
    payload: FlightPayload, top: object, *, endpoint: str
) -> tuple[str | None, str | None]:
    """``(headline, location)``: the two runs between the degree and the separator that
    precedes the Contact info link, as captured. With one run, it is the headline; with
    more than two, the location is left unknown rather than picked among them."""
    runs = _runs(payload, top, endpoint=endpoint)
    if _LINK not in runs:
        return None, None
    end = runs.index(_LINK)
    start = next(
        (i + 1 for i, run in enumerate(runs[:end]) if _is_degree(run)),
        None,
    )
    if start is None:
        log.warning("enrichment: the top card has no degree run; headline and location unknown")
        return None, None
    # The captured top card renders the degree twice (#203), one run after the other:
    # every degree run is the degree, never the headline.
    between = [
        run
        for run in runs[start:end]
        if isinstance(run, str) and run.strip() != "·" and not _is_degree(run)
    ]
    if not between:
        return None, None
    headline = _text(between[0])
    if len(between) > 2:
        # Short runs (a company, a school) sit among them on some profiles; which is
        # which is unverified, so the location is not guessed from among them.
        log.info(
            "enrichment: the top card has %d runs before its link; location unknown", len(between)
        )
        return headline, None
    location = _text(between[1]) if len(between) == 2 else None
    return headline, location


def _is_degree(run: object) -> bool:
    return isinstance(run, str) and _DEGREE.fullmatch(run.strip()) is not None


def _text(value: str) -> str | None:
    """A run as a field: whitespace collapsed; empty, too long, or not text is unknown."""
    if _has_control(value):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_TEXT:
        return None
    return normalized


def _has_control(text: str) -> bool:
    return any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 or ch in _LINE_BREAKS for ch in text)


# --- experience and education -------------------------------------------------------------


def _items(payload: FlightPayload, root: object, *, endpoint: str) -> list[object]:
    """The outermost ``li`` elements under ``root``, in page order."""
    items: list[object] = []
    stack: list[object] = [root]
    entered: set[int] = set()
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > 200_000:
            raise RouteChanged(endpoint, "a card too large to read")
        if is_element(node) and node is not root and isinstance(node, list) and node[1] == "li":
            items.append(node)
            continue
        if isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
        else:
            target = payload.resolve(node)
            if target is not None and id(target) not in entered:
                entered.add(id(target))
                stack.append(target)
    return items


#: Plain HTML elements whose own strings are an entry's runs too. The captured experience
#: entry renders its title as a ``p`` whose ``children`` are the text, ahead of the
#: ``textProps`` runs that hold the company line and the dates (#203).
_PLAIN_TEXT_TAGS: Final = frozenset({"p"})


def _item_runs(
    payload: FlightPayload, item: object, *, endpoint: str
) -> tuple[list[str] | None, list[object]]:
    """An ``li``'s own text runs, in page order, and the ``li`` elements nested in it.

    A run is a string in a text component's ``textProps.children``, or a string among a
    plain ``p`` element's ``children``. The runs are ``None`` when the entry's first
    ``p`` -- its title, as captured -- holds no text of its own (#206 review).
    """
    assert isinstance(item, list)
    nested = _items(payload, item[3], endpoint=endpoint)
    own: list[str] = []
    title_seen = False
    for node in _without(payload, item[3], nested, endpoint=endpoint):
        props = element_props(node)
        if props is None:
            continue
        text = props.get("textProps")
        plain = _is_plain_text(node)
        if plain:
            children = props.get("children")
        else:
            children = text.get("children") if isinstance(text, dict) else None
        strings = (
            [child for child in children if isinstance(child, str)]
            if isinstance(children, list) and not is_element(children)
            else []
        )
        if plain and not title_seen:
            title_seen = True
            if not strings:
                # The entry's title element holds no text of its own (its text sits in
                # an element inside it): the runs after it would slide into its place
                # and a company would read as a title. The entry does not read.
                return None, nested
        own.extend(strings)
    return own, nested


def _is_plain_text(node: object) -> bool:
    return isinstance(node, list) and node[1] in _PLAIN_TEXT_TAGS


def _without(
    payload: FlightPayload, root: object, skip: list[object], *, endpoint: str
) -> Iterator[object]:
    """Every node under ``root`` in page order, not entering any element in ``skip``."""
    skipped = {id(node) for node in skip}
    stack: list[object] = [root]
    entered: set[int] = set()
    seen = 0
    while stack:
        node = stack.pop()
        seen += 1
        if seen > 200_000:
            raise RouteChanged(endpoint, "a card too large to read")
        if id(node) in skipped:
            continue
        yield node
        if isinstance(node, dict):
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
        else:
            target = payload.resolve(node)
            if target is not None and id(target) not in entered:
                entered.add(id(target))
                stack.append(target)


def _roles(payload: FlightPayload, card: object, *, endpoint: str) -> list[PositionEntry]:
    """Every role the experience card renders that reads whole. The rest are skipped."""
    roles: list[PositionEntry] = []
    skipped = 0
    for item in _items(payload, card, endpoint=endpoint):
        runs, nested = _item_runs(payload, item, endpoint=endpoint)
        if nested:
            if runs is None:
                skipped += len(nested)  # the company header did not read: never guess
                continue
            # A company with several roles under it: its own first run is the company.
            company = _text(runs[0]) if runs else None
            for inner in nested:
                inner_runs, deeper = _item_runs(payload, inner, endpoint=endpoint)
                if deeper or inner_runs is None:
                    skipped += 1  # nesting the capture never showed: skip, never guess
                    continue
                role = _role(inner_runs, company=company)
                if role is None:
                    skipped += 1
                else:
                    roles.append(role)
            continue
        role = _role(runs, company=None) if runs is not None else None
        if role is None:
            skipped += 1
        else:
            roles.append(role)
    if skipped:
        log.info("enrichment: %d experience entries did not read and were skipped", skipped)
    return roles


def _role(runs: list[str], *, company: str | None) -> PositionEntry | None:
    """One role from its runs: the title, then ``<Company> · <type>``, then the dates."""
    texts = [" ".join(run.split()) for run in runs]
    at = next((i for i, run in enumerate(texts) if _dates(run) is not None), None)
    if at is None or at == 0 or at > 2:
        return None
    title = _text(texts[0])
    if title is None:
        return None
    if at == 2:
        head = texts[1].partition(" · ")[0].strip()
        if head in EMPLOYMENT_TYPES:
            head_company = company
        else:
            head_company = _text(head)
            if head_company is None:
                return None
    else:
        head_company = company
    dates = _dates(texts[at])
    assert dates is not None
    start_year, start_month, end_year, end_month = dates
    return PositionEntry(
        title=title,
        company=head_company,
        start_year=start_year,
        start_month=start_month,
        end_year=end_year,
        end_month=end_month,
    )


def _dates(text: str) -> tuple[int | None, int | None, int | None, int | None] | None:
    """``(start_year, start_month, end_year, end_month)`` from a role's date run, or ``None``.

    ``Present`` is an open end (both ``None``). A month that is not an English month
    name leaves the whole run unread, rather than a year read without its month.
    """
    match = _DATE_RANGE.fullmatch(text)
    if match is not None:
        start_mon, start_year, end_mon, end_year, present = match.groups()
        start_month = _month(start_mon) if start_mon else None
        end_month = _month(end_mon) if end_mon else None
        if (start_mon and start_month is None) or (end_mon and end_month is None):
            return None
        if present:
            return int(start_year), start_month, None, None
        return int(start_year), start_month, int(end_year), end_month
    return None


def _month(name: str) -> int | None:
    lowered = name.lower().rstrip(".")
    for number, full in enumerate(_MONTHS, start=1):
        if lowered == full or (len(lowered) >= 3 and full.startswith(lowered)):
            return number
    return None


def _schools(payload: FlightPayload, card: object, *, endpoint: str) -> list[EducationEntry]:
    """Education entries, by analogy with experience (**invented**; fails soft)."""
    schools: list[EducationEntry] = []
    for item in _items(payload, card, endpoint=endpoint):
        runs, nested = _item_runs(payload, item, endpoint=endpoint)
        if nested or not runs:
            continue
        texts = [" ".join(run.split()) for run in runs]
        school = _text(texts[0])
        if school is None:
            continue
        degree = field = None
        years: tuple[int | None, int | None] = (None, None)
        for run in texts[1:]:
            dates = _dates(run)
            if dates is not None:
                years = (dates[0], dates[2])
            elif degree is None and field is None:
                head, _, tail = run.partition(", ")
                degree, field = _text(head), _text(tail) if tail else None
        schools.append(
            EducationEntry(
                school=school,
                degree=degree,
                field_of_study=field,
                start_year=years[0],
                end_year=years[1],
            )
        )
    return schools


# --- the contact-info overlay ---------------------------------------------------------------


def parse_contact_info(body: bytes | str, *, slug: str) -> ContactInfo:
    """The overlay's answer (``actions/navigation``), for the profile at ``slug``.

    Raises :class:`~netkeeper.linkedin.voyager.RouteChanged` when the answer has no
    contact sections, when its profile section is missing or links to another profile,
    or when a captured section (email, website) holds a value in a shape the capture
    did not show. The invented sections fail soft.
    """
    endpoint = CONTACT_INFO_ENDPOINT
    payload = parse_flight(body, endpoint=endpoint)
    sections: dict[str, list[object]] = {}
    for node in payload.nodes(endpoint=endpoint):
        props = element_props(node)
        specs = props.get("viewTrackingSpecs") if props is not None else None
        view = specs.get("viewName") if isinstance(specs, dict) else None
        if isinstance(view, str) and view.startswith(SECTION_PREFIX):
            sections.setdefault(view, []).append(node)
    if not sections:
        raise RouteChanged(endpoint, "an overlay with no contact sections")
    profile_links = [
        url
        for node in sections.get(SECTION_PROFILE, [])
        for url, _ in _links(payload, node, endpoint=endpoint)
    ]
    if not profile_links:
        raise RouteChanged(endpoint, "an overlay that does not say whose profile it is")
    for url in profile_links:
        linked = profile_slug(urlsplit(url).path)
        if linked is None or not same_slug(linked, slug):
            raise RouteChanged(endpoint, "an overlay for another profile than the tab's")
    emails = _emails(payload, sections.get(SECTION_EMAIL, []), endpoint=endpoint)
    websites = _websites(payload, sections.get(SECTION_WEBSITE, []), slug=slug, endpoint=endpoint)
    phones = _soft_phones(payload, sections.get(SECTION_PHONE, []), endpoint=endpoint)
    handles = _soft_handles(payload, sections.get(SECTION_TWITTER, []), endpoint=endpoint)
    birthday = _soft_value(payload, sections.get(SECTION_BIRTHDAY, []), endpoint=endpoint)
    address = _soft_value(payload, sections.get(SECTION_ADDRESS, []), endpoint=endpoint)
    known = {
        SECTION_PROFILE,
        SECTION_EMAIL,
        SECTION_WEBSITE,
        SECTION_PHONE,
        SECTION_TWITTER,
        SECTION_BIRTHDAY,
        SECTION_ADDRESS,
    }
    if SECTION_INSTANT_MESSAGE in sections:
        # Nowhere to put it (spec 8.1): known, and read no further.
        log.debug("enrichment: the overlay has a messaging section; not stored")
    for view in sorted(set(sections) - known - {SECTION_INSTANT_MESSAGE}):
        log.info("enrichment: the overlay has a section this reader does not know: %s", view)
    return ContactInfo(
        emails=emails,
        phones=phones,
        websites=websites,
        twitter_handles=handles,
        birthday=birthday,
        address=address,
        connected_on=_connected_since(payload, endpoint=endpoint),
    )


def _links(payload: FlightPayload, section: object, *, endpoint: str) -> list[tuple[str, str]]:
    """``(url, shown text)`` for each link out (``NavigateToUrl``) in a section."""
    links: list[tuple[str, str]] = []
    for node in payload.walk(section, follow=True, endpoint=endpoint):
        props = element_props(node)
        action = props.get("action") if props is not None else None
        actions = action.get("actions") if isinstance(action, dict) else None
        if not isinstance(actions, list):
            continue
        for item in actions:
            url = _url_of(item)
            if url is not None:
                children = props.get("children") if props is not None else None
                shown = (
                    " ".join(c for c in children if isinstance(c, str))
                    if isinstance(children, list) and not is_element(children)
                    else ""
                )
                links.append((url, shown))
    return links


def _url_of(action: object) -> str | None:
    if not isinstance(action, dict) or action.get("$type") != _NAVIGATE:
        return None
    value = action.get("value")
    content = value.get("content") if isinstance(value, dict) else None
    target = content.get("url") if isinstance(content, dict) else None
    if not isinstance(target, dict) or target.get("$type") != _TO_URL:
        return None
    url_value = target.get("urlValue")
    url = url_value.get("url") if isinstance(url_value, dict) else None
    return url if isinstance(url, str) else None


def _emails(payload: FlightPayload, nodes: list[object], *, endpoint: str) -> tuple[str, ...]:
    """Each ``mailto:`` link's address. Anything else in the section is ``RouteChanged``."""
    found: list[str] = []
    for node in nodes:
        for url, _ in _links(payload, node, endpoint=endpoint):
            if not url.lower().startswith("mailto:"):
                raise RouteChanged(endpoint, "an email link that is not mailto:")
            address = unquote(url[len("mailto:") :]).partition("?")[0].strip()
            if not _EMAIL.fullmatch(address) or len(address) > 320:
                raise RouteChanged(endpoint, "an email link whose address is not one")
            found.append(address)
    return tuple(dict.fromkeys(found))


def _websites(
    payload: FlightPayload, nodes: list[object], *, slug: str, endpoint: str
) -> tuple[str, ...]:
    """Each website link's site, unwrapped from LinkedIn's redirect.

    A link on LinkedIn's own host that is not one of the wrappers is a site a person
    listed (a company page, a newsletter, their own profile): LinkedIn does not wrap its
    own links. One that carries a ``url`` parameter on a path that is not a known wrapper
    is skipped, since which parameter is the site is unknown. A site, wrapped or not, that
    is this member's own profile is skipped too: the contact holds that already. So is a
    site that is not an absolute ``http`` or ``https`` url with a host (``javascript:``,
    ``data:``, a protocol-relative ``//host``): it would be stored as a link. Any other
    is kept.
    """
    found: list[str] = []
    skipped = not_web = 0
    for node in nodes:
        for url, _ in _links(payload, node, endpoint=endpoint):
            split = urlsplit(url)
            site: str | None = url
            if _on_linkedin(split.hostname):
                query = parse_qs(split.query)
                if split.path.rstrip("/") in REDIRECT_PATHS:
                    values = query.get(REDIRECT_PARAM, [])
                    site = values[0] if len(values) == 1 else None
                elif REDIRECT_PARAM in query:
                    skipped += 1
                    continue
            if site is None or not site.strip() or _has_control(site) or len(site) > MAX_TEXT:
                raise RouteChanged(endpoint, "a website link without a site in it")
            if any(ch.isspace() for ch in site.strip()):
                raise RouteChanged(endpoint, "a website that is not one url")
            inner = urlsplit(site.strip())
            if inner.scheme not in _SITE_SCHEMES or not inner.hostname:
                # #206 review: a site is stored as a link and rendered as an href, so
                # only an absolute http or https url with a host is one.
                not_web += 1
                continue
            if _on_linkedin(inner.hostname) and _is_own_profile(inner.path, slug):
                skipped += 1
                continue
            found.append(site.strip())
    if skipped:
        log.info("enrichment: skipped %d website link(s) to LinkedIn itself", skipped)
    if not_web:
        log.info("enrichment: skipped %d website link(s) that are not http or https", not_web)
    return tuple(dict.fromkeys(found))


def _on_linkedin(hostname: str | None) -> bool:
    host = (hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _is_own_profile(path: str, slug: str) -> bool:
    linked = profile_slug(path)
    return linked is not None and same_slug(linked, slug)


def _section_texts(payload: FlightPayload, node: object, *, endpoint: str) -> list[str]:
    """A section's own strings in page order, after its heading: the value's text."""
    texts: list[str] = []
    for child in payload.walk(node, follow=True, endpoint=endpoint):
        props = element_props(child)
        children = props.get("children") if props is not None else None
        if (
            isinstance(children, list)
            and not is_element(children)
            and props is not None
            and "action" not in props
        ):
            texts.extend(c for c in children if isinstance(c, str))
    return texts[1:]


def _soft_phones(payload: FlightPayload, nodes: list[object], *, endpoint: str) -> tuple[str, ...]:
    """Phone numbers (**invented** section): ``tel:`` links, else number-shaped text."""
    found: list[str] = []
    for node in nodes:
        for url, _ in _links(payload, node, endpoint=endpoint):
            if url.lower().startswith("tel:"):
                number = unquote(url[4:]).strip()
                if _PHONE.fullmatch(number):
                    found.append(number)
        if not found:
            for text in _section_texts(payload, node, endpoint=endpoint):
                number = " ".join(text.split())
                if _PHONE.fullmatch(number):
                    found.append(number)
    return tuple(dict.fromkeys(found))


def _soft_handles(payload: FlightPayload, nodes: list[object], *, endpoint: str) -> tuple[str, ...]:
    """Twitter/X handles (**invented** section): a link to the profile, else its text."""
    found: list[str] = []
    for node in nodes:
        for url, shown in _links(payload, node, endpoint=endpoint):
            split = urlsplit(url)
            host = (split.hostname or "").lower().removeprefix("www.")
            segment = split.path.strip("/")
            candidate = segment if host in ("twitter.com", "x.com") else shown.strip()
            match = _HANDLE.fullmatch(candidate)
            if match is not None and match.group(1).lower() not in ("i", "intent", "share", "home"):
                found.append(match.group(1))
    return tuple(dict.fromkeys(found))


def _soft_value(payload: FlightPayload, nodes: list[object], *, endpoint: str) -> str | None:
    """A one-value section's text (**invented** sections), or ``None``."""
    if len(nodes) != 1:
        return None
    texts = [
        t for t in (_text(s) for s in _section_texts(payload, nodes[0], endpoint=endpoint)) if t
    ]
    return " ".join(texts)[:MAX_TEXT] if texts else None


def _connected_since(payload: FlightPayload, *, endpoint: str) -> date | None:
    """The day after ``Connected since``, or ``None`` when it does not read."""
    strings: list[str] = []
    for node in payload.nodes(endpoint=endpoint):
        props = element_props(node)
        children = props.get("children") if props is not None else None
        if isinstance(children, list) and not is_element(children):
            strings.extend(c for c in children if isinstance(c, str))
    for index, text in enumerate(strings):
        if text.strip() == CONNECTED_SINCE and index + 1 < len(strings):
            return _day(" ".join(strings[index + 1].split()))
    return None


def _day(text: str) -> date | None:
    us, gb = _DAY_US.fullmatch(text), _DAY_GB.fullmatch(text)
    if us is not None:
        month, day, year = _month(us.group(1)), us.group(2), us.group(3)
    elif gb is not None:
        month, day, year = _month(gb.group(2)), gb.group(1), gb.group(3)
    else:
        return None
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None
