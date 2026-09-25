"""LinkedIn's undocumented Voyager REST API: headers, endpoints, and parsers.

Spec 9.2 (data sources) and 9.3 ("In-page API first, DOM second"); ADR 0002
(attach-only browser mode); ADR 0005 (extractor boundary). Pure module, no
network calls: nothing here opens a socket, and nothing here imports
:mod:`netkeeper.models` or opens a database session (spec 9.10).

Voyager sits under ``/voyager/api/`` on ``linkedin.com`` and is what the web
client itself talks to. It is not documented anywhere LinkedIn maintains, and
its paths, query shapes, and ``decorationId`` values change without notice.
Spec 9.3's answer: keep every one of those constants in this one module, each
annotated with the date someone captured it from a real DevTools session, and
make every parser defensive enough that an endpoint LinkedIn has changed
degrades the run (:class:`RouteChanged`) instead of crashing it.

**On the constants below and "capture date":** the endpoint paths,
``decorationId`` values, and header names in this module were authored on the
date in their comment from the general, publicly documented shape of
Voyager's REST API (the rest.li 2.0.0 collection envelope of ``elements`` +
``paging``, and LinkedIn's normalized ``data``/``included`` cross-referencing)
rather than copied from a live
DevTools capture — this task has no access to a logged-in LinkedIn session and
must not fetch anything from linkedin.com (see "Capturing and sanitizing
fixtures" below). Treat every constant here as a starting point that the first
real run must verify and correct following that same procedure, updating the
capture-date comment when it does. The defensive parsing in this module is
what makes that safe: a wrong guess here degrades to :class:`RouteChanged`
rather than corrupting data.

**No fetch helper here, and none anywhere now.** This module defines
:class:`VoyagerFetch`, a callable protocol, and every parser takes the response
body (``str``) rather than a browser object. P2-01's in-page fetch once
implemented it; ADR 0006 retired that (#187, #190): netkeeper reads what the
page loads and sends no request of its own.

Capturing and sanitizing fixtures
----------------------------------

This is the procedure a human follows later, with DevTools, against their own
logged-in session, to refresh the fixtures under ``tests/fixtures/voyager/``
or to correct one of this module's endpoint constants after LinkedIn changes
something. It is documentation for that future session, not a test to run now
— nothing in this repository's test suite may contact linkedin.com (CLAUDE.md,
CONTRIBUTING.md), and following this procedure never means pointing a test at
the live site.

1. Open ``linkedin.com`` in a normal, logged-in Chrome tab (the dedicated
   netkeeper profile from ADR 0002, or your everyday one — this step is a
   human at the keyboard, not the sidecar). Open DevTools, Network tab,
   filter on ``voyager``.
2. Trigger the request: scroll the connections list for a connections page,
   open the messaging inbox for conversations. (Profiles and contact info are
   read from what the page loads since #190; see
   ``docs/linkedin-flagship-web-shapes.md``.)
3. Copy the request as cURL or inspect it directly to record: the path, the
   query string (note the ``decorationId`` value), and the request headers.
   Update the matching constant in this module with today's date in its
   comment.
4. Copy the **response body** (Response tab, "Copy response"). This is where
   sanitization happens, before the data leaves your clipboard for any file
   in this repository:
   - Replace every person's first and last name with an invented one (keep
     the same one consistently within a single fixture, the way a real
     conversation would use one name throughout).
   - Replace every ``entityUrn`` / URN-shaped value and every
     ``publicIdentifier`` with an invented one of the same shape
     (``urn:li:fsd_profile:ACoAA...`` with a made-up id;
     ``jamie-fake-rivera-1a2b3c4d`` style slugs). Keep the *shape* — the
     parser tests care about that — never the real value.
   - Delete or replace every email address, phone number, and physical
     address with an invented one. Never carry a real email or phone number
     into a fixture, a test, a log line, an issue, a PR body, or a commit
     message — that rule has no exceptions.
   - Replace every company name that would identify a real employer with an
     invented one that reads the same shape (a two-to-three word company-like
     name), *unless* the real name is already a public example used
     throughout this repo's own docs and fixtures (there are none as of this
     writing; when in doubt, invent one).
   - Trim the body to the smallest shape that still exercises the field the
     fixture is for — a connections page fixture needs a couple of elements,
     not forty.
   - Re-indent with a formatter (``python -m json.tool``) so the diff a
     reviewer sees is readable.
   - Never copy anything from ``~/code/netkeeper-private`` into a fixture,
     ever, sanitized or not — that directory holds a real export and nothing
     in it is safe to launder through "sanitization" into this repository.
5. Save the sanitized body under ``tests/fixtures/voyager/<name>.json`` and
   write or update the fixture test that loads it.
6. If the shape differs from what the parser in this module expects, fix the
   parser, not the fixture — the fixture is the source of truth for what
   LinkedIn actually sent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final, Protocol, runtime_checkable

log = logging.getLogger(__name__)


# =============================================================================
# RouteChanged
# =============================================================================


class RouteChanged(Exception):
    """A Voyager response did not match the shape its parser expects.

    Spec 9.7: "HTTP 200 with an unrecognized shape, or 400 on a known
    endpoint" classifies as ``RouteChanged``, and the run gives up on that
    endpoint rather than retrying or crashing. This module is the other half
    of that rule: every parser below raises this — never ``KeyError``,
    ``TypeError``, or ``json.JSONDecodeError`` — for a missing key, a null
    where an object was expected, a list where a scalar was expected, an
    empty response, or a response that is valid JSON but a completely
    different document.

    ``endpoint`` names what was being parsed (for example
    ``"relationships/dash/connections"``); ``detail`` names what was missing
    or wrong, in enough detail to fix the parser without a repro. Message
    bodies passed to parsers may contain a real person's data (spec: never
    logged), so ``detail`` describes *shape*, never field values.

    Every instance logs itself at warning level on construction — spec 9.7's
    "log loudly" — so a caller that lets this propagate still gets one record
    of what changed, without having to remember to log it itself.
    """

    def __init__(self, endpoint: str, detail: str) -> None:
        self.endpoint = endpoint
        self.detail = detail
        super().__init__(f"{endpoint}: {detail}")
        log.warning("route changed: %s: %s", endpoint, detail)


# =============================================================================
# Header builder (spec 9.3)
# =============================================================================

#: The JSON representation the real web client requests. Voyager's plain
#: ``application/json`` still answers, but the client's own requests carry
#: this, and matching it is part of not looking like a bot.
ACCEPT_HEADER: Final = "application/vnd.linkedin.normalized+json+2.1"  # captured 2026-09-22

#: rest.li protocol version, sent on every Voyager request (spec 9.3, verbatim).
RESTLI_PROTOCOL_VERSION: Final = "2.0.0"  # captured 2026-09-22

#: The header name the CSRF token rides in. Not "x-csrf-token": Voyager's own name.
CSRF_HEADER_NAME: Final = "csrf-token"

#: Interface locale header the client always sends. Low-risk to hardcode: it
#: does not vary per request the way ``x-li-track`` does.
LANG_HEADER_VALUE: Final = "en_US"  # captured 2026-09-22


def strip_jsessionid(raw_cookie_value: str) -> str:
    """Return the bare csrf token from a ``JSESSIONID`` cookie's value.

    LinkedIn's ``JSESSIONID`` cookie is quoted (``"ajax:1234567890123456789"``)
    but the ``csrf-token`` header wants the value bare, without the quotes
    (spec 9.3). That stripping is the one part of this function worth a test
    on its own: a header built with the quotes still attached is rejected by
    the API as a CSRF mismatch, silently from the caller's point of view — it
    looks like an auth failure, not a formatting bug.

    A value with no surrounding quotes (a future LinkedIn that stops quoting
    it, or a caller that already stripped it) passes through unchanged, so
    this is always safe to call.

    Only the outer pair of quotes comes off, deliberately: this undoes the
    cookie's own quoting, once, and does not scan for or strip a quote
    character anywhere else in the value. A value like ``'"ajax:12"34"'``
    becomes ``'ajax:12"34'`` — the inner ``"`` stays, because nothing says
    the token itself cannot contain one, and this function's job is "the
    cookie is no longer quoted", not "the value has no quotes in it".
    """
    value = raw_cookie_value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def build_headers(
    jsessionid_cookie_value: str, *, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Build the header set the real Voyager web client sends with an XHR.

    ``jsessionid_cookie_value`` is the raw ``JSESSIONID`` cookie value,
    quotes and all, exactly as a ``document.cookie`` read or a CDP cookie jar
    hands it back — this function strips the quotes itself (see
    :func:`strip_jsessionid`), so no caller has to remember to.

    ``extra`` merges in headers this function does not know a fixed value
    for and should not guess — most notably ``x-li-track``, a JSON blob
    naming the client version, OS, timezone, and device form factor that
    varies per browser and per LinkedIn deploy, and ``x-li-page-instance``,
    which the real client mints per page load. P2-01's fetch helper, running
    inside the actual page, is the right place to read those from the live
    client rather than this module hardcoding a guess that goes stale
    immediately. ``extra`` overrides this function's own headers on key
    collision, so a caller can also override ``accept`` for an endpoint that
    wants something else.

    Raises :class:`ValueError` if ``jsessionid_cookie_value`` is empty —
    building a request with a blank csrf token is never useful and the
    failure is much clearer here than as a 401 three network hops away.
    """
    if not jsessionid_cookie_value.strip():
        raise ValueError("jsessionid_cookie_value is empty; cannot build the csrf-token header")
    headers: dict[str, str] = {
        "accept": ACCEPT_HEADER,
        CSRF_HEADER_NAME: strip_jsessionid(jsessionid_cookie_value),
        "x-restli-protocol-version": RESTLI_PROTOCOL_VERSION,
        "x-li-lang": LANG_HEADER_VALUE,
    }
    if extra:
        headers.update(extra)
    return headers


# =============================================================================
# The fetch-helper interface (P2-01 satisfies this; nothing in this module does)
# =============================================================================


@dataclass(frozen=True, slots=True)
class VoyagerRequest:
    """One in-page Voyager fetch, fully assembled and ready to send.

    ``path`` is one of this module's endpoint path constants (or one built
    from a ``*_path`` function below). ``query`` comes from this module's
    ``*_query`` functions. ``headers`` is the output of :func:`build_headers`
    plus whatever runtime headers the caller merged in through its ``extra``.
    Nothing here is browser- or CDP-shaped: P2-01's provider turns this into
    an in-page ``fetch(...)`` call, but this module never needs to know how.
    """

    path: str
    query: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VoyagerResponse:
    """What a Voyager fetch returns, independent of how it was fetched.

    ``status`` is the HTTP status the in-page ``fetch`` saw. ``body`` is the
    response text, completely undecoded: every parser in this module does its
    own ``json.loads`` and shape validation, so a body that is not JSON at
    all — an HTML checkpoint or login page (spec 9.7) — still reaches
    :class:`RouteChanged` from the right place rather than raising out of the
    fetch helper itself. ``final_url`` is the response's URL after redirects,
    which is what :mod:`netkeeper.linkedin.classify` (P2-03) needs to notice
    a redirect to ``/checkpoint/`` or ``/authwall``.

    This module never logs ``body`` — it can contain a real person's name,
    email, or message text (CLAUDE.md: message bodies, cookies, and tokens
    never appear in logs). A :class:`RouteChanged` message describes shape,
    never field values, for the same reason.
    """

    status: int
    body: str
    final_url: str


@runtime_checkable
class VoyagerFetch(Protocol):
    """An async callable: a :class:`VoyagerRequest` in, a :class:`VoyagerResponse` out.

    Nothing in production implements this any more. P2-01's in-page fetch
    (``linkedin/fetch.py``) did, until ADR 0006 retired it: #187 for the
    connections list and #190 for enrichment, since netkeeper now sends no
    request of its own. It stays as the shape the parsers below were written
    against: they only ever see a :class:`VoyagerResponse`'s body, never a
    ``Page`` or any other Playwright/CDP type (spec 9.10).
    """

    async def __call__(self, request: VoyagerRequest) -> VoyagerResponse: ...


# =============================================================================
# Shape-validation helpers
#
# Every field a parser below reads comes through one of these. That is
# deliberate: a parser that reaches into ``dict`` with ``d["foo"]["bar"]``
# fails this item's "done when" the moment a fixture omits ``foo``. Going
# through ``_field``/``_optional_field`` instead means the *only* way to
# produce a bare ``KeyError`` is to stop using them, which is exactly the
# mutation this item asks to be provable against (CLAUDE.md's review
# standard): swap one of these for a bare subscript and the malformed-input
# test for that field must fail loudly, not pass.
# =============================================================================


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _expect[T](endpoint: str, where: str, value: object, expected: type[T]) -> T:
    """Return ``value`` if it is exactly an instance of ``expected``.

    ``bool`` is deliberately never accepted where ``int`` or ``float`` is
    expected: Python's ``isinstance(True, int)`` is ``True``, but a JSON
    ``true`` in a field like ``count`` is a shape LinkedIn's own client would
    never send, and letting it through would turn a real API change into a
    silent ``1``.
    """
    if expected in (int, float) and isinstance(value, bool):
        raise RouteChanged(endpoint, f"{where}: expected {expected.__name__}, got bool")
    if isinstance(value, expected):
        return value
    raise RouteChanged(endpoint, f"{where}: expected {expected.__name__}, got {_type_name(value)}")


def _field[T](endpoint: str, obj: Mapping[str, object], key: str, expected: type[T]) -> T:
    """Require ``key`` in ``obj`` with type ``expected``; ``RouteChanged`` otherwise.

    Covers both failure modes a missing field can take: the key absent
    entirely, and the key present but ``null`` (``obj.get(key)`` would return
    ``None`` for both, so this checks membership first rather than defaulting).
    """
    if key not in obj:
        raise RouteChanged(endpoint, f"missing '{key}'")
    return _expect(endpoint, f"'{key}'", obj[key], expected)


def _optional_field[T](
    endpoint: str, obj: Mapping[str, object], key: str, expected: type[T]
) -> T | None:
    """Like :func:`_field`, but absent or ``null`` is a legitimate value: ``None``.

    For fields LinkedIn only sometimes populates because the person did not
    fill them in (an email address, a headline) — not for structural fields
    like ``paging`` or ``elements``, which always use :func:`_field`.
    """
    value = obj.get(key)
    if value is None:
        return None
    return _expect(endpoint, f"'{key}'", value, expected)


def _epoch_millis(endpoint: str, key: str, value: int) -> datetime:
    """Convert a Voyager epoch-milliseconds timestamp to an aware UTC ``datetime``."""
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise RouteChanged(endpoint, f"'{key}': not a valid timestamp ({exc})") from None


def _load_json(endpoint: str, body: str) -> object:
    """Parse ``body`` as JSON, turning "not JSON at all" into ``RouteChanged``.

    Covers the empty-response case explicitly (an empty string is not valid
    JSON and ``json.loads`` would raise ``JSONDecodeError``, but the message
    is clearer written for the case this item calls out by name) and a
    response that fails to parse for any other reason — a checkpoint page's
    HTML body, most often (spec 9.7 handles that redirect-and-challenge case
    ahead of this module via :mod:`netkeeper.linkedin.classify`; this is the
    fallback for when it slips through as a 200 anyway).
    """
    if not body.strip():
        raise RouteChanged(endpoint, "empty response body")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RouteChanged(endpoint, f"response body is not valid JSON ({exc})") from None


def _index_included(endpoint: str, included_raw: list[object]) -> dict[str, dict[str, object]]:
    """Index a normalized response's ``included`` array by ``entityUrn``.

    LinkedIn's ``application/vnd.linkedin.normalized+json+2.1`` responses
    factor cross-referenced entities (a connection's profile, a message's
    sender) out of ``data.elements`` into a flat ``included`` array, and
    elements refer to them by URN string. This is the shared lookup every
    parser that reads ``included`` uses.
    """
    index: dict[str, dict[str, object]] = {}
    for i, item in enumerate(included_raw):
        entity = _expect(endpoint, f"included[{i}]", item, dict)
        urn = _field(endpoint, entity, "entityUrn", str)
        index[urn] = entity
    return index


def _resolve_included(
    endpoint: str, included: Mapping[str, dict[str, object]], where: str, ref: str
) -> dict[str, object]:
    entity = included.get(ref)
    if entity is None:
        raise RouteChanged(endpoint, f"{where}: no 'included' entity for '{ref}'")
    return entity


# =============================================================================
# Connections list (spec 9.2 "Connections list"; 9.4 "Connections full/incremental sync")
# =============================================================================

CONNECTIONS_ENDPOINT: Final = "relationships/dash/connections"
CONNECTIONS_PATH: Final = "/voyager/api/relationships/dash/connections"  # captured 2026-09-22

#: captured 2026-09-22. Selects the fields the connections list page needs:
#: URN, public id, name, headline, picture, connected-on (spec 9.2's row).
CONNECTIONS_DECORATION_ID: Final = (
    "com.linkedin.voyager.dash.deco.web.mynetwork.ConnectionListWithProfile-16"
)

#: Page size. Matches spec 9.2's "about 40 contacts per request".
CONNECTIONS_DEFAULT_COUNT: Final = 40


def connections_query(*, start: int, count: int = CONNECTIONS_DEFAULT_COUNT) -> dict[str, str]:
    """Query parameters for one page of the connections list.

    ``start`` is the zero-based offset (spec 9.4: full sync paginates to the
    end; incremental sync pages newest-first and stops at the first page of
    already-known URNs — both are callers of this function, differing only in
    how many pages they fetch and when they stop, not in the query shape).
    """
    if start < 0:
        raise ValueError("start must not be negative")
    if count <= 0:
        raise ValueError("count must be positive")
    return {
        "decorationId": CONNECTIONS_DECORATION_ID,
        "start": str(start),
        "count": str(count),
        "q": "search",
        "sortType": "RECENTLY_ADDED",
    }


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """One connections-list row: enough to sync an edge (spec 9.2, 9.8).

    ``headline`` and ``connected_at`` are the two fields LinkedIn does not
    always populate (a fresh connection can be missing a headline; an older
    export-era connection can be missing ``createdAt``) and so are optional.
    ``public_id`` and both names are required: a connections-list row this
    module cannot read a public id or a name out of is not a connections-list
    row, from either source below. The DOM fallback reports both names empty
    for a card whose name text spans lines (the name and the occupation run
    together, #184): the card is still a sighting of its slug.

    ``urn`` is optional for a different reason (P2-08): this endpoint's own
    parser, :func:`parse_connections_page`, always fills it from
    ``entityUrn`` and never produces ``None`` -- but LinkedIn's internal URN
    is not printed anywhere in the connections list page's rendered HTML, so
    P2-08's scroll-driven DOM fallback (since removed) had no honest way
    to read one. ``None`` there is not "unknown, guess"; it is "this source
    cannot supply a URN". :func:`netkeeper.crm.apply.apply_page` never sends
    such a row through identity resolution: it marks the contact holding the
    slug seen, or, when nobody holds it, creates one contact marked needs
    review (#184) that a later row with a URN confirms. Never invent a URN
    here to fill the gap: a fabricated URN would either match nothing
    (creating a duplicate contact) or, worse, collide with a real one by
    accident.
    """

    urn: str | None
    public_id: str
    first_name: str
    last_name: str
    headline: str | None
    connected_at: datetime | None
    #: The day the connection was made, when the source states a day rather than an
    #: instant: the flagship-web card's "Connected on <Month d, yyyy>" is the day
    #: LinkedIn shows the account owner, already in their zone, so turning it into a
    #: midnight-UTC instant would move it a day for anyone west of Greenwich.
    #: :func:`netkeeper.crm.apply.apply_page` prefers it over ``connected_at``.
    connected_on: date | None = None


@dataclass(frozen=True, slots=True)
class ConnectionsPageResult:
    """One fetched page of the connections list, with the paging the caller needs.

    ``start``/``count``/``total`` are ``paging``'s fields verbatim: a caller
    keeps paging while ``start + count < total`` (full sync) or until it sees
    a page with no unknown URNs (incremental sync); neither loop lives here,
    only the page this fetch returned.
    """

    connections: tuple[ConnectionSummary, ...]
    start: int
    count: int
    total: int


def parse_connections_page(body: str) -> ConnectionsPageResult:
    """Parse one page of ``relationships/dash/connections``.

    Expects the normalized collection envelope: ``data.elements``, an entry
    per connection carrying a ``*connectedMemberResolutionResult`` reference
    into the top-level ``included`` array, where the profile fields actually
    live. ``data.paging`` carries ``start``/``count``/``total``.
    """
    endpoint = CONNECTIONS_ENDPOINT
    payload = _load_json(endpoint, body)
    root = _expect(endpoint, "<root>", payload, dict)
    data = _field(endpoint, root, "data", dict)
    included_raw = _field(endpoint, root, "included", list)
    included = _index_included(endpoint, included_raw)

    elements_raw = _field(endpoint, data, "elements", list)
    paging = _field(endpoint, data, "paging", dict)
    start = _field(endpoint, paging, "start", int)
    count = _field(endpoint, paging, "count", int)
    total = _field(endpoint, paging, "total", int)

    connections: list[ConnectionSummary] = []
    for i, raw_element in enumerate(elements_raw):
        element = _expect(endpoint, f"data.elements[{i}]", raw_element, dict)
        ref = _field(endpoint, element, "*connectedMemberResolutionResult", str)
        profile = _resolve_included(
            endpoint, included, f"data.elements[{i}]['*connectedMemberResolutionResult']", ref
        )
        where = f"included entity '{ref}'"
        connected_at_ms = _optional_field(endpoint, element, "createdAt", int)
        connections.append(
            ConnectionSummary(
                urn=_field(endpoint, profile, "entityUrn", str),
                public_id=_field(endpoint, profile, "publicIdentifier", str),
                first_name=_field(endpoint, profile, "firstName", str),
                last_name=_field(endpoint, profile, "lastName", str),
                headline=_optional_field(endpoint, profile, "headline", str),
                connected_at=(
                    _epoch_millis(endpoint, where, connected_at_ms)
                    if connected_at_ms is not None
                    else None
                ),
            )
        )

    return ConnectionsPageResult(
        connections=tuple(connections), start=start, count=count, total=total
    )


# =============================================================================
# Profile visit results (spec 9.2 "Profile visit"; 9.4 "Enrichment"; 9.10)
#
# Since #190 enrichment reads the profile and its contact-info overlay from the
# answers the page loads (ADR 0006): ``netkeeper.linkedin.flagship_profile``
# parses them into these types. They stay here, beside ``ConnectionSummary``,
# because they are what the extractor hands the core (spec 9.10's "Out" column);
# the Voyager profile and contact-info requests they were first parsed from are
# retired.
# =============================================================================


@dataclass(frozen=True, slots=True)
class ContactInfo:
    """The "Contact info" overlay's contents for one profile.

    Every field is optional at the value level: a person may share none of them.
    ``emails`` lists the addresses in the order the overlay shows them. ``birthday``
    and ``address`` are the overlay's own text, unparsed, and have no column to land
    in (spec 8.1); ``connected_on`` is the overlay's "Connected since" day. The
    phone, address, birthday, and Twitter sections were not in the #149 capture, so
    their readers fail soft: a value they cannot read is left out, never guessed.
    """

    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    websites: tuple[str, ...] = ()
    twitter_handles: tuple[str, ...] = ()
    birthday: str | None = None
    address: str | None = None
    connected_on: date | None = None


@dataclass(frozen=True, slots=True)
class PositionEntry:
    """One entry from a profile's experience section.

    ``company`` and every date are optional: a person can list a role with no
    company name or no dates, and one such entry must not make a whole profile
    unreadable (#171 review).
    """

    title: str
    company: str | None
    start_year: int | None
    start_month: int | None
    end_year: int | None
    end_month: int | None


@dataclass(frozen=True, slots=True)
class EducationEntry:
    """One entry from a profile's education section."""

    school: str
    degree: str | None
    field_of_study: str | None
    start_year: int | None
    end_year: int | None


@dataclass(frozen=True, slots=True)
class ProfileDetails:
    """Everything a profile visit harvests, other than contact info (spec 9.4 step 3-4)."""

    urn: str
    public_id: str
    first_name: str
    last_name: str
    headline: str | None
    location: str | None
    positions: tuple[PositionEntry, ...]
    education: tuple[EducationEntry, ...]


# =============================================================================
# Messaging conversations (spec 9.2 "Messaging conversations"; 9.4 "Inbox poll")
# =============================================================================

CONVERSATIONS_ENDPOINT: Final = "messaging/conversations"
CONVERSATIONS_PATH: Final = "/voyager/api/messaging/conversations"  # captured 2026-09-22

#: captured 2026-09-22.
CONVERSATIONS_DECORATION_ID: Final = (
    "com.linkedin.voyager.dash.deco.messaging.web.WebConversationListWithProfile-15"
)

CONVERSATIONS_DEFAULT_COUNT: Final = 20


def conversations_query(*, count: int = CONVERSATIONS_DEFAULT_COUNT) -> dict[str, str]:
    """Query parameters for one page of the messaging inbox (spec 9.4 "Inbox poll").

    No ``start`` offset: the inbox poll always reads from the most recent
    conversation, newest-first, the way LinkedIn's own inbox does — a caller
    that needs a specific window filters by ``last_activity_at`` on the
    result rather than paging by offset, because unlike the connections list
    this collection's order changes as new messages arrive mid-poll.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    return {"decorationId": CONVERSATIONS_DECORATION_ID, "count": str(count)}


@dataclass(frozen=True, slots=True)
class ConversationParticipant:
    """One participant in a conversation, other than the account owner."""

    urn: str
    public_id: str | None
    first_name: str
    last_name: str


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """One row of the messaging inbox (spec 9.2's "Threads, participants, last message, direction").

    ``participants`` can hold more than one entry for a group thread; spec
    9.4's inbox poll and P4-01's reply detection decide what to do with that
    (spec 9.10's boundary: this module reports what LinkedIn sent, it does
    not decide whether a thread counts as "have we talked").
    """

    urn: str
    last_activity_at: datetime
    unread: bool
    participants: tuple[ConversationParticipant, ...]
    last_message_text: str | None
    last_message_sender_urn: str | None


@dataclass(frozen=True, slots=True)
class ConversationsPageResult:
    """One fetched page of the messaging inbox."""

    conversations: tuple[ConversationSummary, ...]
    start: int
    count: int
    total: int


def _participant_from_entity(
    endpoint: str, entity: Mapping[str, object]
) -> ConversationParticipant:
    return ConversationParticipant(
        urn=_field(endpoint, entity, "entityUrn", str),
        public_id=_optional_field(endpoint, entity, "publicIdentifier", str),
        first_name=_field(endpoint, entity, "firstName", str),
        last_name=_field(endpoint, entity, "lastName", str),
    )


def _conversation_from_element(
    endpoint: str, index: int, element: Mapping[str, object]
) -> ConversationSummary:
    where = f"data.elements[{index}]"
    urn = _field(endpoint, element, "entityUrn", str)
    last_activity_ms = _field(endpoint, element, "lastActivityAt", int)
    unread = _field(endpoint, element, "unread", bool)

    participants_raw = _field(endpoint, element, "participants", list)
    participants = tuple(
        _participant_from_entity(
            endpoint, _expect(endpoint, f"{where}.participants[{j}]", raw, dict)
        )
        for j, raw in enumerate(participants_raw)
    )

    last_message_text: str | None = None
    last_message_sender_urn: str | None = None
    last_message = _optional_field(endpoint, element, "lastMessage", dict)
    if last_message is not None:
        body = _optional_field(endpoint, last_message, "body", dict)
        if body is not None:
            last_message_text = _field(endpoint, body, "text", str)
        sender = _optional_field(endpoint, last_message, "sender", dict)
        if sender is not None:
            last_message_sender_urn = _field(endpoint, sender, "entityUrn", str)

    return ConversationSummary(
        urn=urn,
        last_activity_at=_epoch_millis(endpoint, f"{where}.lastActivityAt", last_activity_ms),
        unread=unread,
        participants=participants,
        last_message_text=last_message_text,
        last_message_sender_urn=last_message_sender_urn,
    )


def parse_conversations_page(body: str) -> ConversationsPageResult:
    """Parse one page of ``messaging/conversations``.

    Unlike the connections list, each element here embeds its participants
    and last message directly rather than referencing ``included`` — matching
    how the messaging API is documented to behave across the reverse-engineering
    write-ups this module's shapes are drawn from (see the module docstring's
    note on capture dates).
    """
    endpoint = CONVERSATIONS_ENDPOINT
    payload = _load_json(endpoint, body)
    root = _expect(endpoint, "<root>", payload, dict)
    data = _field(endpoint, root, "data", dict)

    elements_raw = _field(endpoint, data, "elements", list)
    paging = _field(endpoint, data, "paging", dict)
    start = _field(endpoint, paging, "start", int)
    count = _field(endpoint, paging, "count", int)
    total = _field(endpoint, paging, "total", int)

    conversations = tuple(
        _conversation_from_element(endpoint, i, _expect(endpoint, f"data.elements[{i}]", raw, dict))
        for i, raw in enumerate(elements_raw)
    )

    return ConversationsPageResult(
        conversations=conversations, start=start, count=count, total=total
    )
