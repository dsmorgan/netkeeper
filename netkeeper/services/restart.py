"""Restarting ``netkeeper serve`` from the Settings page (#597).

A few settings are read once, when ``serve`` starts (``linkedin.cdp_url``, and
anything config.toml sets). **Restart netkeeper** applies them without a terminal:

1. ``POST /settings/restart`` answers, then, once nothing is running
   (:mod:`netkeeper.services.restart_gate`), calls :meth:`Restarter.trigger`, which
   asks uvicorn to stop the way Ctrl-C does. The event streams end, the lifespan's
   teardown stops the scheduler, the campaign engine and the mailbox poll, and
   releases the serve lock (#590) before :func:`netkeeper.cli.serve` gets control
   back.
2. ``serve`` then calls :func:`relaunch`. Under the login item (launchd runs it with
   ``XPC_SERVICE_NAME`` set to its label) it exits with :data:`LAUNCHD_RESTART_EXIT`:
   the agent's ``KeepAlive`` restarts a job that did not exit successfully, after its
   ``ThrottleInterval``, through ``serve-launchd.sh`` and its ``db upgrade``. Anywhere
   else it re-execs itself with the same interpreter and arguments, so the process
   keeps its pid, its terminal and its environment (``NETKEEPER_DATA`` included).
3. The new process claims the serve lock again as it starts. Nothing holds it in
   between, so a ``netkeeper restore`` started in that moment wins, and the new
   ``serve`` refuses to start and says so, as it would from a terminal.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, NoReturn

log = logging.getLogger(__name__)

LAUNCHD_LABEL: Final = "fun.tnkr.netkeeper"
"""The login item's label (``scripts/install-launchd.sh``). launchd puts it in the
job's ``XPC_SERVICE_NAME``."""

LAUNCHD_RESTART_EXIT: Final = 75
"""``EX_TEMPFAIL``. Any non-zero status makes launchd start the agent again; zero would
not (``KeepAlive`` ``SuccessfulExit`` false)."""

Mode = Literal["launchd", "exec"]


def mode(environ: Mapping[str, str] = os.environ) -> Mode:
    """``launchd`` when the login item started this process, else ``exec``."""
    return "launchd" if environ.get("XPC_SERVICE_NAME") == LAUNCHD_LABEL else "exec"


@dataclass(slots=True)
class Restarter:
    """What ``serve`` puts on ``app.state.restarter``: ``stop`` asks the server to stop."""

    stop: Callable[[], None]
    mode: Mode
    requested: bool = False

    def trigger(self) -> None:
        """Stop the server so ``serve`` relaunches it. Idempotent."""
        if self.requested:
            return
        self.requested = True
        log.info("restart requested from the Settings page; stopping netkeeper serve")
        self.stop()


def relaunch(
    how: Mode,
    *,
    argv: Sequence[str] | None = None,
    executable: str | None = None,
    execv: Callable[[str, list[str]], object] | None = None,
    exit: Callable[[int], object] = sys.exit,
) -> NoReturn:
    """Start ``serve`` again after it stopped: exit for launchd, or re-exec in place.

    ``argv`` is the interpreter's own command line (``sys.orig_argv``) and
    ``executable`` the interpreter; tests pass fakes for those and for ``execv`` and
    ``exit``.
    """
    if how == "launchd":
        log.info(
            "exiting with status %d so launchd starts netkeeper serve again", LAUNCHD_RESTART_EXIT
        )
        exit(LAUNCHD_RESTART_EXIT)
    else:
        python = sys.executable if executable is None else executable
        args = list(sys.orig_argv if argv is None else argv)
        command = [python, *args[1:]]
        log.info("starting netkeeper serve again in place: %s", " ".join(command[1:]))
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        # The one exec in the package (tests/test_browser_safety.py pins it): this
        # process replaces itself with serve; it never starts another program.
        replace_process = os.execv if execv is None else execv
        replace_process(python, command)
    raise RuntimeError("relaunch returned")  # only a fake execv or exit gets here
