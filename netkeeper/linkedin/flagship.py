"""LinkedIn's ``flagship-web`` client: what its connections pages look like, and how to read them.

The capture in #149 (2026-09-24) showed that LinkedIn serves the connections list and
profiles through ``flagship-web``, a React Server Components client, not through the
Voyager REST API :mod:`netkeeper.linkedin.voyager` was written for. ADR 0006 records
the decision that followed: netkeeper reads the responses the page itself loads. This
module is the shape half of that: constants captured on 2026-09-24, and parsers from
a :class:`~netkeeper.linkedin.flight.FlightPayload` to plain dataclasses. The browser
half is :mod:`netkeeper.linkedin.page_connections`. ``docs/linkedin-flagship-web-shapes.md``
records every shape below, and the profile and contact-info shapes the enrichment
lane needs, as structure only.

**The connections list, as captured.**

* A full page load of ``/mynetwork/invite-connect/connections/`` answers with an HTML
  document whose ``<script id="rehydrate-data">`` assigns
  ``window.__como_rehydration__`` an array of strings: the first screen's flight
  payload, split into chunks. An in-app navigation to the same screen instead sends
  ``POST /flagship-web/mynetwork/invite-connect/connections`` and gets that payload
  back directly. Either carries the first ten cards, the total connection count, and
  the request for the next page.
* Scrolling near the end of the list makes the page send
  ``POST /flagship-web/rsc-action/actions/pagination`` itself. Its JSON body names the
  pager (``com.linkedin.sdui.pagers.mynetwork.connectionsList``; the ``/mynetwork``
  page's other pagers use the same endpoint), the ``startIndex``, and the list's sort
  (``sortByRecentlyAdded`` by default). Each answer carries ten cards and the request
  for the page after it. There is no per-page total.
* Each card is an element whose ``componentKey`` is ``ConnectionCard_<startIndex>-<slug>``.
  The card's rows carry a click trigger that seeds the profile screen's
  ``profile_name_loading_state`` and ``profile_headline_loading_state`` and navigates to
  ``com.linkedin.sdui.flagshipnav.profile.Profile`` with ``{vanityName, vieweeProfileId}``,
  and a text of ``Connected on <Month d, yyyy>``.

**Reading them.** A card becomes a :class:`~netkeeper.linkedin.voyager.ConnectionSummary`
only when every source of its identity agrees: one profile id, one slug, and that slug
equal to the one in its ``componentKey``. Anything else -- two ids in one card, a card
without a name, a date that does not parse, a start index the card does not carry --
refuses the whole payload with :class:`~netkeeper.linkedin.voyager.RouteChanged`. A
page is written whole or not at all: a partly understood page could put one person's
name on another's URN.

Pure: nothing here touches a browser or a database (spec 9.10).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from netkeeper.linkedin.flight import FlightPayload, element_props, parse_flight
from netkeeper.linkedin.voyager import ConnectionSummary, RouteChanged

# --- constants, captured 2026-09-24 (#149) ------------------------------------------

#: The only origin a live read runs against. Fixed, never configurable; the loopback
#: exception in :mod:`netkeeper.linkedin.page_connections` exists for the smoke suite.
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

#: The connections page a person opens. Navigated to, never fetched.
CONNECTIONS_PAGE_PATH: Final = "/mynetwork/invite-connect/connections/"  # captured 2026-09-24

#: The same screen's flight payload, when the client navigates to it in-app.
CONNECTIONS_SCREEN_PATH: Final = (
    "/flagship-web/mynetwork/invite-connect/connections"  # captured 2026-09-24
)

#: Where the page asks for the next page of a list, for every pager on ``/mynetwork``.
PAGINATION_PATH: Final = "/flagship-web/rsc-action/actions/pagination"  # captured 2026-09-24

#: Where the page asks for an overlay screen, such as contact info (enrichment's, next).
NAVIGATION_PATH: Final = "/flagship-web/rsc-action/actions/navigation"  # captured 2026-09-24

#: The connections list's pager. The ``/mynetwork`` page's other pagers (the
#: "people you may know" cohorts) answer on the same path and are not ours.
CONNECTIONS_PAGER_ID: Final = "com.linkedin.sdui.pagers.mynetwork.connectionsList"

#: The ``$type`` of the next-page request a page answer carries.
PAGINATION_REQUEST_TYPE: Final = "proto.sdui.actions.requests.PaginationRequest"

#: The profile screen a card's click navigates to, with the card's identity.
PROFILE_SCREEN_ID: Final = "com.linkedin.sdui.flagshipnav.profile.Profile"

#: The contact-info overlay's screen, for the enrichment lane (ADR 0006's one click).
CONTACT_DETAILS_SCREEN_ID: Final = (
    "com.linkedin.sdui.flagshipnav.profile.ProfileContactDetailsOverlay"
)

#: A card's ``componentKey``: the page's start index and the member's slug.
CARD_KEY_PREFIX: Final = "ConnectionCard_"
_CARD_KEY: Final = re.compile(r"ConnectionCard_(\d{1,7})-(.+)")

#: The state ids a card's click seeds on the profile screen: its name and headline.
NAME_STATE_ID: Final = "profile_name_loading_state"
HEADLINE_STATE_ID: Final = "profile_headline_loading_state"

#: The connections page's own count of the list, a model state on the first screen.
TOTAL_STATE_ID: Final = "totalConnectionsCount"

#: The list's sort, as the page sends it in every pagination request's ``states``.
SORT_STATE_ID: Final = "connectionsListSortOption"
SORT_NEWEST_FIRST: Final = "sortByRecentlyAdded"

#: How each card says when the connection was made, in the en-US interface.
CONNECTED_ON_PREFIX: Final = "Connected on "

#: The script element and global a full page load carries its first payload in.
REHYDRATION_SCRIPT_ID: Final = "rehydrate-data"
REHYDRATION_GLOBAL: Final = "__como_rehydration__"

#: Cards a full page of the list carries. A page with fewer and no request for the
#: next is the list's end; see :class:`ConnectionsChunk`.
FULL_PAGE: Final = 10

#: A profile id, the part after ``urn:li:fsd_profile:``. The captured ones are 39
#: characters of ``[A-Za-z0-9_-]`` starting ``ACo``; the bounds are looser than that
#: so an id LinkedIn lengthens still reads, and tight enough that a url or a name never
#: does.
_PROFILE_ID: Final = re.compile(r"[A-Za-z0-9_-]{8,100}")

#: A slug, as the connections card and LinkedIn's routing carry it. Refuses what could
#: not be one path segment of ``/in/<slug>/``.
_SLUG: Final = re.compile(r"[^\s/?#]{1,100}")

URN_PREFIX: Final = "urn:li:fsd_profile:"

_MONTHS: Final[dict[str, int]] = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}
_CONNECTED_ON: Final = re.compile(r"Connected on ([A-Z][a-z]+) (\d{1,2}), (\d{4})")

#: Unicode's own line and paragraph separators, which ``str.split`` would quietly fold.
_LINE_SEPARATORS: Final = "\u2028\u2029"

#: The longest a display name may be before it is not a name.
MAX_NAME_LENGTH: Final = 200

SCREEN_ENDPOINT: Final = "flagship-web/connections"
PAGINATION_ENDPOINT: Final = "flagship-web/pagination"


# --- the first screen, from a full page load ---------------------------------------


_SCRIPT: Final = re.compile(
    r"<script\b[^>]*\bid\s*=\s*[\"']"
    + re.escape(REHYDRATION_SCRIPT_ID)
    + r"[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT: Final = re.compile(
    r"\s*window\s*\.\s*" + re.escape(REHYDRATION_GLOBAL) + r"\s*=\s*(\[.*\])\s*;?\s*",
    re.DOTALL,
)


def rehydration_payload(document: str, *, endpoint: str = SCREEN_ENDPOINT) -> bytes | None:
    """The flight payload a full page load carries, or ``None`` when the document has none.

    ``None`` means no ``rehydrate-data`` script at all -- a page that loads its first
    screen some other way, or a wall. A script that is there but not the captured
    shape (not one assignment of an array of strings) is :class:`RouteChanged`.
    """
    match = _SCRIPT.search(document)
    if match is None:
        return None
    assignment = _ASSIGNMENT.fullmatch(match.group(1))
    if assignment is None:
        raise RouteChanged(endpoint, "the rehydrate-data script is not one array assignment")
    try:
        chunks = json.loads(assignment.group(1))
    except (json.JSONDecodeError, RecursionError):
        raise RouteChanged(endpoint, "the rehydrate-data array is not JSON") from None
    if not isinstance(chunks, list) or not all(isinstance(chunk, str) for chunk in chunks):
        raise RouteChanged(endpoint, "the rehydrate-data array is not a list of strings")
    return "".join(chunks).encode("utf-8")


# --- the page's own request ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaginationRequest:
    """What the page asked the pagination endpoint for: which pager, from where, sorted how."""

    pager_id: str
    start_index: int
    sort: str | None


def parse_pagination_request(body: str | None) -> PaginationRequest | None:
    """The page's pagination request, or ``None`` when it is not the connections list's.

    The ``/mynetwork`` page's other pagers post to the same path: those are ``None``,
    so a caller skips them. A connections-list request without an integer
    ``startIndex`` is :class:`RouteChanged`: it is ours, and it is not the shape the
    capture showed.
    """
    endpoint = PAGINATION_ENDPOINT
    if body is None:
        raise RouteChanged(endpoint, "a pagination request with no body")
    try:
        request = json.loads(body)
    except (json.JSONDecodeError, RecursionError):
        raise RouteChanged(endpoint, "a pagination request body that is not JSON") from None
    if not isinstance(request, dict):
        raise RouteChanged(endpoint, "a pagination request body that is not an object")
    if request.get("pagerId") != CONNECTIONS_PAGER_ID:
        return None
    arguments = request.get("clientArguments")
    payload = arguments.get("payload") if isinstance(arguments, dict) else None
    start = payload.get("startIndex") if isinstance(payload, dict) else None
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise RouteChanged(endpoint, "a connections pagination request without a startIndex")
    sort: str | None = None
    states = arguments.get("states") if isinstance(arguments, dict) else None
    if isinstance(states, list):
        for state in states:
            if isinstance(state, dict) and state.get("key") == SORT_STATE_ID:
                value = state.get("value")
                sort = value if isinstance(value, str) else None
    return PaginationRequest(pager_id=CONNECTIONS_PAGER_ID, start_index=start, sort=sort)


# --- a page of the list -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionsChunk:
    """One answer's worth of the connections list: the first screen, or one pagination page.

    ``start`` is the index of the first card (the ``<n>`` in each card's
    ``ConnectionCard_<n>-<slug>``; the page's own start when it has no card).
    ``next_start`` is the ``startIndex`` of the request the answer carries for the
    next page, or ``None`` when it carries none. ``total`` is the connection count the
    first screen states, or ``None``; pagination answers carry none.
    """

    cards: tuple[ConnectionSummary, ...]
    start: int
    next_start: int | None
    total: int | None

    @property
    def ends_list(self) -> bool:
        """Whether this answer proves the list ends here.

        An answer with no cards does; so does a short answer (fewer than
        :data:`FULL_PAGE` cards) that asks for no next page. A full page with no next
        request does not: a list whose length is a multiple of ten could end there
        or not, and only the next answer -- or its absence, which proves nothing --
        could say.
        """
        if not self.cards:
            return True
        return self.next_start is None and len(self.cards) < FULL_PAGE


def parse_connections_chunk(
    body: bytes | str, *, endpoint: str, expected_start: int
) -> ConnectionsChunk:
    """Parse one connections-list answer, all of it or none of it.

    ``expected_start`` is the start index this answer must carry: the page asked for
    it (a pagination request's ``startIndex``), or it is the first screen (0). A card
    keyed for any other start is :class:`RouteChanged`.

    An answer with no cards must be *empty*, not merely unreadable: if it mentions a
    connection card or a profile id anywhere, and yet no card parsed, the shape has
    changed and this raises rather than report the end of the list.
    """
    payload = parse_flight(body, endpoint=endpoint)
    cards = _cards(payload, endpoint=endpoint, expected_start=expected_start)
    if not cards:
        raw = body if isinstance(body, bytes) else body.encode("utf-8")
        if CARD_KEY_PREFIX.encode() in raw or b"vieweeProfileId" in raw:
            raise RouteChanged(endpoint, "an answer that mentions cards, but none could be read")
    next_start = _next_start(payload, endpoint=endpoint)
    if next_start is not None and next_start <= expected_start:
        raise RouteChanged(endpoint, "the next page's start does not move past this page")
    return ConnectionsChunk(
        cards=cards,
        start=expected_start,
        next_start=next_start,
        total=_total(payload, endpoint=endpoint),
    )


def _cards(
    payload: FlightPayload, *, endpoint: str, expected_start: int
) -> tuple[ConnectionSummary, ...]:
    """Every card in ``payload``, in page order, each read whole or the payload refused."""
    found: list[tuple[str, object]] = []
    for node in payload.nodes(endpoint=endpoint):
        props = element_props(node)
        if props is None:
            continue
        key = props.get("componentKey")
        if isinstance(key, str) and key.startswith(CARD_KEY_PREFIX):
            found.append((key, node))
    cards: list[ConnectionSummary] = []
    keys: set[str] = set()
    urns: set[str] = set()
    for index, (key, node) in enumerate(found):
        match = _CARD_KEY.fullmatch(key)
        if match is None:
            raise RouteChanged(endpoint, f"card {index}: a componentKey without a start and slug")
        if int(match.group(1)) != expected_start:
            raise RouteChanged(endpoint, f"card {index}: keyed for a different page than asked")
        if key in keys:
            raise RouteChanged(endpoint, f"card {index}: the same card twice in one answer")
        keys.add(key)
        card = _card(payload, node, slug=match.group(2), endpoint=endpoint, index=index)
        if card.urn in urns:
            raise RouteChanged(endpoint, f"card {index}: two cards for one profile id")
        urns.add(card.urn or "")
        cards.append(card)
    return tuple(cards)


def _card(
    payload: FlightPayload, node: object, *, slug: str, endpoint: str, index: int
) -> ConnectionSummary:
    """One card, from every row it refers to."""
    identities: set[tuple[object, object]] = set()
    names: set[str] = set()
    headlines: set[str] = set()
    connected: set[str] = set()
    for child in payload.walk(node, follow=True, endpoint=endpoint):
        if isinstance(child, dict):
            if "vieweeProfileId" in child:
                identities.add((child.get("vieweeProfileId"), child.get("vanityName")))
            state = _set_state(child)
            if state is not None:
                state_id, value = state
                if state_id == NAME_STATE_ID:
                    names.add(value)
                elif state_id == HEADLINE_STATE_ID:
                    headlines.add(value)
        elif isinstance(child, str) and child.startswith(CONNECTED_ON_PREFIX):
            connected.add(child)
    where = f"card {index}"
    if len(identities) != 1:
        raise RouteChanged(endpoint, f"{where}: {len(identities)} profile identities, not one")
    ((profile_id, vanity),) = identities
    if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
        raise RouteChanged(endpoint, f"{where}: a profile id that is not the captured shape")
    if not isinstance(vanity, str) or vanity != slug or not _SLUG.fullmatch(vanity):
        raise RouteChanged(endpoint, f"{where}: the slug does not match the card's key")
    if len(names) != 1:
        raise RouteChanged(endpoint, f"{where}: {len(names)} names, not one")
    first, last = split_display_name(names.pop(), endpoint=endpoint, where=where)
    if len(headlines) > 1:
        raise RouteChanged(endpoint, f"{where}: {len(headlines)} headlines, not one")
    headline = " ".join(headlines.pop().split()) if headlines else None
    if len(connected) > 1:
        raise RouteChanged(endpoint, f"{where}: {len(connected)} connected-on dates, not one")
    connected_on = _connected_on(connected.pop(), endpoint, where) if connected else None
    return ConnectionSummary(
        urn=f"{URN_PREFIX}{profile_id}",
        public_id=vanity,
        first_name=first,
        last_name=last,
        headline=headline or None,
        connected_at=None,
        connected_on=connected_on,
    )


def _set_state(node: dict[str, object]) -> tuple[str, str] | None:
    """``(state id, string value)`` when ``node`` is a SetState action's ``value``; else ``None``.

    The captured shape: ``{"state": {"key": {"key": {"value": {"$case": "id", "id": <id>}},
    "namespace": ...}, "value": {"$case": "stringValue", "stringValue": <text>}}}``.
    """
    state = node.get("state")
    if not isinstance(state, dict):
        return None
    key = state.get("key")
    inner = key.get("key") if isinstance(key, dict) else None
    ident = inner.get("value") if isinstance(inner, dict) else None
    state_id = ident.get("id") if isinstance(ident, dict) else None
    value = state.get("value")
    text = value.get("stringValue") if isinstance(value, dict) else None
    if isinstance(state_id, str) and isinstance(text, str):
        return state_id, text
    return None


def split_display_name(name: str, *, endpoint: str, where: str) -> tuple[str, str]:
    """A card's one display name as ``(first, last)``: split at the first space.

    The card carries one string ("Jane Q. Doe, PMP"), where Voyager gave two. The
    split is the one :mod:`netkeeper.linkedin.dom` already uses, so both sources
    agree; :func:`netkeeper.crm.apply.apply_page` keeps a contact's existing split
    when the two join to the same name, so a sync never reshuffles a name an import
    split differently. Whitespace runs collapse to one space. An empty name, one
    with a control character or a line break, or one past :data:`MAX_NAME_LENGTH`
    is :class:`RouteChanged`: a card's name is the server's own text, and text that
    is not a name means the shape moved.
    """
    text = name
    if any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 or ch in _LINE_SEPARATORS for ch in text):
        raise RouteChanged(endpoint, f"{where}: a name with a control character")
    normalized = " ".join(text.split())
    if not normalized or len(normalized) > MAX_NAME_LENGTH:
        raise RouteChanged(endpoint, f"{where}: a name that is empty or too long")
    first, _, last = normalized.partition(" ")
    return first, last


def _connected_on(text: str, endpoint: str, where: str) -> date:
    match = _CONNECTED_ON.fullmatch(" ".join(text.split()))
    month = _MONTHS.get(match.group(1)) if match else None
    if match is None or month is None:
        raise RouteChanged(endpoint, f"{where}: a connected-on date that is not the captured shape")
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        raise RouteChanged(endpoint, f"{where}: a connected-on date that does not exist") from None


def _next_start(payload: FlightPayload, *, endpoint: str) -> int | None:
    """The ``startIndex`` of the connections-list request this answer carries, if any.

    The captured answer carries it as a JSON *string* holding a ``PaginationRequest``;
    an object of the same ``$type`` is read too. Two different starts are
    :class:`RouteChanged`.
    """
    starts: set[int] = set()
    for node in payload.nodes(endpoint=endpoint):
        request: object = node
        if isinstance(node, str) and PAGINATION_REQUEST_TYPE in node and node.startswith("{"):
            try:
                request = json.loads(node)
            except (json.JSONDecodeError, RecursionError):
                raise RouteChanged(endpoint, "a pagination request that is not JSON") from None
        if not isinstance(request, dict) or request.get("$type") != PAGINATION_REQUEST_TYPE:
            continue
        if request.get("pagerId") != CONNECTIONS_PAGER_ID:
            continue
        arguments = request.get("requestedArguments")
        body = arguments.get("payload") if isinstance(arguments, dict) else None
        start = body.get("startIndex") if isinstance(body, dict) else None
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise RouteChanged(endpoint, "a next-page request without a startIndex")
        starts.add(start)
    if len(starts) > 1:
        raise RouteChanged(endpoint, "more than one next page")
    return starts.pop() if starts else None


def _total(payload: FlightPayload, *, endpoint: str) -> int | None:
    """The first screen's ``totalConnectionsCount`` model state, if it carries one."""
    totals: set[int] = set()
    for node in payload.nodes(endpoint=endpoint):
        if not isinstance(node, dict):
            continue
        key = node.get("key")
        inner = key.get("key") if isinstance(key, dict) else None
        ident = inner.get("value") if isinstance(inner, dict) else None
        if not isinstance(ident, dict) or ident.get("id") != TOTAL_STATE_ID:
            continue
        value = node.get("value")
        if not isinstance(value, dict) or "intValue" not in value:
            continue  # the card menus' "total - 1" expressions, not a count
        count = value.get("intValue")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise RouteChanged(endpoint, "a total connection count that is not a count")
        totals.add(count)
    if len(totals) > 1:
        raise RouteChanged(endpoint, "more than one total connection count")
    return totals.pop() if totals else None
