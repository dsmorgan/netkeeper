"""The DOM fallback: connections-page scroll enumeration and the contact-info overlay (P2-08).

Spec 9.3: "In-page API first, DOM second." :mod:`netkeeper.linkedin.voyager`'s
endpoint constants and ``decorationId`` values are undocumented and change
without notice; when a parser there stops recognizing a response it raises
:class:`~netkeeper.linkedin.voyager.RouteChanged` rather than crash or lie
about what it read (spec 9.7's ``Outcome.ROUTE_CHANGED``). This module is
what a run falls back to when that happens, for "the two paths that matter
most, the connections page infinite scroll and the contact-info overlay"
(spec 9.3's own words): :class:`DomConnectionsSource` implements
:class:`~netkeeper.linkedin.connections.ConnectionsSource`, and
:class:`DomContactInfoSource` implements
:class:`~netkeeper.linkedin.contact_info.ContactInfoSource`. Neither is
wired to switch on its own -- :class:`~netkeeper.linkedin.connections.FallbackConnectionsSource`
and :class:`~netkeeper.linkedin.contact_info.FallbackContactInfoSource` are
the callers' decision about when to use one, made once, in the pure layer
those two live in.

**Read-only, and scroll is the only automation.** Both sources here read a
page that a :class:`~netkeeper.linkedin.browser.BrowserRun` already holds
(or navigate it, with an ordinary ``goto``, to a url the account owner could
type in themselves) and inspect what actually rendered, through
``page.evaluate`` scripts that query the DOM and return plain data -- they
never write to the page, never simulate a click, and never do anything to
open the contact-info overlay beyond navigating directly to LinkedIn's own
overlay url for it (``/in/<public-id>/overlay/contact-info/``), the same url
a real "Contact info" link on the profile page points at. The only actual
*input* either source ever sends the page is
:meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`'s mouse-wheel replay,
already built and rate-limited by :mod:`netkeeper.linkedin.pacing`. Neither
class calls ``add_init_script``, ``route``, or anything else
``tests/test_browser_safety.py`` forbids (ADR 0002, spec 9.1); this module is
registered in that test's ``BROWSER_MODULES`` and ``BROWSER_CALLERS`` because
it is, like :mod:`netkeeper.linkedin.fetch`, a module that holds a real tab
open across real wall-clock time (a scroll's dwell, an ``evaluate`` call) and
so must never be imported from a request handler (spec 9.9).

**Selectors are authored, not captured -- same honesty as voyager.py's
constants** (see that module's docstring for the fuller explanation and the
capture procedure). This task has no logged-in LinkedIn session and must not
fetch anything from linkedin.com, so the selectors below are written from two
things that are genuinely stable and publicly documented rather than read off
a live DevTools capture: LinkedIn's own URL routing (every profile lives at
``/in/<public-id>/``, which the site's URL structure alone establishes) and
ordinary, standards-based HTML (an email link is ``mailto:``, a phone link is
``tel:`` -- conventions LinkedIn did not invent and has no reason to break).
Where neither of those reaches, a best-guess selector is marked with the date
it was authored, exactly like a ``decorationId`` in ``voyager.py``. **#149
tracks verifying voyager.py's constants against a real session; the same
verification is owed to every selector below before a real run relies on
it.** If a selector is wrong, the defensive parsing in this module degrades
to ``Outcome.ROUTE_CHANGED`` (connections list) or a missing
:class:`~netkeeper.linkedin.voyager.ContactInfo` field (contact info) rather
than crashing or, worse, silently returning wrong data -- see
:func:`_parse_cards` and :func:`_parse_contact_info_dom`.

**Why every DOM connections page reports ``total=0``.** LinkedIn's rendered
connections page shows a connection count, but for a large network that
count is capped and shown as e.g. "500+ connections" -- not the real total,
an *undercount* with a plus sign attached. Parsing that as a literal integer
would let a run believe it had seen every connection the moment it passed
500, `aging` everyone past that point as unseen. So this module never reports
a total at all: every :class:`~netkeeper.linkedin.voyager.ConnectionsPageResult`
from :class:`DomConnectionsSource` carries ``total=0``, which
:class:`~netkeeper.linkedin.connections.SyncResult.complete` reads as "unproven".
That property also checks ``len(seen_urns) >= max_total``, and every
connection this module reports has ``urn=None`` (see the next section), so a
DOM page can never grow ``seen_urns`` either -- only a Voyager-served page
can. In practice this means a run that falls back to DOM does not merely
need an *earlier* page to have established a real, positive ``max_total``
(see that property's own docstring); it needs Voyager to have already seen,
by URN, at least as many distinct people as that total claims, before DOM
ever contributes a page that was not already fully redundant with what
Voyager confirmed. A full sync that falls back to DOM from its very first
page, or partway through a network DOM goes on to cover the rest of, can
never be complete, however faithfully it reaches the visible end of the
list: it has no way to prove there was nothing past 500 it was never shown,
or that a person it read from the DOM was not, in fact, new. This is a
deliberate choice to trade completeness for aging safety (the item's own
instruction), not an oversight.

**Why every connection this module reports has ``urn=None``.** See
:class:`~netkeeper.linkedin.voyager.ConnectionSummary`'s docstring. LinkedIn's
internal URN is API data, not anything the rendered connections list prints;
inventing one to fill the required-looking field would be worse than leaving
it empty, so identity resolves these rows by public id alone (spec 8.2), and
:func:`netkeeper.crm.apply._mark_seen` matches by either now, for exactly this
reason.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Final
from urllib.parse import urlsplit

from netkeeper.linkedin.browser import BrowserRun, PageLike, ScrollOutcome
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.connections import SourcePage
from netkeeper.linkedin.contact_info import ContactInfoResult
from netkeeper.linkedin.pacing import (
    DEFAULT_SCROLL_PROFILE,
    ScrollPlan,
    ScrollProfile,
    scroll_like_a_person,
)
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import (
    ConnectionsPageResult,
    ConnectionSummary,
    ContactInfo,
    RouteChanged,
)

log = logging.getLogger(__name__)

#: The only host a production DOM read may run against -- same value and same
#: reasoning as netkeeper.linkedin.fetch.LINKEDIN_ORIGIN: fixed, never
#: configurable, with a loopback exception that exists only for the smoke suite.
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# --- selectors: authored, not captured (see the module docstring) -----------

#: Where the connections list lives. Authored 2026-09-23.
CONNECTIONS_LIST_PATH: Final = "/mynetwork/invite-connect/connections/"

#: One list item per connection card. ``data-view-name`` is the kind of
#: tracking/testability attribute LinkedIn's own React front end uses
#: elsewhere in the product; an ``li`` is the expected wrapper for an
#: infinite-scroll list. Authored 2026-09-23.
CARD_SELECTOR: Final = 'li[data-view-name="connections-list-item"]'

#: Within a card, the profile link -- the one selector here built on
#: something LinkedIn's URL routing itself guarantees rather than a guessed
#: attribute. Authored 2026-09-23.
PROFILE_LINK_SELECTOR: Final = 'a[href*="/in/"]'

#: Within a card, the name and headline, when the card marks them with a name
#: of their own rather than leaving the name as the link's bare text.
#: Authored 2026-09-23.
NAME_SELECTOR: Final = '[data-view-name="connections-list-item-name"]'
HEADLINE_SELECTOR: Final = '[data-view-name="connections-list-item-headline"]'

#: The contact-info overlay's own url -- the same url a real "Contact info"
#: link on the profile page points at, so navigating here directly is not a
#: click simulated, it is the destination a click would have reached.
#: Authored 2026-09-23.
CONTACT_INFO_OVERLAY_PATH_TEMPLATE: Final = "/in/{public_id}/overlay/contact-info/"

DOM_CONNECTIONS_ENDPOINT: Final = "dom/connections-list"
DOM_CONTACT_INFO_ENDPOINT: Final = "dom/contact-info-overlay"

#: How many extra scroll-and-read cycles one fetch_page (or one contact-info
#: read) may spend settling a lazy-loaded page before treating "no growth" as
#: the true end, rather than a render that has not caught up yet. Bounded so
#: a stalled page cannot turn one budgeted unit of work into an unbounded
#: loop (spec 9.9: budgets are enforced between units of work, not spent
#: without limit inside one).
MAX_SETTLE_ATTEMPTS: Final = 3


class DomFetchError(RuntimeError):
    """The DOM read mechanism itself failed -- not a LinkedIn response to classify.

    Mirrors :class:`netkeeper.linkedin.fetch.VoyagerFetchError`'s role for the
    API path: the tab not being on the origin this instance is bound to,
    ``page.evaluate`` itself throwing, or a result shaped unlike anything this
    module's own script produces. Never raised for a page spec 9.7 has a row
    for -- a checkpoint or a login wall is an :class:`Outcome`, carried on
    :class:`~netkeeper.linkedin.connections.SourcePage` or
    :class:`~netkeeper.linkedin.contact_info.ContactInfoResult`, not this.
    """


def _require_dom_origin(origin: str) -> str:
    """``origin``, rebuilt and canonical, if a DOM read may run against it.

    Identical reasoning to ``netkeeper.linkedin.fetch._require_fetchable_origin``:
    parsed through :func:`~netkeeper.linkedin.strict_origin.parse_strict_origin`
    first, so a backslash or userinfo trick that fools ``urlsplit`` alone is
    refused before it matters, and compared against the *parsed and rebuilt*
    origin, never the original string. No caller outside a test ever passes
    anything but the default.
    """
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise ValueError(f"{origin!r} is not a usable DOM-read origin: {exc}") from exc
    if str(parsed) == LINKEDIN_ORIGIN:
        return str(parsed)
    if parsed.scheme in ("http", "https") and parsed.host in _LOOPBACK_HOSTS:
        return str(parsed)
    raise ValueError(
        f"a DOM read may only run against {LINKEDIN_ORIGIN!r}, or this machine's own"
        f" loopback for tests, got {origin!r}"
    )


def _require_page_on_origin(page_url: str, origin: str) -> None:
    """Refuse to evaluate anything unless the tab is actually on ``origin``.

    The DOM-read counterpart of ``fetch.py``'s F2 fix, for the same reason: a
    tab left over from a previous navigation, or one that moved on after this
    source was constructed, could otherwise have its content read as if it
    were LinkedIn's own. ``page_url`` is Chrome's own, already-resolved url,
    so a plain ``urlsplit`` compare of scheme, host, and port is enough here,
    exactly as it is in ``fetch.py``.
    """
    page = urlsplit(page_url)
    want = urlsplit(origin)
    if (page.scheme, page.hostname, page.port) != (want.scheme, want.hostname, want.port):
        raise DomFetchError(
            f"the tab is not on {origin!r} (it is on"
            f" {page.scheme or '?'}://{page.hostname or '?'}"
            f"{f':{page.port}' if page.port else ''}); refusing to read the DOM from a"
            " page that might not be able to be trusted"
        )


def _classify_page_url(url: str) -> Outcome | None:
    """Whether ``url`` alone shows a checkpoint or a login wall; ``None`` otherwise.

    DOM enumeration has no HTTP status of its own -- it inspects a page that
    is already rendered, rather than receiving a response -- so this reuses
    :func:`~netkeeper.linkedin.classify.classify` with a fixed ``200`` and an
    empty body. That is deliberate, not a shortcut: passing ``200`` means
    only the status-derived branches (throttled, not-found, the catch-all
    route-changed) never fire here, which is correct -- none of those
    describe a page a browser is currently sitting on -- while the two
    branches that matter, a checkpoint or a login-wall *url*, are checked
    exactly the way spec 9.7's table already checks a fetched response's
    final url. Unlike the in-page Voyager fetch (spec 9.7's "the API is
    fetched in-page rather than navigated to"), every url this function is
    given came from a real ``page.goto`` navigation, so a redirect to a login
    wall genuinely does change ``page.url`` -- there is no body-scanning
    fallback here because none is needed.

    This is spec 9.3's "attack your own fallback" case: a login wall's
    connections-shaped url renders no cards at all, and without this check
    that would parse as an honest, empty page (``Outcome.OK`` with no
    connections) rather than the ``LoggedOut`` it actually is.
    """
    outcome = classify(200, url, "")
    return outcome if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT) else None


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


# --- connections list ---------------------------------------------------------


def _cards_expression() -> str:
    """The script :class:`DomConnectionsSource` hands to ``page.evaluate``.

    Every currently-rendered card, read fresh -- LinkedIn's infinite scroll
    keeps every card already loaded in the DOM as more are appended below it
    (never virtualized away), so this always returns the *whole* list seen so
    far, not only what a scroll just added; :meth:`DomConnectionsSource.fetch_page`
    is what turns that into pages. Constants are embedded as JSON literals,
    the same technique ``fetch.py``'s ``_fetch_expression`` uses and for the
    same reason: JSON string syntax is a strict subset of a JavaScript string
    literal, so ``json.dumps``'s escaping is already enough.
    """
    return (
        "() => {"
        f"const cards = document.querySelectorAll({json.dumps(CARD_SELECTOR)});"
        "const out = [];"
        "for (const card of cards) {"
        f"  const link = card.querySelector({json.dumps(PROFILE_LINK_SELECTOR)});"
        "  if (!link) { continue; }"
        "  const href = link.getAttribute('href') || '';"
        "  const m = href.match(/\\/in\\/([^/?#]+)/);"
        "  if (!m) { continue; }"
        f"  const nameEl = card.querySelector({json.dumps(NAME_SELECTOR)});"
        f"  const headlineEl = card.querySelector({json.dumps(HEADLINE_SELECTOR)});"
        "  const name = (nameEl ? nameEl.textContent"
        "    : (link.getAttribute('aria-label') || link.textContent || '')).trim();"
        "  const headline = headlineEl ? headlineEl.textContent.trim() : null;"
        "  out.push({publicId: decodeURIComponent(m[1]), name: name, headline: headline});"
        "}"
        "return out;"
        "}"
    )


_CARDS_EXPRESSION: Final = _cards_expression()


def _split_name(name: str) -> tuple[str, str]:
    """``name`` as ``(first, last)``. The DOM has no separate first/last fields
    the way Voyager's JSON does (spec 9.2), only one rendered display name, so
    this splits on the first run of whitespace -- the same heuristic a person
    reading the card would use. A one-word name (rare; more often a data
    oddity than a real card) keeps the whole thing as ``first`` and reports
    an empty ``last``, rather than raising: ``ConnectionSummary.last_name`` is
    a required ``str``, and an empty one is the honest "unknown" here, the
    same way voyager.py's own optional fields read absence.
    """
    parts = name.split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _parse_one_card(item: object) -> ConnectionSummary | None:
    """One raw card object as a :class:`ConnectionSummary`, or ``None`` if unreadable.

    ``None`` rather than raising: a single odd card (a sponsored suggestion
    or a skeleton placeholder the selectors above did not mean to catch) is
    common in a scraped list and is not, on its own, evidence the page's
    shape changed. :func:`_parse_cards` is what escalates "every card in a
    nonempty batch was unreadable" to :class:`RouteChanged`.
    """
    if not isinstance(item, Mapping):
        return None
    public_id = item.get("publicId")
    name = item.get("name")
    if not isinstance(public_id, str) or not public_id.strip():
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    headline_raw = item.get("headline")
    headline = headline_raw.strip() if isinstance(headline_raw, str) else None
    headline = headline if headline else None
    first, last = _split_name(name.strip())
    return ConnectionSummary(
        urn=None,
        public_id=public_id.strip(),
        first_name=first,
        last_name=last,
        headline=headline,
        connected_at=None,
    )


def _parse_cards(endpoint: str, raw: object) -> tuple[ConnectionSummary, ...]:
    """Every readable card in ``raw``, LinkedIn's own script result.

    ``raw`` not being a list at all is a structural break (the script did not
    return what :func:`_cards_expression` promises) and is
    :class:`RouteChanged` immediately. A nonempty list where *every* card
    individually failed to parse (:func:`_parse_one_card` returning ``None``
    for all of them) is escalated the same way -- see that function's
    docstring for why one bad card alone is not. An empty ``raw`` list is
    simply "nothing rendered (yet)" and returns ``()`` -- the caller decides
    what an empty batch means, the way an empty Voyager page does.
    """
    if not isinstance(raw, list):
        raise RouteChanged(endpoint, f"expected a list of cards, got {_type_name(raw)}")
    if not raw:
        return ()
    cards: list[ConnectionSummary] = []
    skipped = 0
    for item in raw:
        parsed = _parse_one_card(item)
        if parsed is None:
            skipped += 1
            continue
        cards.append(parsed)
    if not cards:
        raise RouteChanged(
            endpoint, f"{len(raw)} card(s) returned by the DOM read, none had a readable shape"
        )
    if skipped:
        log.warning(
            "connections dom: skipped %d of %d card(s) with an unreadable shape",
            skipped,
            len(raw),
        )
    return tuple(cards)


class DomConnectionsSource:
    """The connections list's infinite scroll as a
    :class:`~netkeeper.linkedin.connections.ConnectionsSource` (spec 9.3).

    One instance enumerates one run's worth of the connections list: the
    first call navigates the run's tab to the list (an ordinary ``goto``, the
    same as visiting the page by hand); every call after that reuses whatever
    is already rendered and scrolls for more only when a page needs cards
    this instance has not accumulated yet. Cards already read are kept
    (:attr:`_cards`), so asking for page 3 after page 2 never re-scrolls from
    the top.

    ``fetch_page(start, count)`` treats the whole scrolled-so-far list as one
    virtual sequence, indexed exactly like Voyager's ``start``/``count``, and
    slices it -- so from :func:`~netkeeper.linkedin.connections.run_connections_sync`'s
    point of view this reads exactly like paging an API, one gated,
    human-paced unit of work at a time (spec 9.5, 9.6), even though the
    underlying mechanism is scrolling rather than a sequence of independent
    requests.

    ``rng`` feeds :func:`~netkeeper.linkedin.pacing.scroll_like_a_person`,
    which builds the plan :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll`
    replays -- the same human-like wheel-event pacing spec 9.5 describes for a
    profile visit, reused here because scrolling a list is exactly the motion
    it already models. ``scroll_profile`` is that function's own
    :class:`~netkeeper.linkedin.pacing.ScrollProfile`, defaulting to the module's
    defaults the same way :func:`~netkeeper.linkedin.pacing.scroll_like_a_person`
    itself does; a caller with its own pacing config (``netkeeper.services.pacing``,
    once ``[linkedin.pacing]`` grows a scroll key -- see that module's docstring)
    passes one through here, and the opt-in smoke suite passes a fast one so a
    handful of invented cards do not cost a real dwell per settle attempt.
    ``sleep`` stands in for the wait between wheel events in an offline test, the
    same parameter :meth:`BrowserRun.scroll` itself takes; the default is real time.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        rng: random.Random | None = None,
        scroll_profile: ScrollProfile = DEFAULT_SCROLL_PROFILE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._run = run
        self._origin = _require_dom_origin(origin)
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        self._scroll_profile = scroll_profile
        self._sleep = sleep
        self._cards: list[ConnectionSummary] = []
        self._navigated = False

    @property
    def endpoint(self) -> str:
        return DOM_CONNECTIONS_ENDPOINT

    async def fetch_page(self, *, start: int, count: int) -> SourcePage:
        page = await self._goto_or_ensure()
        _require_page_on_origin(page.url, self._origin)
        blocked = _classify_page_url(page.url)
        if blocked is not None:
            return SourcePage(outcome=blocked, final_url=page.url)

        for _attempt in range(MAX_SETTLE_ATTEMPTS):
            if len(self._cards) >= start + count:
                break
            plan = scroll_like_a_person(
                self._rng,
                steps_range=self._scroll_profile.steps_range,
                delta_range_px=self._scroll_profile.delta_range_px,
                pause_range_s=self._scroll_profile.pause_range_s,
                back_up_p=self._scroll_profile.back_up_p,
                back_up_delta_range_px=self._scroll_profile.back_up_delta_range_px,
                dwell_median_s=self._scroll_profile.dwell_median_s,
                dwell_sigma=self._scroll_profile.dwell_sigma,
            )
            outcome = await self._scroll(plan)
            page = outcome.page
            _require_page_on_origin(page.url, self._origin)
            blocked = _classify_page_url(page.url)
            if blocked is not None:
                return SourcePage(outcome=blocked, final_url=page.url)
            try:
                raw = await page.evaluate(_CARDS_EXPRESSION)
            except Exception as exc:
                raise DomFetchError(
                    f"could not read the connections list DOM: {_reason(exc)}"
                ) from exc
            try:
                cards = _parse_cards(self.endpoint, raw)
            except RouteChanged:
                return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
            if len(cards) > len(self._cards):
                self._cards = list(cards)
            # Otherwise: no growth this attempt. Keep the loop's attempt
            # counter moving rather than retrying forever -- MAX_SETTLE_ATTEMPTS
            # bounds how many extra scrolls one page of work may spend
            # waiting for a slow render before this call gives up and reports
            # whatever it has, which the caller reads as a short or empty
            # page (see the module docstring on why that is still safe).

        selected = tuple(self._cards[start : start + count])
        return SourcePage(
            outcome=Outcome.OK,
            final_url=page.url,
            page=ConnectionsPageResult(connections=selected, start=start, count=count, total=0),
        )

    async def _goto_or_ensure(self) -> PageLike:
        if not self._navigated:
            self._navigated = True
            return await self._run.goto(f"{self._origin}{CONNECTIONS_LIST_PATH}")
        return await self._run.ensure_page()

    async def _scroll(self, plan: ScrollPlan) -> ScrollOutcome:
        if self._sleep is None:
            return await self._run.scroll(plan)
        return await self._run.scroll(plan, sleep=self._sleep)


# --- contact-info overlay -----------------------------------------------------


def _contact_info_expression() -> str:
    """The script :class:`DomContactInfoSource` hands to ``page.evaluate``.

    Built on ``mailto:`` and ``tel:`` links (a standards-based convention,
    not a LinkedIn-specific one -- see the module docstring) rather than a
    guessed class name: an email is read from the first ``mailto:`` link on
    the page, phone numbers from every ``tel:`` link, and every other
    ``http(s)`` link is sorted into a Twitter/X handle (``twitter.com`` or
    ``x.com``) or an ordinary website, excluding LinkedIn's own domain (a
    "view profile" or navigation link back to linkedin.com is not a website
    the person shared).
    """
    return (
        "() => {"
        "const email = document.querySelector('a[href^=\"mailto:\"]');"
        "const phones = Array.from(document.querySelectorAll('a[href^=\"tel:\"]'));"
        "const links = Array.from(document.querySelectorAll('a[href^=\"http\"]'));"
        "const websites = [];"
        "const twitterHandles = [];"
        "for (const a of links) {"
        "  let host;"
        "  try { host = new URL(a.href).hostname.replace(/^www\\./, ''); }"
        "  catch (e) { continue; }"
        "  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) { continue; }"
        "  if (host === 'twitter.com' || host === 'x.com') {"
        "    const parts = a.href.split('/').filter(Boolean);"
        "    const handle = parts[parts.length - 1];"
        "    if (handle) { twitterHandles.push(handle); }"
        "    continue;"
        "  }"
        "  websites.push(a.href);"
        "}"
        "return {"
        "  email: email ? email.getAttribute('href').replace(/^mailto:/, '') : null,"
        "  phones: phones.map(a => a.getAttribute('href').replace(/^tel:/, '')),"
        "  websites: websites,"
        "  twitterHandles: twitterHandles,"
        "};"
        "}"
    )


_CONTACT_INFO_EXPRESSION: Final = _contact_info_expression()


def _string_tuple(value: object) -> tuple[str, ...]:
    """A list's string entries, trimmed, empty ones dropped. Never raises: this
    module's own script produced ``value``, so a malformed entry here means
    a rendering quirk (an empty ``tel:`` link), not a shape LinkedIn changed
    out from under a parser (unlike voyager.py's identically-named intent for
    real API data, where the stricter :class:`RouteChanged` is the right
    reaction; see :func:`_parse_contact_info_dom` for the one check in this
    function's caller that does still raise).
    """
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _parse_contact_info_dom(endpoint: str, raw: object) -> ContactInfo:
    """``raw``, the contact-info script's result, as a :class:`ContactInfo`.

    Only the top-level shape is required to be an object -- if the script
    returned anything else, this module's own contract with its own script is
    broken and that is :class:`RouteChanged`, the same standard
    ``voyager.py``'s parsers hold every field to. Below that, malformed
    individual entries are dropped rather than fatal, via :func:`_string_tuple`.
    """
    if not isinstance(raw, Mapping):
        raise RouteChanged(endpoint, f"expected an object, got {_type_name(raw)}")
    email_raw = raw.get("email")
    email = email_raw.strip() if isinstance(email_raw, str) else None
    email = email if email else None
    return ContactInfo(
        email=email,
        phones=_string_tuple(raw.get("phones")),
        websites=_string_tuple(raw.get("websites")),
        twitter_handles=_string_tuple(raw.get("twitterHandles")),
    )


class DomContactInfoSource:
    """The profile contact-info overlay as a
    :class:`~netkeeper.linkedin.contact_info.ContactInfoSource` (spec 9.3, 9.4 step 3).

    Each call navigates the run's tab straight to LinkedIn's own contact-info
    overlay url for ``public_id`` -- the same url a real "Contact info" link
    on the profile page points at (see the module docstring) -- and reads the
    rendered overlay. This never clicks anything: the overlay renders as the
    page's own content at that url, so navigating there *is* the "open the
    overlay" step, not a way around it.
    """

    def __init__(self, run: BrowserRun, *, origin: str = LINKEDIN_ORIGIN) -> None:
        self._run = run
        self._origin = _require_dom_origin(origin)

    @property
    def endpoint(self) -> str:
        return DOM_CONTACT_INFO_ENDPOINT

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        if not public_id.strip():
            raise ValueError("public_id is empty")
        path = CONTACT_INFO_OVERLAY_PATH_TEMPLATE.format(public_id=public_id)
        page = await self._run.goto(f"{self._origin}{path}")
        _require_page_on_origin(page.url, self._origin)
        blocked = _classify_page_url(page.url)
        if blocked is not None:
            return ContactInfoResult(outcome=blocked, final_url=page.url)
        try:
            raw = await page.evaluate(_CONTACT_INFO_EXPRESSION)
        except Exception as exc:
            raise DomFetchError(
                f"could not read the contact-info overlay DOM: {_reason(exc)}"
            ) from exc
        try:
            info = _parse_contact_info_dom(self.endpoint, raw)
        except RouteChanged:
            return ContactInfoResult(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
        return ContactInfoResult(outcome=Outcome.OK, final_url=page.url, info=info)
