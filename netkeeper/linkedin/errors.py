"""The ways a browser path gives up, apart from the browser itself (#177).

:mod:`netkeeper.linkedin.browser` raises these and re-exports them; import them
from there in browser code. They live here so code that may not import the
browser module (``services/runs.py``, which request handlers import, spec 9.9)
can still tell a lost browser from any other failure when it records how a run
ended. Nothing here imports anything.
"""

from __future__ import annotations


class BrowserError(RuntimeError):
    """Base class for the ways a browser path gives up."""


class BrowserUnavailable(BrowserError):
    """Chrome is not reachable, or it went away mid-run and one reattach did not fix it.

    The run aborts. Nothing retries it here: the scheduler parks a retry 20 to 50
    minutes out (spec 9.9), and no code path may answer this by starting a browser.
    """


class BrowserBusy(BrowserError):
    """Another run, in this process or another netkeeper process, holds the account's lock.

    Two CDP clients on one browser drop each other's connection, so the caller waits
    or reports ``busy`` (spec 9.9); it never opens a second browser to get around it.
    The message names the holder (command, pid, since when) when the holder left a note.
    """
