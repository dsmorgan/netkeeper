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

**Origin is fixed, not configurable.** Every real call runs against
:data:`LINKEDIN_ORIGIN`. The only way to point an instance anywhere else is the
``origin`` keyword, and the constructor refuses anything that is not
:data:`LINKEDIN_ORIGIN` itself or this machine's own loopback -- there is no config
value, flag, or environment variable that reaches this far, so a misconfigured run
cannot end up fetching from an arbitrary host. The loopback exception exists only so
the opt-in smoke suite (``tests/smoke/``) can point this class at a fixture server it
starts itself; :func:`netkeeper.linkedin.rehearse._require_neutral` refuses the
opposite way (never LinkedIn, always loopback) with the same rigor.

**Cookie values never leave this module.** The csrf-token header needs the live
``JSESSIONID`` cookie's value (spec 9.3), which only this module -- the one piece of
the extractor with a browser -- can read, over CDP, the same way
:mod:`netkeeper.linkedin.preflight` reads cookie names and expiry. The value is used
once, to build a header, and is never logged, returned to a caller, or included in any
exception message this module raises.

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
from netkeeper.linkedin.preflight import COOKIE_DOMAIN_SUFFIX, CSRF_COOKIE
from netkeeper.linkedin.voyager import VoyagerRequest, VoyagerResponse, build_headers

#: The only host a production fetch may run against.
LINKEDIN_ORIGIN: Final = "https://www.linkedin.com"

#: Every endpoint constant in ``voyager.py`` starts with this. A request path that
#: does not is not a Voyager request, whatever origin it would otherwise reach.
VOYAGER_PATH_PREFIX: Final = "/voyager/api/"

#: The three loopback spellings a test may point this class at -- the same set
#: :mod:`netkeeper.linkedin.rehearse` allows for its own, opposite refusal.
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})


class NotLinkedInOrigin(ValueError):
    """An origin a :class:`PageVoyagerFetch` was asked to use is neither LinkedIn nor loopback."""


class VoyagerFetchError(RuntimeError):
    """The in-page fetch mechanism itself failed or returned something unreadable.

    Never a LinkedIn API change -- that is :class:`~netkeeper.linkedin.voyager.RouteChanged`,
    raised by a parser, or an :class:`Outcome` other than ``Ok`` from :func:`parse_ok`.
    This is for the fetch plumbing breaking: no CSRF cookie in the jar, a cookie jar
    that could not be read at all, or an in-page ``fetch()`` result that is not the
    shape this module's own script produces.
    """


def _require_fetchable_origin(origin: str) -> str:
    """``origin``, if a fetch may run against it; :class:`NotLinkedInOrigin` otherwise.

    Refuses everything except :data:`LINKEDIN_ORIGIN` itself and this machine's own
    loopback. There is no override: the only caller that ever passes something other
    than the default is a test, on purpose, against a server it started itself.
    """
    try:
        split = urlsplit(origin)
    except ValueError as exc:
        raise NotLinkedInOrigin(f"{origin!r} is not a url a Voyager fetch can use") from exc
    if origin == LINKEDIN_ORIGIN:
        return origin
    if split.scheme in ("http", "https") and split.hostname in _LOOPBACK_HOSTS:
        return origin
    raise NotLinkedInOrigin(
        f"a Voyager fetch may only run against {LINKEDIN_ORIGIN!r}, or this machine's"
        f" own loopback for tests, got {origin!r}"
    )


class PageVoyagerFetch:
    """Satisfies :class:`~netkeeper.linkedin.voyager.VoyagerFetch` from a live tab.

    ``run`` is the :class:`~netkeeper.linkedin.browser.BrowserRun` whose tab the
    request runs inside; every call reads it through :meth:`BrowserRun.ensure_page`,
    so a tab the user closed is reopened the same way any other in-page work recovers
    it (spec 9.9). ``origin`` defaults to :data:`LINKEDIN_ORIGIN` and should never be
    passed to anything else outside a test (see the module docstring).
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

        Builds the header set fresh on every call, from :func:`build_headers` and the
        live ``JSESSIONID`` cookie -- a csrf token can rotate, and only this module
        can read the browser's own jar (spec 9.10's boundary keeps that off the pure
        side). ``request.headers`` is merged in as ``build_headers``'s ``extra``, so a
        caller that already knows an override (a different ``accept``, say) still
        wins on a key collision.
        """
        if not request.path.startswith(VOYAGER_PATH_PREFIX):
            raise ValueError(
                f"not a Voyager path (must start with {VOYAGER_PATH_PREFIX!r}): {request.path!r}"
            )
        raw_csrf = await self._read_jsessionid()
        headers = build_headers(raw_csrf, extra=request.headers)
        url = self._url_for(request)
        page = await self._run.ensure_page()
        raw = await page.evaluate(_fetch_expression(url, headers))
        return _response_from(raw)

    def _url_for(self, request: VoyagerRequest) -> str:
        query = urlencode(request.query)
        return f"{self._origin}{request.path}" + (f"?{query}" if query else "")

    async def _read_jsessionid(self) -> str:
        """The live ``JSESSIONID`` cookie's raw value, quotes and all.

        Read over CDP through the run's own context, the same call
        :mod:`netkeeper.linkedin.preflight` uses to read cookie *names*; this is the
        one place in netkeeper that reads a cookie *value*, because the csrf-token
        header needs it (spec 9.3). The value is returned only to
        :meth:`__call__`, which hands it straight to :func:`build_headers` and never
        logs, stores, or reports it -- and neither does this method: every error path
        below names the cookie by name, never by value.
        """
        domain_suffix = (
            COOKIE_DOMAIN_SUFFIX if self._origin == LINKEDIN_ORIGIN else _host(self._origin)
        )
        try:
            jar = await self._run.context.cookies()
        except Exception as exc:
            raise VoyagerFetchError("could not read the browser's cookie jar") from exc
        for entry in jar:
            if str(entry.get("name", "")) != CSRF_COOKIE:
                continue
            domain = str(entry.get("domain", "")).lstrip(".")
            if domain != domain_suffix and not domain.endswith(f".{domain_suffix}"):
                continue
            value = entry.get("value")
            if isinstance(value, str) and value:
                return value
        raise VoyagerFetchError(
            f"no live {CSRF_COOKIE} cookie for this profile; log in to LinkedIn in the"
            " netkeeper Chrome profile first"
        )


def _host(origin: str) -> str:
    return urlsplit(origin).hostname or origin


def _fetch_expression(url: str, headers: Mapping[str, str]) -> str:
    """The script :meth:`PageVoyagerFetch.__call__` hands to ``page.evaluate``.

    ``PageLike.evaluate`` (spec 9.10 keeps it narrow) takes one expression string and
    no separate argument, so ``url`` and ``headers`` are embedded as JSON literals
    rather than passed alongside -- safe because JSON string syntax is a strict
    subset of a JavaScript string literal, so ``json.dumps``'s own escaping is
    already enough. ``credentials: 'same-origin'`` is what makes the fetch carry the
    tab's session cookies at all; without it a same-origin fetch still sends them by
    default in every browser netkeeper supports, but naming it is cheap insurance
    against ever changing that default by accident.
    """
    return (
        "(async () => {"
        f"const r = await fetch({json.dumps(url)}, "
        f"{{method: 'GET', credentials: 'same-origin', headers: {json.dumps(dict(headers))}}});"
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
