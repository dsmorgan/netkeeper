"""The Chrome launch command, as data, for ``GET /linkedin/browser`` (P2-12).

``netkeeper browser launch`` (``netkeeper/cli.py``) prints this same command from a
terminal -- literally: it calls :func:`chrome_launch_command` and :func:`cdp_port`
too, rather than keeping its own copy, so there is exactly one place that knows how
to build the command and the two can never drift apart. This module exists
separately so the LinkedIn page can show the same thing, without the route that
serves it importing anything that touches a browser: ``netkeeper.linkedin.browser``
is one of ``BROWSER_MODULES`` in ``tests/test_browser_safety.py``, so nothing under
``netkeeper/web/`` may import it, even for a constant, and ``netkeeper/cli.py`` is
one of the few modules the safety rules do allow to. Everything here is a pure
string computation from config -- no CDP, no Playwright, nothing awaited -- so a
request handler may call it directly (CLAUDE.md: "never `await` browser work inside
a request handler").

``CHROME_PROFILE_DIRNAME`` is the one thing still duplicated from
``netkeeper.linkedin.browser`` rather than imported, for the same reason an import
of the whole module is forbidden; ``tests/test_browser_launch.py`` pins the two
constants together so they cannot silently drift apart, and separately asserts that
``netkeeper browser launch``'s own printed lines are exactly this module's output,
so an import forgetting to happen would fail loudly rather than pass by coincidence.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

#: Mirrors ``netkeeper.linkedin.browser.CHROME_PROFILE_DIRNAME``. See the module
#: docstring for why this is a copy and not an import.
CHROME_PROFILE_DIRNAME = "chrome-profile"

#: Chrome's usual remote-debugging port, used when ``cdp_url`` names none.
DEFAULT_CDP_PORT = 9222


def cdp_port(cdp_url: str) -> int:
    """The debug port ``cdp_url`` names, falling back to Chrome's usual one."""
    try:
        port = urlsplit(cdp_url).port
    except ValueError:
        port = None
    return DEFAULT_CDP_PORT if port is None else port


def chrome_launch_command(port: int, profile: Path) -> list[str]:
    """The platform's Chrome command, as lines a person can paste (spec 9.1).

    ``sys.platform`` is read on whichever machine runs this -- the backend and
    the Chrome it attaches to are the same machine today (spec 5, "Multi-user
    readiness"), so that is also the machine the person would paste this into.
    """
    platform: str = sys.platform
    opener = 'open -na "Google Chrome" --args \\' if platform == "darwin" else "google-chrome \\"
    return [opener, f"  --remote-debugging-port={port} \\", f'  --user-data-dir="{profile}"']


def remote_host_note(cdp_url: str) -> str | None:
    """Why the command above will not work here, when ``cdp_url`` points elsewhere.

    Chrome's debug port only answers on its own loopback address, so a
    ``cdp_url`` naming another host can never be reached this way -- the same
    check ``netkeeper browser launch`` prints as a warning.
    """
    host = urlsplit(cdp_url).hostname
    if host in (None, "localhost", "127.0.0.1", "::1"):
        return None
    return (
        f"linkedin.cdp_url points at {host}, not this machine. Chrome's debug port is only"
        " reachable on its own loopback address."
    )
