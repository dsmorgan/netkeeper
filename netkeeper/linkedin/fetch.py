"""The in-page Voyager fetch: :mod:`netkeeper.linkedin.voyager`'s ``VoyagerFetch``, wired
to a real tab (P2-01, #150).

:mod:`netkeeper.linkedin.voyager` defines ``VoyagerFetch`` as a callable protocol on
purpose and stops there -- making the actual request needs a live page, and a browser
or CDP type crossing into that module would break spec 9.10's boundary. This module is
the other half: :class:`PageVoyagerFetch` runs a real ``fetch()`` *inside* the page a
:class:`~netkeeper.linkedin.browser.BrowserRun` holds, via ``page.evaluate`` -- the
same mechanism the real web client uses, so the request carries the tab's own session
cookies and client hints (spec 9.3) rather than anything netkeeper constructs.

**Nothing here is importable by ``voyager.py`` or ``classify.py``, only the reverse.**
This module depends on :mod:`netkeeper.linkedin.browser` (for the page) and on
:mod:`netkeeper.linkedin.voyager` and :mod:`netkeeper.linkedin.classify` (for the
request/response shapes and the outcome), never the other way around -- so the pure
modules stay exercisable by fixtures alone, with no browser in sight, exactly as their
own docstrings promise.

**Origin is fixed, not configurable, and checked twice.** Every real call runs against
:data:`LINKEDIN_ORIGIN`. The only way to point an instance anywhere else is the
``origin`` keyword, whose value goes through
:func:`~netkeeper.linkedin.strict_origin.parse_strict_origin` before anything else --
refusing a backslash, userinfo, or a path/query/fragment closes the parser
differential a reviewer found between ``urlsplit`` and a real browser's URL parser
(see that module's docstring) -- and the constructor then refuses anything that is
not :data:`LINKEDIN_ORIGIN` itself or this machine's own loopback. There is no config
value, flag, or environment variable that reaches this far, so a misconfigured run
cannot end up fetching from an arbitrary host. The loopback exception exists only so
the opt-in smoke suite (``tests/smoke/``) can point this class at a fixture server it
starts itself; :func:`netkeeper.linkedin.rehearse._require_neutral` refuses the
opposite way (never LinkedIn, always loopback) with the same rigor, through the same
strict parser.

**Every call also checks the page is actually there before running anything in it**
(F2 of the #168 review): a security reviewer showed that a page on some *other*
origin -- one a run's tab had merely been left on, or one it navigated to after this
instance was built -- could shadow ``window.fetch`` and read back whatever this
module handed to ``page.evaluate``, headers included. :meth:`PageVoyagerFetch.__call__`
therefore compares ``page.url``'s scheme, host, and port against this instance's
origin *before* evaluating anything, and refuses to proceed on a mismatch.

**The csrf-token value never reaches Python, at all.** The earlier version of this
module read the live ``JSESSIONID`` cookie over CDP (``context.cookies()``) and built
the ``csrf-token`` header in Python. A security reviewer showed that value transiting
Python was needless exposure: the in-page script now reads ``document.cookie``
*inside the page itself* and sets the header there, so the token is generated,
consumed, and discarded entirely inside the browser process. Python supplies every
*other* header (from the same constants :func:`~netkeeper.linkedin.voyager.build_headers`
uses) and the url; it never sees, logs, returns, or reports the cookie's value, and
``context.cookies()`` is not called anywhere in this file any more. When the page has
no readable ``JSESSIONID`` (not logged in, or the cookie is ``HttpOnly`` -- browsers
do not make an ``HttpOnly`` cookie visible to ``document.cookie`` by design, so this
is also the fallback for the day that assumption about LinkedIn's own cookie turns
out to be wrong) the script raises inside the page, and :meth:`PageVoyagerFetch.__call__`
turns that into a plain :class:`VoyagerFetchError` naming nothing but the cookie's
*name*. That specific failure mode -- and the more general one, an in-page ``fetch()``
that throws for any other reason, a cross-origin redirect's "Failed to fetch" among
them -- gets verified for real at the first live run; this is flagged here and in the
PR body rather than asserted, because nothing offline can prove a browser's own
cookie-visibility rules without a browser.

**The gate.** :func:`parse_ok` is the one obvious way to hand a fetched response to a
parser: it classifies first (spec 9.7) and only calls the parser when the outcome is
``Ok``, raising :class:`VoyagerNotOk` otherwise. A caller that reaches for
``parser(response.body)`` directly skips that check entirely.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Final
from urllib.parse import urlencode, urlsplit

from netkeeper.linkedin.browser import BrowserRun
from netkeeper.linkedin.classify import Outcome, classify
from netkeeper.linkedin.preflight import CSRF_COOKIE
from netkeeper.linkedin.strict_origin import NotAStrictOrigin, parse_strict_origin
from netkeeper.linkedin.voyager import (
    ACCEPT_HEADER,
    CSRF_HEADER_NAME,
    LANG_HEADER_VALUE,
    RESTLI_PROTOCOL_VERSION,
    VoyagerRequest,
    VoyagerResponse,
)

#: The only host a production fetch may run against.
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

#: Parsed once, at import time, so every comparison against it goes through the same
#: strict parser as any caller-supplied origin -- there is exactly one reading of
#: what "LinkedIn" means here, never a raw string compared a different way.
_LINKEDIN_ORIGIN_PARSED: Final = parse_strict_origin(LINKEDIN_ORIGIN)

#: Every endpoint constant in ``voyager.py`` starts with this. A request path that
#: does not is not a Voyager request, whatever origin it would otherwise reach.
VOYAGER_PATH_PREFIX: Final = "/voyager/api/"

#: The three loopback spellings a test may point this class at -- the same set
#: :mod:`netkeeper.linkedin.rehearse` allows for its own, opposite refusal.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: What the in-page script throws when ``document.cookie`` has no live JSESSIONID.
#: A string netkeeper itself chose and controls -- never anything a browser or a
#: page could echo a cookie value through -- so it is always safe to look for it in
#: the text of whatever exception ``page.evaluate`` raises.
_NO_CSRF_MARKER: Final = "netkeeper-fetch: no live JSESSIONID cookie readable on this page"

#: What the in-page script throws when ``location.origin`` does not match the
#: origin this instance is bound to, checked a second time *inside* the script
#: itself (#170 item 3). Same discipline as :data:`_NO_CSRF_MARKER`: a fixed
#: string netkeeper chose, never anything a page could echo back through.
_ORIGIN_MISMATCH_MARKER: Final = "netkeeper-fetch: page origin does not match the expected origin"


class NotLinkedInOrigin(ValueError):
    """An origin a :class:`PageVoyagerFetch` was asked to use is neither LinkedIn nor loopback."""


class VoyagerFetchError(RuntimeError):
    """The in-page fetch mechanism itself failed or returned something unreadable.

    Never a LinkedIn API change -- that is :class:`~netkeeper.linkedin.voyager.RouteChanged`,
    raised by a parser, or an :class:`Outcome` other than ``Ok`` from :func:`parse_ok`.
    This is for the fetch plumbing breaking: the page not being on the origin this
    instance is bound to, no CSRF cookie readable on the page, an in-page ``fetch()``
    that itself threw (a network error, a cross-origin redirect's "Failed to fetch"),
    or a result that is not the shape this module's own script produces.

    A caller of this module (P2-06 onward) has no :class:`VoyagerResponse` to
    classify when this is raised -- there was no response, only a failure to get
    one -- so it cannot be handed to :func:`parse_ok`. Spec 9.7's table has no row
    for it either, because every row there classifies an *answer* LinkedIn gave.
    Until P2-06 decides otherwise, the safe default is the same one an unclassifiable
    failure gets anywhere else in the extractor: this unit of work did not complete,
    it is not a checkpoint or a login wall (neither sets the session flag), and it is
    worth logging loudly and moving on rather than retrying blindly.
    """


def _require_fetchable_origin(origin: str) -> str:
    """``origin``, rebuilt and canonical, if a fetch may run against it.

    Raises :class:`NotLinkedInOrigin` for anything else. ``origin`` first goes
    through :func:`~netkeeper.linkedin.strict_origin.parse_strict_origin`, which
    refuses a backslash, userinfo, or anything past the authority before ``urlsplit``
    -- which disagrees with a real browser on some of those -- ever sees the string
    (see that module's docstring). What is left is compared against
    :data:`LINKEDIN_ORIGIN` and this machine's own loopback, on the *parsed and
    rebuilt* origin, never the original string: a scheme, a host, and a port, with
    no way for the two to differ again once parsing is done. There is no override:
    the only caller that ever passes something other than the default is a test, on
    purpose, against a server it started itself.
    """
    try:
        parsed = parse_strict_origin(origin)
    except NotAStrictOrigin as exc:
        raise NotLinkedInOrigin(f"{origin!r} is not a usable Voyager origin: {exc}") from exc
    if parsed == _LINKEDIN_ORIGIN_PARSED:
        return str(parsed)
    if parsed.scheme in ("http", "https") and parsed.host in _LOOPBACK_HOSTS:
        return str(parsed)
    raise NotLinkedInOrigin(
        f"a Voyager fetch may only run against {LINKEDIN_ORIGIN!r}, or this machine's"
        f" own loopback for tests, got {origin!r}"
    )


class PageVoyagerFetch:
    """Satisfies :class:`~netkeeper.linkedin.voyager.VoyagerFetch` from a live tab.

    ``run`` is the :class:`~netkeeper.linkedin.browser.BrowserRun` whose tab the
    request runs inside; every call reads it through :meth:`BrowserRun.ensure_page`,
    so a tab the user closed is reopened the same way any other in-page work recovers
    it (spec 9.9) -- but recovery restores the tab to wherever the run last
    navigated it, never to this instance's origin on its own, so a caller must
    already have put the tab on that origin (a ``run.goto(...)``) before the first
    call. ``origin`` defaults to :data:`LINKEDIN_ORIGIN` and should never be passed
    anything else outside a test (see the module docstring).
    """

    def __init__(self, run: BrowserRun, *, origin: str = LINKEDIN_ORIGIN) -> None:
        self._origin = _require_fetchable_origin(origin)
        self._run = run

    @property
    def origin(self) -> str:
        """The origin this instance fetches against. Read-only: set once, at construction."""
        return self._origin

    async def __call__(self, request: VoyagerRequest) -> VoyagerResponse:
        """Run ``request`` as a real in-page ``fetch()`` and return what came back.

        Every header but ``csrf-token`` is built here, from the same constants
        :func:`~netkeeper.linkedin.voyager.build_headers` uses, merged with
        ``request.headers`` (which can override any of them). ``csrf-token`` is not
        one of them: the in-page script reads it fresh from ``document.cookie`` on
        every call, and a caller cannot override it through ``request.headers`` --
        letting anything but the page's own live cookie supply that value is exactly
        what this module was rewritten not to do (see the module docstring's "the
        csrf-token value never reaches Python" section).
        """
        if not request.path.startswith(VOYAGER_PATH_PREFIX):
            raise ValueError(
                f"not a Voyager path (must start with {VOYAGER_PATH_PREFIX!r}): {request.path!r}"
            )
        page = await self._run.ensure_page()
        _require_page_on_origin(page.url, self._origin)
        base_headers = _base_headers(request.headers)
        url = self._url_for(request)
        try:
            raw = await page.evaluate(_fetch_expression(url, base_headers, origin=self._origin))
        except Exception as exc:
            if _ORIGIN_MISMATCH_MARKER in str(exc):
                # #170 item 3: the Python-side check above already read page.url, but
                # a page can navigate in the gap between that read and this evaluate
                # actually running -- this is the in-page script's own, atomic check
                # of the same thing, from inside the page at the moment it runs.
                raise VoyagerFetchError(
                    f"the tab navigated away from {self._origin!r} between the origin"
                    " check and the fetch itself; refusing to run a Voyager fetch from"
                    " a page that might not be able to be trusted"
                ) from exc
            if _NO_CSRF_MARKER in str(exc):
                raise VoyagerFetchError(
                    f"no live {CSRF_COOKIE} cookie readable on this page; log in to"
                    " LinkedIn in the netkeeper Chrome profile first"
                ) from exc
            raise VoyagerFetchError(
                "the in-page fetch failed (a network error, a cross-origin redirect,"
                " or the page navigating away mid-request)"
            ) from exc
        return _response_from(raw)

    def _url_for(self, request: VoyagerRequest) -> str:
        query = urlencode(request.query)
        return f"{self._origin}{request.path}" + (f"?{query}" if query else "")


def _base_headers(extra: Mapping[str, str]) -> dict[str, str]:
    """Every header :func:`~netkeeper.linkedin.voyager.build_headers` would set,
    except ``csrf-token`` -- which only the in-page script can supply, from the
    page's own live cookie (see the module docstring). Reads the same constants
    ``build_headers`` does, so the two cannot drift apart on the headers they share.

    ``extra`` (``request.headers``) can override any header here -- ``accept``
    among them, on purpose (see :meth:`PageVoyagerFetch.__call__`'s docstring) --
    except ``csrf-token`` itself, matched case-insensitively (#170 item 5): HTTP
    header names are case-insensitive, and a caller spelling it ``CSRF-Token`` was
    found to slip past a plain ``dict`` key check, land in this dict under its own
    casing, and then get *combined* with the script's own value rather than
    overridden by it -- ``fetch()``'s ``Headers`` merges same-name headers
    case-insensitively instead of replacing one. Dropping every casing of it here,
    before it ever reaches the in-page script, is what keeps that value entirely
    out of ``request.headers``'s reach, whatever a caller spells it as.
    """
    headers: dict[str, str] = {
        "accept": ACCEPT_HEADER,
        "x-restli-protocol-version": RESTLI_PROTOCOL_VERSION,
        "x-li-lang": LANG_HEADER_VALUE,
    }
    headers.update(
        (name, value) for name, value in extra.items() if name.lower() != CSRF_HEADER_NAME.lower()
    )
    return headers


def _require_page_on_origin(page_url: str, origin: str) -> None:
    """Refuse to evaluate anything unless the tab is actually on ``origin``.

    F2 of the #168 review: a page on some other origin -- left over from a previous
    navigation, or one the tab moved to after this instance was constructed -- could
    shadow ``window.fetch`` and read back the headers this module hands to
    ``page.evaluate``, cookie-derived csrf-token included. ``page_url`` is Chrome's
    own, already-resolved url (not a string this module has to distrust the parsing
    of, unlike a caller-supplied ``origin`` -- see :mod:`netkeeper.linkedin.strict_origin`),
    so a plain ``urlsplit`` compare of scheme, host, and port is enough here.
    """
    page = urlsplit(page_url)
    want = urlsplit(origin)
    if (page.scheme, page.hostname, page.port) != (want.scheme, want.hostname, want.port):
        raise VoyagerFetchError(
            f"the tab is not on {origin!r} (it is on"
            f" {page.scheme or '?'}://{page.hostname or '?'}"
            f"{f':{page.port}' if page.port else ''}); refusing to run a Voyager fetch"
            " from a page that might not be able to be trusted"
        )


def _fetch_expression(url: str, base_headers: Mapping[str, str], *, origin: str) -> str:
    """The script :meth:`PageVoyagerFetch.__call__` hands to ``page.evaluate``.

    ``PageLike.evaluate`` (spec 9.10 keeps it narrow) takes one expression string and
    no separate argument, so ``url``, ``base_headers``, and ``origin`` are embedded as
    JSON literals rather than passed alongside -- safe because JSON string syntax is a
    strict subset of a JavaScript string literal, so ``json.dumps``'s own escaping is
    already enough. ``credentials: 'same-origin'`` is what makes the fetch carry the
    tab's session cookies at all; without it a same-origin fetch still sends them by
    default in every browser netkeeper supports, but naming it is cheap insurance
    against ever changing that default by accident.

    **The very first thing the script does is check its own origin again (#170 item
    3).** :meth:`PageVoyagerFetch.__call__` already checks ``page.url`` in Python
    before calling this, but there is a real gap between that read and this script
    actually starting to run inside the page -- the tab is free to navigate in
    between. Checking again from inside the page, atomically with everything else
    the script does, closes that gap; a mismatch throws
    :data:`_ORIGIN_MISMATCH_MARKER` before touching a cookie or sending a request.

    The script then reads ``document.cookie``, finds ``JSESSIONID``, and strips its
    surrounding quotes the same way :func:`~netkeeper.linkedin.voyager.strip_jsessionid`
    does for the value this module used to read over CDP -- the value never crosses
    into Python at all now (see the module docstring). An absent or empty cookie
    throws :data:`_NO_CSRF_MARKER`, a fixed string ``__call__`` recognizes and turns
    into a :class:`VoyagerFetchError` that names nothing but the cookie's name.
    """
    return (
        "(async () => {"
        f"if (location.origin !== {json.dumps(origin)}) "
        f"{{ throw new Error({json.dumps(_ORIGIN_MISMATCH_MARKER)}); }}"
        "const m = document.cookie.match(/(?:^|;\\s*)JSESSIONID=([^;]*)/);"
        "let t = m ? decodeURIComponent(m[1]) : '';"
        "if (t.length >= 2 && t[0] === '\"' && t[t.length - 1] === '\"') { t = t.slice(1, -1); }"
        f"if (!t) {{ throw new Error({json.dumps(_NO_CSRF_MARKER)}); }}"
        "const headers = Object.assign({}, "
        f"{json.dumps(dict(base_headers))}, {{{json.dumps(CSRF_HEADER_NAME)}: t}});"
        f"const r = await fetch({json.dumps(url)}, "
        "{method: 'GET', credentials: 'same-origin', headers: headers});"
        "const body = await r.text();"
        "return {status: r.status, body: body, url: r.url};"
        "})()"
    )


def _response_from(raw: object) -> VoyagerResponse:
    """The in-page script's return value as a :class:`VoyagerResponse`.

    Raises :class:`VoyagerFetchError` for any shape other than the one
    :func:`_fetch_expression` produces -- this is not spec 9.7's classification
    (that happens once there is a real :class:`VoyagerResponse` to classify, in
    :func:`parse_ok`); it is this module's own contract with its own script.
    """
    if not isinstance(raw, Mapping):
        raise VoyagerFetchError(f"the in-page fetch returned {type(raw).__name__}, not an object")
    status = raw.get("status")
    body = raw.get("body")
    final_url = raw.get("url")
    if not isinstance(status, int) or isinstance(status, bool):
        raise VoyagerFetchError("the in-page fetch result has no numeric 'status'")
    if not isinstance(body, str):
        raise VoyagerFetchError("the in-page fetch result has no string 'body'")
    if not isinstance(final_url, str):
        raise VoyagerFetchError("the in-page fetch result has no string 'url'")
    return VoyagerResponse(status=status, body=body, final_url=final_url)


class VoyagerNotOk(Exception):
    """:func:`parse_ok` refused to parse a response that did not classify as ``Ok``.

    ``outcome`` is spec 9.7's classification, and it is what a caller catching this
    acts on -- stop the run and set the session flag on ``Checkpoint`` or
    ``LoggedOut``, retry with the escalating cooldown on ``Throttled``, and so on.
    This module only refuses to let the response reach a parser; deciding what
    happens next belongs to that caller (P2-06 onward), not here. ``response`` is
    attached for that decision, but the message repeats neither its body nor its
    url's query string: the body is somebody's own data on a real ``Ok`` fetch, and
    while none of the non-``Ok`` outcomes are expected to carry it, this module has
    no way to prove that of every one of them, so the message never risks it either.
    """

    def __init__(self, outcome: Outcome, response: VoyagerResponse) -> None:
        self.outcome = outcome
        self.response = response
        super().__init__(f"voyager response classified {outcome.value}, not ok")


def parse_ok[T](response: VoyagerResponse, parser: Callable[[str], T]) -> T:
    """Classify ``response`` and hand its body to ``parser`` only if it classifies ``Ok``.

    The one obvious way to call a parser on a fetched response (#150's gate): a
    caller that reaches for ``parser(response.body)`` directly skips classification
    (spec 9.7) entirely, and can hand a checkpoint interstitial's or a login wall's
    HTML to a parser that was never written to expect one. Every other outcome
    raises :class:`VoyagerNotOk` instead of calling ``parser`` at all.
    """
    outcome = classify(response.status, response.final_url, response.body)
    if outcome is not Outcome.OK:
        raise VoyagerNotOk(outcome, response)
    return parser(response.body)
