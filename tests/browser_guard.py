"""The pieces of ``tests/conftest.py``'s real-browser guard that tests import (#293 review)."""

from __future__ import annotations

from urllib.parse import urlsplit


class RealBrowserBlocked(BaseException):
    """A test tried to reach a real Chrome over CDP. Never caught by production code.

    A ``BaseException`` on purpose: ``AttachBrowserProvider._attach`` turns any
    ``Exception`` from its connector into ``BrowserUnavailable``, and the worker and
    the scheduler treat that as "retry later". This must fail the test loudly instead.
    """


#: The debug ports a person's own Chrome listens on. No test may ever reach one of
#: these, opted out or not: the netkeeper profile behind them is a real LinkedIn
#: session, and during an armed week, a live scheduler's (#293 review).
_PERSONAL_CDP_PORTS = frozenset({9222})


def is_personal_cdp(url: str) -> bool:
    """Whether ``url`` is a port-9222 debug address (any host), has no port, or cannot be read."""
    try:
        port = urlsplit(url if "://" in url else f"http://{url}").port
    except ValueError:
        return True  # unparseable: refuse rather than guess
    # Any host: 127.0.0.1, localhost, ::1, or a LAN address forwarding to it.
    return port is None or port in _PERSONAL_CDP_PORTS


#: A CDP address where nothing listens: port 1 on the loopback refuses at once. Helpers
#: that hand a fake connector to ``AttachBrowserProvider`` use it, so a provider that
#: lost its ``connector=`` fails instead of attaching to a real Chrome. Child processes
#: (``activity_lock_proc.py``) run without ``conftest.py``'s guard, so this is all that
#: stands between them and port 9222 (#294).
UNREACHABLE_CDP_URL = "http://127.0.0.1:1"


def isolated_cdp(url: str) -> str:
    """``url``, for a smoke test's own raw ``connect_over_cdp``, or
    :class:`RealBrowserBlocked` when it is a personal Chrome's address (#497 review).

    A raw connect skips ``PlaywrightCdpConnector`` and so ``conftest.py``'s guard: each
    one calls this first, before Playwright starts."""
    if is_personal_cdp(url):
        raise RealBrowserBlocked(
            f"a smoke test tried a raw CDP connect to {url}, a personal Chrome's debug"
            " port or no port. Set NETKEEPER_CDP_URL to an isolated Chrome on another port"
        )
    return url
