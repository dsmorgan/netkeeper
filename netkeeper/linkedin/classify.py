"""netkeeper.linkedin.classify: what a LinkedIn response actually means (spec 9.7).

Pure, behind the extractor boundary (spec 9.10, ADR 0005): a status code, a
url, and a body come in; an :class:`Outcome` goes out. No models, no session,
no I/O -- this module only ever looks at what a caller already fetched.

The reason this is its own step, rather than every job switching on
``response.status`` itself, is that the status code is not trustworthy on its
own. Two shapes make that concrete, and both are covered in the tests:

* A throttle response can come back wearing a checkpoint's clothes. LinkedIn
  sometimes answers a rate-limited request with ``429`` whose body is the
  same challenge page a real checkpoint serves, not a throttle message. Read
  the status first and this looks retryable; it is a checkpoint, and
  retrying a checkpoint burns the account (spec 9.7's Action column;
  CLAUDE.md's browser-identity rule). So a url or body pointing at
  ``/checkpoint/`` or ``/challenge/`` outranks every status-based signal,
  ``429`` included.
* A login wall can come back wearing a success's clothes. The Voyager API
  (9.3) is fetched in-page rather than navigated to, so LinkedIn does not
  need an HTTP redirect to put a request back at the login page -- it can
  just answer the original request url with ``200`` and the login page's
  HTML instead of JSON. Read the status first and this looks like ``Ok``, or
  at best an unrecognized shape; it is ``LoggedOut``, and the session flag
  needs to be set so nothing keeps retrying against a session that no
  longer exists.

Because of that second case, a url or body naming ``/login``, ``/authwall``,
or ``/uas/`` is checked the same way the checkpoint paths are, even though
spec 9.7's table only spells out "redirect" for that row and not "or body
pointing at" the way it does for the checkpoint row. Both a followed
redirect and a login page served in place of the JSON land a request on the
same page; only one of the two ways LinkedIn has of saying so changes
``url``, and the other only shows up in ``body``. Treating the two rows the
same way is this module's one interpretive call beyond the table -- spec 9.7
is amended to say so; see the PR that introduced it for the fuller argument.

The body is only ever scanned for those paths when it is *not* recognizable
JSON. A real Voyager response is somebody's own data, and that data
routinely contains a path-shaped substring with no bearing on the session at
all -- a contact's personal website ending in ``/login``, a tracking url
carrying ``/checkpoint/``, a headline that happens to mention
``/challenge/``. A checkpoint interstitial and a login wall are always HTML,
never a JSON object or array, so restricting the body scan to a body that
already failed the JSON-shape check catches both real cases with no false
positive from a JSON body's own content. The url is still always checked,
JSON body or not, because a url is structure the response chose, not data a
contact wrote.

:func:`is_retryable` is the other half of "no retry on Checkpoint" (P2-03):
the job loop that will eventually call :func:`classify` (P2-06 onward) asks
it before attempting the same unit of work again, rather than every job
re-deriving which outcomes are worth another attempt.
"""

from __future__ import annotations

import enum
import json
from typing import Final

_CHECKPOINT_PATHS: Final[tuple[str, ...]] = ("/checkpoint/", "/challenge/")
_LOGGED_OUT_PATHS: Final[tuple[str, ...]] = ("/login", "/authwall", "/uas/")


class Outcome(enum.StrEnum):
    """The six outcomes spec 9.7 classifies a response into."""

    OK = "ok"
    """HTTP 200 with a body :func:`classify` can parse as a JSON object or array."""

    THROTTLED = "throttled"
    """HTTP 429, or LinkedIn's own HTTP 999, with no checkpoint signal underneath.

    Action (spec 9.7, enforced by a future caller, not here): escalating
    cooldown, raise heat, at most 3 attempts; two consecutive throttled units
    abort the run.
    """

    CHECKPOINT = "checkpoint"
    """A url or body pointing at ``/checkpoint/`` or ``/challenge/``, at any
    status code. Action: stop the run, set the session flag, banner in the
    UI. Never retry -- see :func:`is_retryable`.
    """

    LOGGED_OUT = "logged_out"
    """HTTP 401, or a url or body pointing at ``/login``, ``/authwall``, or
    ``/uas/``. Action: stop, set the session flag, banner says log in to the
    netkeeper Chrome profile.
    """

    NOT_FOUND = "not_found"
    """HTTP 404. Action: terminal for the contact this run; increments a
    not-found streak used by spec 9.8 (a caller's concern, not this module's).
    """

    ROUTE_CHANGED = "route_changed"
    """HTTP 200 with a body that is not recognizable JSON, or HTTP 400 on a
    known endpoint -- and, as the conservative default, any other status
    code spec 9.7's table does not name (403, 500, 502, 503, and anything
    else unfamiliar). The table has no row for a server error or an
    unrecognized status, and guessing one is retryable is not a call this
    module is positioned to make, so it folds into the same "something is
    off, do not trust this endpoint right now" outcome as an unrecognized
    200 body. Action: give up on that endpoint for the run, log loudly,
    fall back to DOM if one exists.
    """


_RETRYABLE: Final[frozenset[Outcome]] = frozenset({Outcome.THROTTLED})
"""The only outcome spec 9.7's Action column describes as worth attempting again."""


def is_retryable(outcome: Outcome) -> bool:
    """Whether the unit of work that produced ``outcome`` is worth attempting again.

    Only :attr:`Outcome.THROTTLED` is -- and even then, spec 9.7 caps it at
    three attempts and aborts the run after two throttled units in a row,
    which is the calling job's responsibility (P2-06 onward), not this
    function's. Every other outcome, ``Checkpoint`` above all, is False: this
    is the one place "no retry on Checkpoint" is decided, so a future job
    loop asks here instead of re-deriving it, and a change to that rule has
    exactly one place to change.
    """
    return outcome in _RETRYABLE


def classify(response: int, url: str, body: str) -> Outcome:
    """Map a LinkedIn response to the outcome spec 9.7 says it is.

    ``response`` is the final HTTP status code -- after any redirect a fetch
    or page navigation already followed (9.3), never a bare 3xx. ``url`` is
    the url that response actually came from, likewise final. ``body`` is
    its raw text, JSON or HTML alike; this function decides which it is.

    Checked in this order, and the order is the point (see the module
    docstring): a checkpoint signal outranks everything, a logged-out signal
    outranks the remaining status-based rows, and only once neither applies
    does the status code get to speak for itself.
    """
    body_is_json = _is_recognizable_json(body)
    if _mentions(url, body, _CHECKPOINT_PATHS, scan_body=not body_is_json):
        return Outcome.CHECKPOINT
    if response == 401 or _mentions(url, body, _LOGGED_OUT_PATHS, scan_body=not body_is_json):
        return Outcome.LOGGED_OUT
    if response == 429 or response == 999:
        return Outcome.THROTTLED
    if response == 404:
        return Outcome.NOT_FOUND
    if response == 400:
        return Outcome.ROUTE_CHANGED
    if response == 200 and body_is_json:
        return Outcome.OK
    return Outcome.ROUTE_CHANGED


def _mentions(url: str, body: str, paths: tuple[str, ...], *, scan_body: bool) -> bool:
    """True when the url, or (when ``scan_body``) the body, contains any of ``paths``.

    The url is structure the response itself chose and is always checked: an
    actual redirect changes it, and it never carries a contact's own data.
    The body is only scanned when ``scan_body`` is true -- see the module
    docstring for why a recognizable-JSON body is excluded.
    """
    if any(path in url for path in paths):
        return True
    return scan_body and any(path in body for path in paths)


def _is_recognizable_json(body: str) -> bool:
    """True when ``body`` parses as a JSON object or array.

    Every LinkedIn API response this tool expects is one of those two
    shapes. A bare string, number, ``true``, or ``null`` is syntactically
    valid JSON but not a shape any Voyager endpoint returns, so it is
    treated the same as text that does not parse at all: an unrecognized
    shape, not ``Ok``.
    """
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict | list)
