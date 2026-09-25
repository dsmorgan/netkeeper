"""The DOM fallback for the contact-info overlay (P2-08). Not wired to any job.

P2-08 built two DOM fallbacks for spec 9.3's "In-page API first, DOM second": the
connections list's infinite scroll and the contact-info overlay. The connections
half is gone (#187's review): the #149 capture showed LinkedIn serves the list
through ``flagship-web``, and :class:`~netkeeper.linkedin.page_connections.PageConnections`
now reads the page's own answers (ADR 0006) instead of scraping the DOM with
selectors that were authored, never seen on the live page, and found nothing on
the first supervised run.

What is left is :class:`DomContactInfoSource`, which implements
:class:`~netkeeper.linkedin.contact_info.ContactInfoSource` by reading the overlay a
navigation to ``/in/<public-id>/overlay/contact-info/`` renders. Nothing wires it:
enrichment reads contact info through the in-page API today, and the enrichment
lane moves it onto ADR 0006's seam, reading the overlay's own navigation answer
after the one permitted click. That lane decides whether this module goes too.

**Read-only.** The source navigates with an ordinary ``goto`` and inspects what
rendered through ``page.evaluate`` scripts that query the DOM and return plain data.
It never writes to the page and never clicks. It calls nothing
``tests/test_browser_safety.py`` forbids, and it is registered there as a browser
module, never imported from a request handler (spec 9.9).

**Selectors are authored, not captured.** Where a selector rests on something
standard (a ``mailto:`` link, a ``role="dialog"`` overlay) it says so; the rest is
marked with the date it was authored. A wrong selector degrades to
``Outcome.ROUTE_CHANGED`` rather than to wrong data: see
:func:`_parse_contact_info_dom` and the dialog checks in :class:`DomContactInfoSource`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

from netkeeper.linkedin.browser import BrowserRun, PageLike
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.contact_info import ContactInfoResult
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import ContactInfo, RouteChanged

log = logging.getLogger(__name__)

#: The only host a production DOM read may run against -- same value and same
#: reasoning as netkeeper.linkedin.fetch.LINKEDIN_ORIGIN: fixed, never
#: configurable, with a loopback exception that exists only for the smoke suite.
#: Pinned literally in tests/test_linkedin_dom.py, per CLAUDE.md's rule for a
#: safety-relevant constant (#173 review, R15).
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

# --- selectors: authored, not captured (see the module docstring) -----------

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
#: person shared through this overlay. A real profile page can render more
#: than one element with this role at once (a messaging overlay is a dialog
#: too), so this selector alone only finds *candidates* -- see
#: :data:`CONTACT_INFO_HEADING_TEXT` and :func:`_contact_info_expression`
#: for how exactly one is chosen (#174 item 1, revised by its own review --
#: **both** a heading and a link-back are required, not either). Authored
#: 2026-09-23.
CONTACT_INFO_DIALOG_SELECTOR: Final = '[role="dialog"]'

#: The overlay's own heading text, matched *exactly* (after trimming and
#: collapsing internal whitespace, case-insensitively) -- **required**, not
#: merely one of two alternatives, for a dialog to qualify as the
#: contact-info overlay (#174 item 1, #176 review H1/L1). A `startsWith`
#: prefix match let "Contact info shared with advertisers" -- a real,
#: differently-scoped LinkedIn dialog -- through; an exact match refuses it
#: instead, and a localized heading ("Kontaktinfo") refuses too rather than
#: guess a translation, which fails safe (flagged for #149: a real capture
#: would tell us whether to add known translations here, never to guess
#: one). LinkedIn's own title for this overlay, the same "ordinary HTML
#: convention" a heading element is (see the module docstring). Authored
#: 2026-09-23.
CONTACT_INFO_HEADING_TEXT: Final = "contact info"

#: Url shorteners excluded from "websites" (#173 review, F7): LinkedIn's own
#: (``lnkd.in``) and the handful of others common enough that a link through
#: one is more likely tracking noise than a website the person meant to
#: share. Not exhaustive -- a new one showing up is a missed website, not a
#: wrong one, so the list stays short and named rather than guessed at.
#: Authored 2026-09-23.
LINK_SHORTENER_HOSTS: Final[frozenset[str]] = frozenset(
    {"lnkd.in", "bit.ly", "t.co", "tinyurl.com"}
)

#: X/Twitter path segments that are the site's own reserved namespace, never a
#: person's handle (#174 item 2, extended by #176 review L7). Compared
#: case-insensitively. ``i`` was the only one excluded before #174 (a tweet
#: permalink's own first segment, ``/i/status/...``); the rest is a
#: reasonable, named list -- not exhaustive, the same trade-off
#: :data:`LINK_SHORTENER_HOSTS` makes: a new reserved path showing up is a
#: missed exclusion, not a wrong handle. :data:`_X_HANDLE_PATTERN` is the
#: other half of the guard, for a segment that is not a *real X handle* by
#: shape even though it is not on this list either. Authored 2026-09-23.
RESERVED_X_PATHS: Final[frozenset[str]] = frozenset(
    {
        "i",
        "intent",
        "share",
        "home",
        "search",
        "hashtag",
        "explore",
        "settings",
        "messages",
        "notifications",
        "login",
        "signup",
        "compose",
        "tos",
        "privacy",
        "account",
        "about",
        "bookmarks",
        "lists",
        "logout",
        "jobs",
        "communities",
        "download",
        "oauth",
        "en",
        "premium",
        "help",
        "terms",
    }
)

#: A real X/Twitter handle's own shape (1-15 chars, letters/digits/underscore
#: -- X's documented username rule), checked in addition to
#: :data:`RESERVED_X_PATHS` (#176 review L7): a first path segment that is
#: not on the denylist can still fail to look like a handle at all (a purely
#: numeric id from a misread url, a segment with a dot or a space), and
#: guessing it is one anyway is exactly the mistake this module's own
#: fallback exists not to make.
_X_HANDLE_PATTERN: Final = r"^[A-Za-z0-9_]{1,15}$"

DOM_CONTACT_INFO_ENDPOINT: Final = "dom/contact-info-overlay"

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
    before every ``evaluate`` call (#173 review, R9);
    :func:`_contact_info_expression`'s own
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

    This is spec 9.3's "attack your own fallback" case: a login wall at the
    overlay's url renders no contact info at all, and without this check that
    would read as an honest, empty overlay rather than the ``LoggedOut`` it
    actually is.
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


# --- contact-info overlay -----------------------------------------------------


def _contact_info_expression(origin: str, public_id: str) -> str:
    """The script :class:`DomContactInfoSource` hands to ``page.evaluate``.

    Checks ``location.origin`` first (#173 review, L3), so a tab that navigated
    away between the Python-side check and this script is never read. Then finds every
    :data:`CONTACT_INFO_DIALOG_SELECTOR` candidate on the page and keeps only
    the ones that *qualify* as the contact-info overlay (#174 item 1, revised
    by the #176 review's H1): a dialog carrying **both** a heading whose text
    exactly matches :data:`CONTACT_INFO_HEADING_TEXT` (trimmed, internal
    whitespace collapsed, case-insensitive) **and** a link back to
    ``/in/<public_id>`` for the ``public_id`` this call navigated to and
    already knows. **Both, not either** -- the heading alone let a promo
    dialog ("Contact info shared with advertisers") through, and the
    link-back alone let a docked chat with the *same* person qualify: a
    chat's header commonly links to that person's own profile too, so on a
    page with only a chat open (no contact-info overlay at all) the old
    "either" rule handed back the chat's own email and links (#176 review
    H1, the reviewer's probe case A). A real profile page can render more
    than one ``role="dialog"`` element at once (a messaging overlay is a
    dialog too), and picking blindly among them risks reading whatever
    unrelated dialog happens to render first. The link-back check itself
    only trusts a link whose own origin is this page's origin or
    ``linkedin.com`` (#176 review M2) -- ``https://medium.example/in/<id>``
    must not qualify a dialog just because its path happens to contain
    ``/in/<id>`` -- and compares the extracted id case-insensitively, after
    percent-decoding, against ``public_id``, also lower-cased on the Python
    side before it is even embedded (#176 review M13: an upper-case or
    percent-encoded id must still match). Exactly one qualifying dialog answers
    ``{dialogPresent: true, ...}``; zero or more than one answers
    ``{dialogPresent: false}`` without reading anything else (#173 review,
    F5(d); #174 item 1) -- an overlay that never rendered its dialog (an
    error page, a wall), and an ambiguous page with more than one candidate,
    are both refused the same way a caller cannot trust. A heading that would
    only match after translation (a localized "Kontaktinfo") also refuses
    rather than guess -- fails safe, flagged for #149. Nested dialogs (an
    outer modal wrapper around an inner one, both containing the same
    heading and link-back as descendants) also refuse as ambiguous, for the
    same "more than one candidate" reason -- also flagged for #149, since a
    real capture would show whether LinkedIn's own markup ever nests this way.
    Every content query below is scoped to that one dialog element (#173
    review, F7): the surrounding profile page can carry its own ``mailto:``
    link (a bio mentioning an email) or website that has nothing to do with
    what this overlay actually shows.

    Built on ``mailto:`` and ``tel:`` links (a standards-based convention,
    not a LinkedIn-specific one -- see the module docstring) rather than a
    guessed class name: an email is read from the first ``mailto:`` link in
    the dialog, phone numbers from every ``tel:`` link, and every other
    ``http(s)`` link is sorted into a Twitter/X handle (``twitter.com`` or
    ``x.com``), a website, or dropped -- LinkedIn's own domain and a handful
    of named url shorteners (:data:`LINK_SHORTENER_HOSTS`) are never a
    "website the person shared" (#173 review, F7). A Twitter/X handle is the
    *first* path segment, not the last -- ``.../i/status/12345`` is a tweet
    permalink, not a profile -- and that first segment must both miss
    :data:`RESERVED_X_PATHS`, X's own reserved namespace, *and* look like a
    real handle by shape (:data:`_X_HANDLE_PATTERN`, #176 review L7): a
    denylist alone only excludes what it happens to name, and a segment that
    is not a plausible handle at all (too long, punctuation, whitespace) is
    rejected on shape regardless of whether this module's own list has caught
    up to it (#173 review, F7; #174 item 2).
    """
    return (
        "() => {"
        f"if (location.origin !== {json.dumps(origin)}) "
        f"{{ throw new Error({json.dumps(_ORIGIN_MISMATCH_MARKER)}); }}"
        f"const wantId = {json.dumps(public_id.strip().lower())};"
        f"const headingExact = {json.dumps(CONTACT_INFO_HEADING_TEXT)};"
        f"const pageOrigin = {json.dumps(origin)};"
        "function normalizeText(text) {"
        "  return (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();"
        "}"
        "function linkedProfileId(href) {"
        "  let url;"
        "  try { url = new URL(href || '', location.href); } catch (e) { return null; }"
        "  const host = url.hostname.replace(/^www\\./, '').toLowerCase();"
        "  const sameOrigin = url.origin === pageOrigin;"
        "  const isLinkedin = host === 'linkedin.com' || host.endsWith('.linkedin.com');"
        "  if (!sameOrigin && !isLinkedin) { return null; }"
        "  const m = url.pathname.match(/^\\/in\\/([^/?#]+)/);"
        "  if (!m) { return null; }"
        "  try { return decodeURIComponent(m[1]); } catch (e) { return null; }"
        "}"
        "function isContactInfoDialog(el) {"
        "  const headings = Array.from(el.querySelectorAll('h1,h2,h3,h4,h5,h6'));"
        "  const hasHeading = headings.some(h => normalizeText(h.textContent) === headingExact);"
        "  if (!hasHeading) { return false; }"
        "  const links = Array.from(el.querySelectorAll('a[href*=\"/in/\"]'));"
        "  return links.some(a => {"
        "    const id = linkedProfileId(a.getAttribute('href'));"
        "    return id !== null && id.toLowerCase() === wantId;"
        "  });"
        "}"
        "const dialogs = Array.from("
        f"document.querySelectorAll({json.dumps(CONTACT_INFO_DIALOG_SELECTOR)}));"
        "const qualifying = dialogs.filter(isContactInfoDialog);"
        "if (qualifying.length !== 1) { return {dialogPresent: false}; }"
        "const dialog = qualifying[0];"
        "const email = dialog.querySelector('a[href^=\"mailto:\"]');"
        "const phones = Array.from(dialog.querySelectorAll('a[href^=\"tel:\"]'));"
        "const links = Array.from(dialog.querySelectorAll('a[href^=\"http\"]'));"
        "const websites = [];"
        "const twitterHandles = [];"
        f"const shorteners = new Set({json.dumps(sorted(LINK_SHORTENER_HOSTS))});"
        f"const reservedXPaths = new Set({json.dumps(sorted(RESERVED_X_PATHS))});"
        f"const handlePattern = new RegExp({json.dumps(_X_HANDLE_PATTERN)});"
        "for (const a of links) {"
        "  let url;"
        "  try { url = new URL(a.href); } catch (e) { continue; }"
        "  const host = url.hostname.replace(/^www\\./, '');"
        "  if (host === 'linkedin.com' || host.endsWith('.linkedin.com')) { continue; }"
        "  if (host === 'twitter.com' || host === 'x.com') {"
        "    const segments = url.pathname.split('/').filter(Boolean);"
        "    const first = segments[0];"
        "    if ("
        "      first && !reservedXPaths.has(first.toLowerCase()) && handlePattern.test(first)"
        "    ) {"
        "      twitterHandles.push(first);"
        "    }"
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

    **Chooses among possibly several dialogs on the page (#174 item 1,
    revised by the #176 review's H1).** A real profile page can render more
    than one ``role="dialog"`` element at once -- a messaging overlay is a
    dialog too -- so this reads only the one dialog that carries **both** a
    heading titled "Contact info" **and** a link back to the ``public_id``
    this call navigated to; the link-back alone used to be enough, which let
    a docked chat with the same person qualify (its header commonly links to
    that person's profile too), returning the chat's own email and links
    instead of refusing when no contact-info overlay was actually open.
    Refuses as :attr:`~netkeeper.linkedin.classify.Outcome.ROUTE_CHANGED`
    when zero or more than one dialog qualifies: see
    :func:`_contact_info_expression`.

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

    @property
    def endpoint(self) -> str:
        return DOM_CONTACT_INFO_ENDPOINT

    async def fetch_contact_info(self, public_id: str) -> ContactInfoResult:
        if not public_id.strip():
            raise ValueError("public_id is empty")
        public_id = public_id.strip()
        path = CONTACT_INFO_OVERLAY_PATH_TEMPLATE.format(public_id=public_id)
        page = await self._run.goto(f"{self._origin}{path}")
        _require_page_on_origin(page.url, self._origin)
        blocked = _classify_page_url(page.url)
        if blocked is not None:
            return ContactInfoResult(outcome=blocked, final_url=page.url)
        # Built per call, not cached in __init__ (#174 item 1): the dialog
        # qualification check needs this call's own public_id.
        expression = _contact_info_expression(self._origin, public_id)
        raw = await _evaluate(page, expression, what="the contact-info overlay DOM")
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
