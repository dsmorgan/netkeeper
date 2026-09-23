"""Attach to the Chrome you already run, and never to any other browser (spec 9.1, ADR 0002).

netkeeper has one browser mode. It connects over the Chrome DevTools Protocol to a
Chrome the user started with a dedicated profile and a debug port, reuses the
context that is already there, opens one tab for the run, and closes that tab when
the run ends. It never launches a browser, never creates a second context, never
writes cookies, and never overrides the user agent or the timezone: LinkedIn has to
see one device with one fingerprint, and the user's own browsing is the cover
traffic. ADR 0002 has the incident that decided this.

The :class:`BrowserProvider` seam stays so a future ADR can add a mode without every
caller learning about modes, and ``attach`` is its only member. There is nothing to
fall back to: when Chrome is unreachable the run raises :class:`BrowserUnavailable`
and the scheduler tries again later.

Nothing here imports the ORM or opens a session (spec 9.10, ADR 0005).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.activity_lock import SINGLE_ACCOUNT_KEY as SINGLE_ACCOUNT_KEY

log = logging.getLogger(__name__)

#: The only browser mode there is (ADR 0002).
ATTACH = "attach"

#: Directory under the data directory that the user points Chrome's ``--user-data-dir``
#: at. Chrome 136 and later refuse a debug port on the default profile directory, so
#: the sidecar's Chrome has its own (spec 9.1). netkeeper never creates or writes it:
#: Chrome does, when the user runs the command `netkeeper browser launch` prints.
CHROME_PROFILE_DIRNAME = "chrome-profile"


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


class PageLike(Protocol):
    """The slice of a Playwright ``Page`` this package uses.

    A narrow protocol instead of the imported class keeps Playwright out of every
    signature and lets the offline tests drive the same code with a fake tab.
    """

    @property
    def url(self) -> str: ...

    def is_closed(self) -> bool: ...

    async def goto(self, url: str) -> object: ...

    async def evaluate(self, expression: str) -> Any: ...

    async def close(self) -> None: ...


class ContextLike(Protocol):
    """The slice of a Playwright ``BrowserContext`` this package uses.

    Deliberately tiny: ``add_init_script``, ``route``, ``add_cookies`` and the other
    context mutators are absent so that reaching for one is a type error as well as
    a review failure (spec 9.1).
    """

    #: The tabs open in this context, ours and the user's. Read-only: netkeeper
    #: counts them and closes its own, never anyone else's.
    @property
    def pages(self) -> Sequence[PageLike]: ...

    async def new_page(self) -> PageLike: ...

    async def cookies(self) -> Sequence[Mapping[str, Any]]: ...


class BrowserLike(Protocol):
    """The slice of a Playwright ``Browser`` this package uses. Note the absent ``new_context``."""

    @property
    def contexts(self) -> Sequence[ContextLike]: ...

    @property
    def version(self) -> str: ...

    def is_connected(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Connection:
    """A live CDP connection to the user's browser, and the call that lets go of it."""

    browser: BrowserLike
    detach: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Attachment:
    """A connection plus the context we reuse: ``browser.contexts[0]``, never a new one."""

    browser: BrowserLike
    context: ContextLike
    detach: Callable[[], Awaitable[None]]


class CdpConnector(Protocol):
    """How :class:`AttachBrowserProvider` reaches a browser. The seam the tests replace."""

    async def connect(self, cdp_url: str) -> Connection: ...


class PlaywrightCdpConnector:
    """The production connector: Playwright's ``connect_over_cdp``, and nothing else.

    This is the only place in netkeeper that opens a browser connection, and the only
    Playwright entry point it names is the attaching one. ``chromium.launch`` and
    ``launch_persistent_context`` are not used here or anywhere else, and a test
    walks the package's syntax tree to keep it that way.
    """

    async def connect(self, cdp_url: str) -> Connection:
        # Imported here rather than at module scope: Playwright costs a few hundred
        # milliseconds to import, and `netkeeper --help` should not pay for it.
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
        except BaseException as exc:
            await playwright.stop()
            raise BrowserUnavailable(
                f"cannot attach to Chrome at {cdp_url}: {_reason(exc)}; "
                "start it with the command `netkeeper browser launch` prints"
            ) from exc

        async def detach() -> None:
            # Stopping the driver closes our CDP socket and nothing else. We never
            # call browser.close() or context.close(): that browser is the user's,
            # and netkeeper neither starts nor stops it (spec 9.1).
            await playwright.stop()

        return Connection(browser=browser, detach=detach)


class ActivityLocks:
    """One activity lock per LinkedIn account, across every netkeeper process (spec 9.9).

    Every browser-touching path goes through the lock for its account: scheduled jobs,
    ``netkeeper preflight``, ``posture --probe``, ``rehearse``, and the Settings page's
    "check session" button alike. The key is the ``linkedin_account`` the work belongs
    to, so two accounts on one machine do not block each other while two runs on one
    account always do.

    A hold takes two locks, in this order:

    1. **This process's** ``asyncio.Lock`` for the account. It is what lets a run with
       ``wait=True`` queue behind another coroutine in the same process, in order and
       without polling, and it keeps a second coroutine off the file lock entirely.
    2. **The account's OS file lock** (:mod:`netkeeper.linkedin.activity_lock`), shared
       by every process using this data directory. This is the one that keeps
       ``netkeeper preflight`` in a terminal from attaching while ``netkeeper serve``
       holds the browser. The kernel drops it when its holder exits, ``SIGKILL``
       included, so a crashed holder never parks it.

    ``directory`` is where the lock files live; ``None`` means ``<data dir>/locks``,
    resolved at each hold. Hold one registry per process, on the provider; a second
    registry is still gated by the file lock, but its coroutines would not queue.
    """

    #: How often a ``wait=True`` hold looks at a lock another process holds.
    POLL_S = 0.25

    #: A claim can lose to a peek (:func:`activity_lock.inspect`) holding a shared
    #: lock for a few microseconds; a refused claim looks again this much later
    #: before calling the account busy.
    CONFIRM_S = 0.05

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def directory(self) -> Path:
        """Where the lock files live."""
        return activity_lock.locks_dir() if self._directory is None else self._directory

    def lock_for(self, account: str) -> asyncio.Lock:
        """The account's in-process lock, created on first use."""
        lock = self._locks.get(account)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[account] = lock
        return lock

    def is_busy(self, account: str) -> bool:
        """Whether any run, in this process or another, holds this account's lock."""
        lock = self._locks.get(account)
        if lock is not None and lock.locked():
            return True
        return activity_lock.inspect(account, self.directory).held

    @asynccontextmanager
    async def hold(self, account: str, *, wait: bool = False) -> AsyncIterator[None]:
        """Hold the account's lock for the block. Busy raises :class:`BrowserBusy`.

        ``wait=True`` queues behind the current holder instead, for a job that has
        nothing better to do: behind a coroutine in this process on the
        ``asyncio.Lock``, behind another process by looking again every
        :attr:`POLL_S`. There is no race between the in-process check and the
        acquire: acquiring a free ``asyncio.Lock`` does not yield to the loop.
        """
        lock = self.lock_for(account)
        if not wait and lock.locked():
            raise BrowserBusy(
                f"another run in this process (pid {os.getpid()}) already holds the"
                f" browser for LinkedIn account {account!r}; {_BUSY_ADVICE}"
            )
        await lock.acquire()
        try:
            claim = await self._claim(account, wait=wait)
            try:
                yield
            finally:
                claim.release()
        finally:
            lock.release()

    async def _claim(self, account: str, *, wait: bool) -> activity_lock.Claim:
        """The account's file lock, or :class:`BrowserBusy` when another process has it."""
        claim = activity_lock.try_claim(account, self.directory)
        if claim is None:
            await asyncio.sleep(self.CONFIRM_S)
            claim = activity_lock.try_claim(account, self.directory)
        while claim is None and wait:
            await asyncio.sleep(self.POLL_S)
            claim = activity_lock.try_claim(account, self.directory)
        if claim is None:
            holder = activity_lock.read_holder(account, self.directory)
            who = holder.describe() if holder is not None else "another netkeeper process"
            raise BrowserBusy(
                f"the browser for LinkedIn account {account!r} is in use by {who}; {_BUSY_ADVICE}"
            )
        return claim


#: The second half of every busy message: what happened, and what to do about it.
_BUSY_ADVICE = (
    "netkeeper keeps one browser client per account, so this did not attach."
    " Wait for that run to finish, or stop that process, and try again"
)


class BrowserRun:
    """One unit of browser work: the activity lock, one tab, and at most one reattach.

    Only :meth:`AttachBrowserProvider.run` builds one, and it takes the lock before
    the run exists, so there is no way to hold a tab without holding the lock.

    The tab is this run's alone. It is opened lazily, reopened if the user closes it,
    and closed at the end of the run; the context and the browser are left exactly as
    they were found.
    """

    def __init__(
        self, provider: AttachBrowserProvider, account: str, attachment: Attachment
    ) -> None:
        self.account = account
        self._provider = provider
        self._attachment = attachment
        self._page: PageLike | None = None
        self._last_url: str | None = None
        self._reattached = False
        self._closed = False

    @property
    def browser(self) -> BrowserLike:
        """The attached browser. Read-only as far as this package is concerned."""
        return self._attachment.browser

    @property
    def context(self) -> ContextLike:
        """The context this run reuses: the one the user's browsing already lives in."""
        return self._attachment.context

    @property
    def reattached(self) -> bool:
        """Whether this run has already spent its one reattach."""
        return self._reattached

    @property
    def last_url(self) -> str | None:
        """The last URL this run navigated to, restored after the tab is lost."""
        return self._last_url

    async def ensure_page(self) -> PageLike:
        """This run's tab, reopened at the last URL if it went away (spec 9.9).

        Call it before every navigation and before every in-page fetch. A tab the
        user closed is reopened in the same context and taken back to where the run
        was, because an in-page fetch needs the tab it was reading from. A context
        that has gone away costs the run its one reattach; a second loss raises
        :class:`BrowserUnavailable` and the run is over.
        """
        return await self._ensure_page(restore=True)

    async def goto(self, url: str) -> PageLike:
        """Navigate this run's tab, reopening it first, or again, if it was lost.

        Recovery skips restoring the previous URL: this call is about to navigate
        anyway, and a restore would spend a page view (and, on a profile, a budgeted
        visit) on a page nobody asked for twice.

        A browser that dies *during* the navigation is the same loss arriving a
        moment later, so it is handled the same way: reopen the tab, spending the
        run's one reattach if the context went with it, and navigate once more. A
        navigation that fails while the tab and the browser are both still there is
        not a loss — that is the site answering, and it belongs to the caller and to
        the response classification in spec 9.7, so it is raised unchanged.
        """
        page = await self._ensure_page(restore=False)
        try:
            await page.goto(url)
        except Exception as exc:
            if not self._lost(page):
                raise
            log.warning("lost the tab while navigating: %s", _reason(exc))
            page = await self._reopen_for(url)
        self._last_url = url
        return page

    async def _reopen_for(self, url: str) -> PageLike:
        """Open a tab again after a navigation lost one, and navigate it once."""
        self._page = None
        page = await self._ensure_page(restore=False)
        try:
            await page.goto(url)
        except Exception as exc:
            if not self._lost(page):
                raise
            raise BrowserUnavailable(
                "the browser went away again while navigating; aborting the run"
            ) from exc
        log.info("the run carried on after reopening its tab")
        return page

    def _lost(self, page: PageLike) -> bool:
        """Whether a failure means the tab or the browser went away, not the page."""
        return page.is_closed() or not self._attachment.browser.is_connected()

    async def close(self) -> None:
        """Close this run's tab and let go of the connection. Idempotent.

        Only the tab this run opened is closed. The context, the other tabs, and the
        browser process belong to the user.
        """
        if self._closed:
            return
        self._closed = True
        page, self._page = self._page, None
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except Exception as exc:
                log.debug("could not close the run's tab: %s", exc)
        await _detach_quietly(self._attachment.detach)

    async def _ensure_page(self, *, restore: bool) -> PageLike:
        """The recovery routine behind :meth:`ensure_page` (spec 9.9's ``_ensure_page``)."""
        if self._closed:
            raise BrowserUnavailable("this run is over; its tab and connection are closed")
        page = self._page
        if page is not None and not page.is_closed():
            return page
        if page is not None:
            log.warning("the run's tab was closed; reopening it in the same context")
        try:
            page = await self._attachment.context.new_page()
        except Exception as exc:
            page = await self._reopen_after_reattach(exc)
        self._page = page
        if restore and self._last_url is not None:
            log.info("restoring the reopened tab to where the run was")
            await page.goto(self._last_url)
        return page

    async def _reopen_after_reattach(self, cause: Exception) -> PageLike:
        """Reattach once and open the tab again, or give up on the run."""
        if self._reattached:
            raise BrowserUnavailable(
                "the attached browser went away twice in one run; aborting the run"
            ) from cause
        self._reattached = True
        log.warning("lost the browser (%s); reattaching once", cause)
        await _detach_quietly(self._attachment.detach)
        self._attachment = await self._provider.attach()
        try:
            return await self._attachment.context.new_page()
        except Exception as exc:
            raise BrowserUnavailable(
                "reattached to Chrome but still cannot open a tab; aborting the run"
            ) from exc


@runtime_checkable
class BrowserProvider(Protocol):
    """How the extractor gets a browser. ``attach`` is the only way there is.

    ADR 0002 keeps this interface so a mode can be added by a later ADR. Any
    implementation that grows a ``launch`` creates a second LinkedIn device, which is
    the restriction trigger the whole design exists to avoid.
    """

    mode: str
    cdp_url: str

    async def attach(self) -> Attachment: ...

    def run(
        self, account: str = SINGLE_ACCOUNT_KEY, *, wait: bool = False
    ) -> AbstractAsyncContextManager[BrowserRun]: ...


class AttachBrowserProvider:
    """The only provider: attach over CDP, reuse ``contexts[0]``, one tab per run.

    ``cdp_url`` comes from ``linkedin.cdp_url`` in the config and points at the debug
    port of the Chrome the user started (``http://127.0.0.1:9222`` by default).
    """

    mode = ATTACH

    def __init__(
        self,
        cdp_url: str,
        *,
        connector: CdpConnector | None = None,
        locks: ActivityLocks | None = None,
    ) -> None:
        self.cdp_url = cdp_url
        self._connector = PlaywrightCdpConnector() if connector is None else connector
        self.locks = ActivityLocks() if locks is None else locks

    async def attach(self) -> Attachment:
        """Connect to the user's Chrome and reuse the context that is already open.

        Raises :class:`BrowserUnavailable` when Chrome is unreachable or has no
        context to reuse. It never answers the failure by starting a browser.
        """
        try:
            connection = await self._connector.connect(self.cdp_url)
        except BrowserUnavailable:
            raise
        except Exception as exc:
            raise BrowserUnavailable(
                f"cannot attach to Chrome at {self.cdp_url}: {_reason(exc)}; "
                "start it with the command `netkeeper browser launch` prints"
            ) from exc
        contexts = connection.browser.contexts
        if not contexts:
            await _detach_quietly(connection.detach)
            raise BrowserUnavailable(
                f"Chrome at {self.cdp_url} has no open browser context to reuse; "
                "open a window in the netkeeper profile and try again"
            )
        if len(contexts) > 1:
            # One profile, one context, normally. More than one means another tool is
            # driving this Chrome; we still take the first and touch nothing else.
            log.warning("the attached Chrome has %d contexts; reusing the first", len(contexts))
        log.debug("attached to Chrome %s at %s", connection.browser.version, self.cdp_url)
        return Attachment(browser=connection.browser, context=contexts[0], detach=connection.detach)

    @asynccontextmanager
    async def run(
        self, account: str = SINGLE_ACCOUNT_KEY, *, wait: bool = False
    ) -> AsyncIterator[BrowserRun]:
        """Hold the account's activity lock, attach, and yield the run's tab handle.

        The lock -- this process's and the cross-process file lock -- is taken before
        the connection is opened, so a busy account never gets as far as a second CDP
        client, whichever netkeeper process holds it. The tab closes and the connection detaches
        when the block ends, whether or not the body raised.
        """
        async with self.locks.hold(account, wait=wait):
            attachment = await self.attach()
            run = BrowserRun(self, account, attachment)
            try:
                yield run
            finally:
                await run.close()


def _reason(exc: BaseException) -> str:
    """The first line of an exception's message, for a log line or a report row.

    Playwright's errors carry a multi-line call log; the first line says what went
    wrong and the rest belongs in the traceback the ``from exc`` chain keeps.
    """
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


async def _detach_quietly(detach: Callable[[], Awaitable[None]]) -> None:
    """Let go of a connection, logging rather than raising when it is already gone."""
    try:
        await detach()
    except Exception as exc:
        log.debug("detaching from Chrome failed: %s", exc)
