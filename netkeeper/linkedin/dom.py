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
``tel:``, and a modal overlay carries ``role="dialog"`` -- conventions
LinkedIn did not invent and has no reason to break). Where neither of those
reaches, a best-guess selector is marked with the date it was authored,
exactly like a ``decorationId`` in ``voyager.py``. **#149 tracks verifying
voyager.py's constants against a real session; the same verification is owed
to every selector below before a real run relies on it.** If a selector is
wrong, the defensive parsing in this module degrades to
``Outcome.ROUTE_CHANGED`` rather than crashing or, worse, silently returning
wrong data -- see :func:`_parse_cards` and :func:`_parse_contact_info_dom`,
and the container/dialog presence checks in :class:`DomConnectionsSource` and
:class:`DomContactInfoSource` (#173 review, F5).

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
can. On top of that, ``SyncResult.complete`` now also refuses outright once
:class:`~netkeeper.linkedin.connections.FallbackConnectionsSource` has ever
switched to this module for a run (its ``switched`` property; #173 review,
F5(a)) -- a second, simpler invariant that does not depend on the totals
math above at all. A full sync that falls back to DOM from its very first
page, or partway through a network DOM goes on to cover the rest of, can
never be complete, however faithfully it reaches the visible end of the
list: it has no way to prove there was nothing past 500 it was never shown,
or that a person it read from the DOM was not, in fact, new. This is a
deliberate choice to trade completeness for aging safety (the item's own
instruction), not an oversight.

**Why every connection this module reports has ``urn=None``, and why a DOM
row never creates a contact or writes a field (#173 review's design
decision).** See :class:`~netkeeper.linkedin.voyager.ConnectionSummary`'s
docstring for the URN half. The field-writing half goes further: a DOM row's
*only* identity signal is a public-id slug, and a slug is not owned by one
person forever -- LinkedIn lets an account release a vanity url and another
claim it (spec 9.6), so a slug this module reads today could belong to
someone else than the last time an authoritative source (Voyager, the
archive) saw it. Writing a name, a headline, or any other field on the
strength of a slug alone risks attributing a stranger's data to the wrong
contact, or creating a duplicate under a renamed slug an authoritative
source has not caught up to yet. So a DOM row is **sighting-only**:
:func:`netkeeper.crm.apply.apply_page` never resolves or applies it as an
:class:`~netkeeper.crm.identity.IncomingContact` -- it only uses the slug to
mark an *already-known* contact as seen (:func:`netkeeper.crm.apply._mark_seen`),
which is safe in one direction only (it can prevent an aging that should not
happen; it can never itself age anyone) and creates nothing. A DOM sighting
of a slug nobody holds is silently a no-op: the new or renamed connection it
would have described is picked up honestly by the next Voyager sync or
archive import, which carry (or already hold) a URN. ``first_name``,
``last_name``, and ``headline`` on a DOM-sourced :class:`ConnectionSummary`
are therefore read but never written to a contact by the mapping layer; they
exist mainly so a caller inspecting a page can log or display what was seen.
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
#: Pinned literally in tests/test_linkedin_dom.py, per CLAUDE.md's rule for a
#: safety-relevant constant (#173 review, R15).
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# --- selectors: authored, not captured (see the module docstring) -----------

#: Where the connections list lives. Authored 2026-09-23.
CONNECTIONS_LIST_PATH: Final = "/mynetwork/invite-connect/connections/"

#: The element the whole card list renders inside. Read as a structural
#: signal separate from card count (#173 review, F5(b)): its *absence* --
#: unlike zero cards while it is present, which can just be a page still
#: loading -- is a strong sign this is not the connections list at all: an
#: error page, a wall rendered in its place, or a shape LinkedIn changed.
#: Authored 2026-09-23.
LIST_CONTAINER_SELECTOR: Final = '[data-view-name="connections-list"]'

#: One list item per connection card, read from inside the container above.
#: ``data-view-name`` is the kind of tracking/testability attribute
#: LinkedIn's own React front end uses elsewhere in the product; an ``li`` is
#: the expected wrapper for an infinite-scroll list. Authored 2026-09-23.
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

#: The overlay's own modal element, read the same structural way as the
#: connections list's container (#173 review, F5(d), F7): a standards-based
#: ARIA role, not a guessed class name, and every contact-info query below is
#: scoped inside it -- the surrounding profile page carries its own links
#: (a bio's ``mailto:``, an unrelated website) that are not contact info the
#: person shared through this overlay. Authored 2026-09-23.
CONTACT_INFO_DIALOG_SELECTOR: Final = '[role="dialog"]'

#: Url shorteners excluded from "websites" (#173 review, F7): LinkedIn's own
#: (``lnkd.in``) and the handful of others common enough that a link through
#: one is more likely tracking noise than a website the person meant to
#: share. Not exhaustive -- a new one showing up is a missed website, not a
#: wrong one, so the list stays short and named rather than guessed at.
#: Authored 2026-09-23.
LINK_SHORTENER_HOSTS: Final[frozenset[str]] = frozenset(
    {"lnkd.in", "bit.ly", "t.co", "tinyurl.com"}
)

DOM_CONNECTIONS_ENDPOINT: Final = "dom/connections-list"
DOM_CONTACT_INFO_ENDPOINT: Final = "dom/contact-info-overlay"

#: How many extra scroll-and-read cycles one fetch_page (or one contact-info
#: read) may spend settling a lazy-loaded page before giving up. Bounded so
#: a stalled page cannot turn one budgeted unit of work into an unbounded
#: loop (spec 9.9: budgets are enforced between units of work, not spent
#: without limit inside one). Exhausting every attempt without reaching what
#: was asked for is never treated as a confirmed answer -- see
#: :meth:`DomConnectionsSource.fetch_page`'s docstring (#173 review, F5(c)).
MAX_SETTLE_ATTEMPTS: Final = 3

#: What the in-page scripts throw when ``location.origin`` does not match the
#: origin this instance is bound to, checked a second time *inside* the page
#: itself -- the DOM-read counterpart of fetch.py's #170 item 3 (#173 review,
#: L3). A fixed string netkeeper chose, never anything a page could echo back.
_ORIGIN_MISMATCH_MARKER: Final = "netkeeper-dom: page origin does not match the expected origin"


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
    exactly as it is in ``fetch.py``. This is the Python-side check, run
    before every ``evaluate`` call including after a scroll (#173 review,
    R9); :func:`_cards_expression`/:func:`_contact_info_expression`'s own
    ``location.origin`` check (L3) is the atomic, in-page second check for
    the gap between this read and the script actually starting to run.
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
    connections) rather than the ``LoggedOut`` it actually is. Called again
    after every scroll, not only before the first one (#173 review, R9/R10):
    a session can be kicked to a login wall mid-run just as easily as before
    it, and the url a tab is on can change without a fresh ``goto`` at all.
    """
    outcome = classify(200, url, "")
    return outcome if outcome in (Outcome.CHECKPOINT, Outcome.LOGGED_OUT) else None


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


async def _evaluate(page: PageLike, expression: str, *, what: str) -> object:
    """Run ``expression`` and translate a raised origin mismatch or any other
    failure into a :class:`DomFetchError` -- shared by both sources below."""
    try:
        return await page.evaluate(expression)
    except Exception as exc:
        if _ORIGIN_MISMATCH_MARKER in str(exc):
            raise DomFetchError(
                f"the tab navigated away between the origin check and reading {what}"
            ) from exc
        raise DomFetchError(f"could not read {what}: {_reason(exc)}") from exc


# --- connections list ---------------------------------------------------------


def _cards_expression(origin: str) -> str:
    """The script :class:`DomConnectionsSource` hands to ``page.evaluate``.

    Checks its own ``location.origin`` first, atomically with everything else
    it does (#173 review, L3, mirroring fetch.py's #170 item 3) -- the
    Python-side origin check runs before this is even called, but the tab is
    free to navigate in the gap between that read and this script starting to
    run. Then looks for :data:`LIST_CONTAINER_SELECTOR`; its absence answers
    ``{containerPresent: false}`` immediately, without reading cards at all
    (#173 review, F5(b)) -- an error page or a wall rendered in its place is
    structurally different from the connections list, and that is a stronger,
    cheaper signal than counting cards. Every currently-rendered card inside
    the container is read fresh -- LinkedIn's infinite scroll keeps every card
    already loaded in the DOM as more are appended below it (never
    virtualized away), so this always returns the *whole* list seen so far,
    not only what a scroll just added; :meth:`DomConnectionsSource.fetch_page`
    is what turns that into pages. A card whose ``publicId`` fails to decode
    (a malformed percent-encoding) is skipped rather than failing the whole
    read (#173 review, L2). Constants are embedded as JSON literals, the same
    technique ``fetch.py``'s ``_fetch_expression`` uses and for the same
    reason: JSON string syntax is a strict subset of a JavaScript string
    literal, so ``json.dumps``'s escaping is already enough.
    """
    return (
        "() => {"
        f"if (location.origin !== {json.dumps(origin)}) "
        f"{{ throw new Error({json.dumps(_ORIGIN_MISMATCH_MARKER)}); }}"
        f"const container = document.querySelector({json.dumps(LIST_CONTAINER_SELECTOR)});"
        "if (!container) { return {containerPresent: false, cards: []}; }"
        f"const cards = container.querySelectorAll({json.dumps(CARD_SELECTOR)});"
        "const out = [];"
        "for (const card of cards) {"
        f"  const link = card.querySelector({json.dumps(PROFILE_LINK_SELECTOR)});"
        "  if (!link) { continue; }"
        "  const href = link.getAttribute('href') || '';"
        "  const m = href.match(/\\/in\\/([^/?#]+)/);"
        "  if (!m) { continue; }"
        "  let publicId;"
        "  try { publicId = decodeURIComponent(m[1]); } catch (e) { continue; }"
        f"  const nameEl = card.querySelector({json.dumps(NAME_SELECTOR)});"
        f"  const headlineEl = card.querySelector({json.dumps(HEADLINE_SELECTOR)});"
        "  const name = (nameEl ? nameEl.textContent"
        "    : (link.getAttribute('aria-label') || link.textContent || '')).trim();"
        "  const headline = headlineEl ? headlineEl.textContent.trim() : null;"
        "  out.push({publicId: publicId, name: name, headline: headline});"
        "}"
        "return {containerPresent: true, cards: out};"
        "}"
    )


def _split_name(name: str) -> tuple[str, str]:
    """``name`` as ``(first, last)``. The DOM has no separate first/last fields
    the way Voyager's JSON does (spec 9.2), only one rendered display name, so
    this splits on the first run of whitespace -- the same heuristic a person
    reading the card would use. Whitespace (including a newline a card's own
    markup embedded, when the name and a trailing bit of headline share one
    text node) is collapsed to single spaces first, so a split never returns
    a "last name" carrying an embedded line break (#173 review, S6). A
    one-word name (rare; more often a data oddity than a real card) keeps the
    whole thing as ``first`` and reports an empty ``last``, rather than
    raising: ``ConnectionSummary.last_name`` is a required ``str``, and an
    empty one is the honest "unknown" here, the same way voyager.py's own
    optional fields read absence.
    """
    normalized = " ".join(name.split())
    parts = normalized.split(" ", 1)
    if not parts or not parts[0]:
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

    Reminder for the mapping layer (spec 9.8's as-built note, and this
    module's own docstring): ``first_name``/``last_name``/``headline`` are
    read here for completeness and for a caller that wants to log or display
    what was seen, but ``crm.apply.apply_page`` never writes them to a
    contact -- a DOM row is sighting-only.
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
    """Every readable card in ``raw`` (the script's ``cards`` array, container
    presence already confirmed by the caller).

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
    the top, and a read that comes back *shorter* than what is already known
    never overwrites it (a transient render glitch loses nothing already
    confirmed -- #173 review, R14).

    ``fetch_page(start, count)`` treats the whole scrolled-so-far list as one
    virtual sequence, indexed exactly like Voyager's ``start``/``count``, and
    slices it -- so from :func:`~netkeeper.linkedin.connections.run_connections_sync`'s
    point of view this reads exactly like paging an API, one gated,
    human-paced unit of work at a time (spec 9.5, 9.6), even though the
    underlying mechanism is scrolling rather than a sequence of independent
    requests.

    **Every settle attempt spent without reaching what was asked for is a
    refusal, never a confirmed empty or short page (#173 review, F5(c)).** A
    page that never renders the list container at all is refused immediately
    (F5(b)); one that renders it but never grows enough within
    :data:`MAX_SETTLE_ATTEMPTS` tries is refused once those run out --
    ``Outcome.ROUTE_CHANGED`` either way. This module has no reliable way to
    tell "the list truly has fewer connections than asked for" apart from "a
    slow render never caught up in time", and guessing the friendlier of the
    two is exactly the mistake spec 9.3's fallback exists to not make;
    returning an honest, empty ``Ok`` page here would let the caller read a
    stall as a confirmed end of list. The cost is that a DOM run essentially
    never gets to *positively* declare "I saw everyone" the way an empty
    Voyager page can -- it only ever succeeds at handing over pages it is
    confident about, or gives up. Combined with
    :class:`~netkeeper.linkedin.connections.SyncResult`'s ``complete``
    refusing outright once a fallback has ever switched to this class (see
    the module docstring), that is the point, not a gap.

    A cancelled scroll (:attr:`~netkeeper.linkedin.browser.ScrollOutcome.cancelled`,
    spec 9.9's cooperative cancel) stops the settle loop at once and reports
    whatever was already accumulated, the same as running out of the page's
    own ``count`` -- it is not treated as a failure (#173 review, L5).

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
    same parameter :meth:`BrowserRun.scroll` itself takes; the default is real
    time. ``cancelled``, when given, is forwarded to every
    :meth:`~netkeeper.linkedin.browser.BrowserRun.scroll` call unchanged.
    """

    def __init__(
        self,
        run: BrowserRun,
        *,
        origin: str = LINKEDIN_ORIGIN,
        rng: random.Random | None = None,
        scroll_profile: ScrollProfile = DEFAULT_SCROLL_PROFILE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self._run = run
        self._origin = _require_dom_origin(origin)
        self._cards_expression = _cards_expression(self._origin)
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        self._scroll_profile = scroll_profile
        self._sleep = sleep
        self._cancelled = cancelled
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
            if outcome.cancelled:
                page = outcome.page
                break
            page = outcome.page
            _require_page_on_origin(page.url, self._origin)
            blocked = _classify_page_url(page.url)
            if blocked is not None:
                return SourcePage(outcome=blocked, final_url=page.url)
            raw = await _evaluate(page, self._cards_expression, what="the connections list DOM")
            if not isinstance(raw, Mapping) or "containerPresent" not in raw or "cards" not in raw:
                raise DomFetchError(
                    f"the connections list DOM read returned {_type_name(raw)}, not the"
                    " {containerPresent, cards} shape this module's own script produces"
                )
            if not raw["containerPresent"]:
                # F5(b): a missing container -- an error page, a wall rendered in
                # place, or a shape change -- is never "zero connections", and
                # scrolling again will not make a structural mismatch resolve.
                return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
            try:
                cards = _parse_cards(self.endpoint, raw["cards"])
            except RouteChanged:
                return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
            if len(cards) > len(self._cards):
                self._cards = list(cards)
            # Otherwise: no growth this attempt. Keep the loop's attempt
            # counter moving rather than retrying forever -- MAX_SETTLE_ATTEMPTS
            # bounds how many extra scrolls one page of work may spend waiting
            # for a slow render before the `for`/`else` below gives up.
        else:
            # F5(c): every attempt spent without ever reaching start + count.
            # See the class docstring for why this is a refusal, not a page.
            return SourcePage(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)

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
            return await self._run.scroll(plan, cancelled=self._cancelled)
        return await self._run.scroll(plan, sleep=self._sleep, cancelled=self._cancelled)


# --- contact-info overlay -----------------------------------------------------


def _contact_info_expression(origin: str) -> str:
    """The script :class:`DomContactInfoSource` hands to ``page.evaluate``.

    Checks ``location.origin`` first, the same as :func:`_cards_expression`
    and for the same reason (#173 review, L3). Then looks for
    :data:`CONTACT_INFO_DIALOG_SELECTOR`; its absence answers
    ``{dialogPresent: false}`` without reading anything else (#173 review,
    F5(d)) -- an overlay that never rendered its dialog (an error page, a
    wall) is not "nobody shared any contact info". Every query below is
    scoped to that dialog element (#173 review, F7): the surrounding profile
    page can carry its own ``mailto:`` link (a bio mentioning an email) or
    website that has nothing to do with what this overlay actually shows.

    Built on ``mailto:`` and ``tel:`` links (a standards-based convention,
    not a LinkedIn-specific one -- see the module docstring) rather than a
    guessed class name: an email is read from the first ``mailto:`` link in
    the dialog, phone numbers from every ``tel:`` link, and every other
    ``http(s)`` link is sorted into a Twitter/X handle (``twitter.com`` or
    ``x.com``), a website, or dropped -- LinkedIn's own domain and a handful
    of named url shorteners (:data:`LINK_SHORTENER_HOSTS`) are never a
    "website the person shared" (#173 review, F7). A Twitter/X handle is the
    *first* path segment, not the last -- ``.../i/status/12345`` is a tweet
    permalink, not a profile, and its first segment ``i`` is X's own reserved
    namespace, so that one specific first segment is rejected rather than
    kept as if it were a handle (#173 review, F7).
    """
    return (
        "() => {"
        f"if (location.origin !== {json.dumps(origin)}) "
        f"{{ throw new Error({json.dumps(_ORIGIN_MISMATCH_MARKER)}); }}"
        f"const dialog = document.querySelector({json.dumps(CONTACT_INFO_DIALOG_SELECTOR)});"
        "if (!dialog) { return {dialogPresent: false}; }"
        "const email = dialog.querySelector('a[href^=\"mailto:\"]');"
        "const phones = Array.from(dialog.querySelectorAll('a[href^=\"tel:\"]'));"
        "const links = Array.from(dialog.querySelectorAll('a[href^=\"http\"]'));"
        "const websites = [];"
        "const twitterHandles = [];"
        f"const shorteners = new Set({json.dumps(sorted(LINK_SHORTENER_HOSTS))});"
        "for (const a of links) {"
        "  let url;"
        "  try { url = new URL(a.href); } catch (e) { continue; }"
        "  const host = url.hostname.replace(/^www\\./, '');"
        "  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) { continue; }"
        "  if (host === 'twitter.com' || host === 'x.com') {"
        "    const segments = url.pathname.split('/').filter(Boolean);"
        "    const first = segments[0];"
        "    if (first && first !== 'i') { twitterHandles.push(first); }"
        "    continue;"
        "  }"
        "  if (shorteners.has(host)) { continue; }"
        "  websites.push(a.href);"
        "}"
        "return {"
        "  dialogPresent: true,"
        "  email: email ? email.getAttribute('href').replace(/^mailto:/, '') : null,"
        "  phones: phones.map(a => a.getAttribute('href').replace(/^tel:/, '')),"
        "  websites: websites,"
        "  twitterHandles: twitterHandles,"
        "};"
        "}"
    )


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
    """``raw``, the contact-info script's result, as a :class:`ContactInfo`
    (dialog presence already confirmed by the caller).

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
    overlay" step, not a way around it. That navigation is a **second** page
    load for whatever profile visit this call belongs to -- spec 9.4's step 1
    ("navigate to the profile page") already counted one ``profile_visits``
    budget unit and one real page view against the account's heat for this
    contact; this overlay navigation is not separately budgeted or counted
    (``linkedin/`` has no budget or database access of its own, ADR 0005), so
    a caller that wires this in is spending a second real navigation per
    fallback visit that its own accounting does not see (#173 review, L4).
    That is a deliberate scope boundary, not an oversight -- flagged here for
    whoever wires this in.

    **This class carries no URN of its own (#173 review, F7) and is not wired
    into enrichment.** :class:`~netkeeper.linkedin.contact_info.ContactInfoSource`
    answers by ``public_id`` alone; nothing here confirms the profile actually
    on screen belongs to the contact a caller thinks it visited (a slug can
    change hands, the same reasoning :mod:`netkeeper.linkedin.dom`'s module
    docstring gives for connections rows). ``netkeeper.crm.apply.apply_harvest``
    already refuses to write a harvest whose profile URN does not match the
    contact's stored one (its own "whose profile it is" check) -- but that
    check runs on ``details.urn`` from the *profile-details* harvest, not on
    anything this class reads. Before any future PR wires
    ``DomContactInfoSource``/``FallbackContactInfoSource`` into enrichment, the
    caller must guarantee this overlay is only ever read for a visit whose
    URN check on that same visit has already passed -- for example, by
    reading contact info only after a successful, URN-matched profile-details
    harvest for the same navigation, never on its own. Until that guarantee
    is written and tested where the wiring actually happens, treat this class
    as unwired.
    """

    def __init__(self, run: BrowserRun, *, origin: str = LINKEDIN_ORIGIN) -> None:
        self._run = run
        self._origin = _require_dom_origin(origin)
        self._contact_info_expression = _contact_info_expression(self._origin)

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
        raw = await _evaluate(
            page, self._contact_info_expression, what="the contact-info overlay DOM"
        )
        if not isinstance(raw, Mapping) or "dialogPresent" not in raw:
            raise DomFetchError(
                f"the contact-info overlay DOM read returned {_type_name(raw)}, not the"
                " {dialogPresent, ...} shape this module's own script produces"
            )
        if not raw["dialogPresent"]:
            # F5(d): an overlay that never rendered its dialog must not read as
            # "Ok, nobody shared anything" -- ROUTE_CHANGED is what
            # apply_harvest already treats as HarvestResult.UNREADABLE, the
            # same as an unparseable API response, feeding enrichment's own
            # two-unreadable-in-a-row stop.
            return ContactInfoResult(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
        try:
            info = _parse_contact_info_dom(self.endpoint, raw)
        except RouteChanged:
            return ContactInfoResult(outcome=Outcome.ROUTE_CHANGED, final_url=page.url)
        return ContactInfoResult(outcome=Outcome.OK, final_url=page.url, info=info)
