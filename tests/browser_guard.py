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
