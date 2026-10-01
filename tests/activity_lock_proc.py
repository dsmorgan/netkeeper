"""A second netkeeper process, for ``test_activity_lock_processes.py`` to start.

Each mode builds its own :class:`AttachBrowserProvider` with its own fresh
:class:`ActivityLocks` and a :class:`FakeConnector`, exactly as ``netkeeper
preflight`` or ``netkeeper serve`` builds its own provider, and prints one JSON line
saying what happened and how many times its connector attached. Nothing here reaches
a real browser: the connector is the offline fake.

Modes:

``hold ACCOUNT [--as LABEL]``
    Enter ``provider.run(ACCOUNT)``, print ``{"state": "held", ...}``, then block
    until stdin closes. The test either closes stdin (a clean release) or sends
    ``SIGKILL`` (a crash, with no cleanup of any kind).
``try ACCOUNT [--wait]``
    Enter and leave ``provider.run(ACCOUNT)``; print ``attached`` or ``busy``.
``preflight ACCOUNT``
    Run :func:`netkeeper.linkedin.preflight.preflight` and print its verdict.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from browser_guard import UNREACHABLE_CDP_URL

from netkeeper.linkedin.browser import (
    ActivityLocks,
    AttachBrowserProvider,
    BrowserBusy,
)
from netkeeper.linkedin.preflight import preflight

CDP_URL = UNREACHABLE_CDP_URL  # never a real Chrome, even without the guard (#294)


def _emit(**fields: object) -> None:
    print(json.dumps({"pid": os.getpid(), **fields}), flush=True)


def _provider(connector: FakeConnector) -> AttachBrowserProvider:
    return AttachBrowserProvider(CDP_URL, connector=connector, locks=ActivityLocks())


async def _hold(account: str) -> None:
    connector = FakeConnector()
    async with _provider(connector).run(account):
        _emit(state="held", attaches=connector.attaches)
        await asyncio.to_thread(sys.stdin.read)
    _emit(state="released", attaches=connector.attaches)


async def _try(account: str, *, wait: bool) -> None:
    connector = FakeConnector()
    try:
        async with _provider(connector).run(account, wait=wait):
            pass
    except BrowserBusy as exc:
        _emit(outcome="busy", message=str(exc), attaches=connector.attaches)
        return
    _emit(outcome="attached", attaches=connector.attaches)


async def _preflight(account: str) -> None:
    context = FakeContext(evaluate_result={"userAgent": "Chrome/140.0.7339.80"})
    connector = FakeConnector([FakeBrowser([context])])
    report = await preflight(_provider(connector), account)
    _emit(
        attached=report.attached,
        ok=report.ok,
        problems=list(report.problems),
        attaches=connector.attaches,
    )


def main(argv: list[str]) -> None:
    mode, account, *rest = argv
    if mode == "hold":
        if "--as" in rest:
            # What the busy message will call this process: `netkeeper serve`, say.
            sys.argv = rest[rest.index("--as") + 1].split()
        asyncio.run(_hold(account))
    elif mode == "try":
        asyncio.run(_try(account, wait="--wait" in rest))
    elif mode == "preflight":
        asyncio.run(_preflight(account))
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
