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
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable
from urllib.parse import unquote, urljoin, urlsplit

from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.activity_lock import LEGACY_SHARED_KEY
from netkeeper.linkedin.activity_lock import SINGLE_ACCOUNT_KEY as SINGLE_ACCOUNT_KEY
from netkeeper.linkedin.body_tap import BodyTap
from netkeeper.linkedin.observe import (
    ListenablePage,
    Observation,
    ObservationLimits,
    ResponseMatch,
)
from netkeeper.linkedin.pacing import ScrollPlan, rest_pointer_like_a_person

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


class _MouseLike(Protocol):
    """The slice of a Playwright ``Mouse`` a :class:`~netkeeper.linkedin.pacing.ScrollPlan`
    is replayed through, and the pointer rested over content before one (#192).

    Not part of :class:`PageLike`: #152 kept that protocol to exactly what every
    other caller needs, and :meth:`BrowserRun.scroll` is the only thing in this
    package that reaches for a page's mouse. Declaring the wider slice here, local to
    the one method that uses it, is the point -- widening the shared protocol would
    hand every other caller the whole Playwright mouse API for a replay that has
    exactly one shape. ``move`` is not a click or a hover on any element -- it only
    ever targets a bare point, never a locator -- so it needs nothing from the wider
    :data:`PAGE_DRIVERS` refusal in ``tests/test_browser_safety.py``, which pins that
    reading, scoped to the one method that may call it (#192).
    """

    async def wheel(self, delta_x: float, delta_y: float) -> None: ...

    async def move(self, x: float, y: float) -> None: ...


class _LocatorLike(Protocol):
    """The slice of a Playwright ``Locator`` :meth:`BrowserRun._rest_pointer_over_content`
    reads a box from (#192).

    Playwright resolves the locator in its own isolated utility world and reads
    the box over CDP -- never by running script in the page's own execution
    context the way ``evaluate`` does -- so page script can't see or answer it,
    which is why ADR 0006's amendment treats it as a read rather than an input:
    nothing about it resembles the ``fetch`` a page's own bot-detection telemetry
    watches for. ``first`` narrows a locator that could otherwise match more than
    one element, the same way ``.first`` does on a real Playwright ``Locator``.
    """

    @property
    def first(self) -> _LocatorLike: ...

    async def bounding_box(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- mirrors Playwright's own signature
    ) -> Mapping[str, float] | None: ...


class _ScrollablePage(PageLike, Protocol):
    """A tab that can also be scrolled. See :class:`_MouseLike` and :class:`_LocatorLike`."""

    @property
    def mouse(self) -> _MouseLike: ...

    @property
    def viewport_size(self) -> Mapping[str, int] | None:
        """This tab's viewport, when Playwright knows it -- a passive, already-cached
        read, not an ``evaluate`` call. Attach mode never calls ``set_viewport_size``
        (spec 9.1: never mutate the user's context), so this is commonly ``None`` for
        every tab a real run opens; :func:`_viewport_size` is where that is handled.
        """
        ...

    def locator(self, selector: str) -> _LocatorLike: ...


class _CdpSessionLike(Protocol):
    """The slice of a Playwright ``CDPSession`` the body tap uses, and nothing more (#200).

    ``send`` is called in exactly one method, with two read-only methods named as
    literals (``tests/test_browser_safety.py``); ``on`` only listens.
    """

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any: ...

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

    async def detach(self) -> None: ...


class _TapContext(Protocol):
    """``new_cdp_session``, borrowed by :meth:`BrowserRun._open_body_tap` alone (#200).

    :class:`ContextLike` leaves every context mutator out so that reaching for one is
    a type error; this protocol hands the one method the body tap needs to that one
    method, the way :class:`_ObservablePage` borrows the listener methods.
    """

    async def new_cdp_session(self, page: Any) -> Any: ...


#: The Network buffers the body tap's own session asks Chrome for (#200): one answer
#: up to the observation's body limit, and a few of them at once. Chrome needs a
#: buffer to stream from; it is the tap's session's own, not the one Playwright reads.
TAP_RESOURCE_BUFFER_BYTES: Final = 8 * 1024 * 1024
TAP_TOTAL_BUFFER_BYTES: Final = 32 * 1024 * 1024


class _ObservablePage(PageLike, ListenablePage, Protocol):
    """A tab that can also be listened to, for :meth:`BrowserRun.observe` alone.

    Same reasoning as :class:`_ScrollablePage`: the listener methods are borrowed
    here, by the one method that needs them, instead of widening :class:`PageLike`
    for every caller. ``on`` and ``remove_listener`` only listen; nothing in either
    protocol can hold, change, or answer a request (ADR 0006).
    """


#: The landmark :meth:`BrowserRun._rest_pointer_over_content` reads a box from
#: first (#192). A modern page's primary content -- flagship-web included -- is
#: conventionally wrapped in a ``<main>`` element; :func:`_content_box` is where
#: the read (and its fallback when there is no box) happens.
CONTENT_LANDMARK_SELECTOR: Final = "main"

#: How long to wait for the landmark's box before giving up on it. Short on
#: purpose: this is a best-effort read, not something worth stalling a run's
#: pacing over.
CONTENT_BOX_TIMEOUT_MS: Final = 1000.0

#: A conservative fallback viewport for :meth:`BrowserRun.scroll`'s pointer-rest step
#: (#192), used only when neither a content box nor the tab's own ``viewport_size``
#: is available. Sized like an ordinary laptop browser window -- but this is the
#: last resort, not the primary source of truth: the first cut of this fix aimed at
#: a fraction of *this* constant regardless of the tab's real window size, and
#: missed at 560px wide, at a 293px-tall viewport, and on a centered column on a
#: 2200px ultrawide (#192 review, F1). :data:`CONTENT_LANDMARK_SELECTOR`'s box is
#: read first and is what the pointer actually targets whenever the page has one.
DEFAULT_VIEWPORT_WIDTH: Final = 1280
DEFAULT_VIEWPORT_HEIGHT: Final = 800

#: Where the pointer comes to rest when there is no content box to read: centered
#: in the guessed viewport, but never above :data:`REST_MIN_Y_PX`.
REST_Y_FRACTION: Final = 0.5

#: No rest point, or any waypoint on the way to one, may land above this many
#: pixels down from the top of the page -- an absolute floor, not a fraction of a
#: guessed viewport height (#192 review, F1/F5: a fraction of the *wrong* guess is
#: no floor at all, and the review's mutation replacing one with 0 went
#: undetected). Real fixed headers on this kind of app run well under 100px tall
#: (LinkedIn's connections page is about 52px); this clears any of them with room
#: to spare. It applies whether the target came from a content box or the
#: viewport fallback -- a box's coordinates are already real page pixels, in the
#: same space this floor is measured in.
REST_MIN_Y_PX: Final = 96

#: How far below the top of a content box's *visible* part the rest point may aim
#: (#192 review round 2, N1). A box read from an in-flow ``<main>`` whose ancestor
#: -- not ``<main>`` itself -- is the scrolling element reports its own full
#: content height here, which is the whole list and grows as more pages load, not
#: the sliver of it the viewport actually shows. Centering on that box, or letting
#: jitter roam across it, aims the pointer far below the real window -- reproduced
#: with a real ~2300px ``<main>``, and exactly the #31 symptom again. Only a box's
#: own top edge is anywhere near the visible viewport when this runs (right after
#: landing, before anything has scrolled), so the target -- and every waypoint on
#: the way to it -- stays within this many pixels of that top, never more than
#: halfway into a short box either.
REST_VISIBLE_SPAN_PX: Final = 250


class _ControlLike(Protocol):
    """The slice of a Playwright ``Locator`` :meth:`BrowserRun.click_contact_info` uses.

    Local to that one method, like :class:`_MouseLike` is to :meth:`BrowserRun.scroll`:
    counting the matches, reading one attribute, and the one click ADR 0006 allows.
    Nothing here types, hovers, presses a key, or runs script.
    """

    async def count(self) -> int: ...

    # Playwright's own signatures, timeout in milliseconds included.
    async def get_attribute(
        self,
        name: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> str | None: ...

    async def click(
        self,
        *,
        delay: float | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None: ...


class _ClickablePage(PageLike, Protocol):
    """A tab whose controls can be found by accessible role and name. See :class:`_ControlLike`."""

    def get_by_role(self, role: str, *, name: str, exact: bool) -> _ControlLike: ...


#: The control ADR 0006 allows one click on, by accessible role and name (#190).
CONTACT_INFO_ROLE = "link"
CONTACT_INFO_NAME = "Contact info"
#: What the Contact info link's href adds to the profile's own path.
CONTACT_INFO_HREF_SUFFIX = "overlay/contact-info/"
#: How long Playwright may wait for the control to be clickable, in milliseconds. It
#: clicks once when it is; a control that never becomes clickable is not clicked.
CONTACT_INFO_CLICK_TIMEOUT_MS = 10_000.0
#: Time between the press and the release, as a person's click takes, in milliseconds.
CONTACT_INFO_PRESS_MS = 90.0


@dataclass(frozen=True, slots=True)
class ContactInfoClick:
    """What :meth:`BrowserRun.click_contact_info` did.

    ``clicked`` is ``True`` only when the one click was sent. Otherwise ``refusal`` is
    a fixed phrase saying why nothing was clicked: never a url, a name, or a selector's
    text.
    """

    page: PageLike
    clicked: bool
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class ScrollOutcome:
    """What :meth:`BrowserRun.scroll` actually did.

    ``page`` is the page it scrolled (reopened first if it had already been lost --
    see the method's docstring). ``cancelled`` is ``True`` when the ``cancelled``
    callback stopped the replay before it sent every wheel event and waited out the
    dwell -- the caller's own signal to stop, echoed back, so it does not have to
    re-poll that callback itself or reconstruct the answer by comparing how many
    wheel events landed against how many the plan had (#168 review, F8).
    """

    page: PageLike
    cancelled: bool


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

    async def cookies(self, urls: str | Sequence[str] | None = None) -> Sequence[Mapping[str, Any]]:
        """Every cookie in this context's jar, or only those visible to ``urls``.

        Mirrors Playwright's own ``BrowserContext.cookies()``: a cookie is
        included when no ``urls`` are given, or when its domain and path
        make it visible to at least one of them (ordinary browser
        cookie-scoping rules, not a netkeeper filter). A caller that knows
        which site's cookies it actually needs should pass ``urls`` (#174
        item 7): the unfiltered form pulls *every* cookie in the context's
        jar -- every site the profile is logged into -- into process memory
        for no reason. ``tests/smoke/test_fetch_smoke.py``'s teardown does
        this, scoped to its own loopback fixture, against the developer's
        real Chrome profile (spec 9.1).

        ``netkeeper.linkedin.preflight``'s own read stays unscoped for now
        (#176 review, L8): its two cookie names (``li_at``, ``JSESSIONID``)
        are already filtered to a ``linkedin.com`` domain suffix in Python,
        so scoping the call itself with ``urls=["https://www.linkedin.com/"]``
        looks safe in principle, but this codebase has no way to verify,
        without reaching the site, that LinkedIn never sets either cookie on
        a narrower host a ``www.linkedin.com`` url would not see -- and a
        false "no session" reading there is a worse failure than the small
        amount of extra jar this one, already narrow, name+domain+expiry-only
        read pulls in. Left alone rather than guessed at.
        """
        ...


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

    def __init__(
        self, directory: Path | None = None, *, legacy_partner: str | None = SINGLE_ACCOUNT_KEY
    ) -> None:
        self._directory = directory
        self._locks: dict[str, asyncio.Lock] = {}
        #: The account whose holds also claim the legacy ``browser-local.lock``
        #: (#169 F): the local user's account, which is what pre-P2-10 code acted
        #: for. Account 1 unless whoever builds the registry knows better; a
        #: caller with a database passes the local account's key (#175 review, F10).
        self.legacy_partner = legacy_partner

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
        if activity_lock.inspect(account, self.directory).held:
            return True
        return (
            account == self.legacy_partner
            and activity_lock.inspect(LEGACY_SHARED_KEY, self.directory).held
        )

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
            claims = await self._claims(account, wait=wait)
            try:
                yield
            finally:
                for claim in reversed(claims):
                    claim.release()
        finally:
            lock.release()

    async def _claims(self, account: str, *, wait: bool) -> list[activity_lock.Claim]:
        """The account's file lock; for :attr:`legacy_partner`, the legacy one first (#169 F).

        See :data:`~netkeeper.linkedin.activity_lock.LEGACY_SHARED_KEY`: a process
        running older code holds only the legacy file, and only ever for the local
        user's account.
        Either one busy releases whatever was taken and raises.
        """
        keys = [LEGACY_SHARED_KEY, account] if account == self.legacy_partner else [account]
        held: list[activity_lock.Claim] = []
        try:
            for key in keys:
                held.append(await self._claim(key, wait=wait, account=account))
        except BaseException:
            for claim in reversed(held):
                claim.release()
            raise
        return held

    async def _claim(self, key: str, *, wait: bool, account: str) -> activity_lock.Claim:
        """The file lock ``key``, or :class:`BrowserBusy` when another process has it."""
        claim = activity_lock.try_claim(key, self.directory)
        if claim is None:
            await asyncio.sleep(self.CONFIRM_S)
            claim = activity_lock.try_claim(key, self.directory)
        while claim is None and wait:
            await asyncio.sleep(self.POLL_S)
            claim = activity_lock.try_claim(key, self.directory)
        if claim is None:
            holder = activity_lock.read_holder(key, self.directory)
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


async def _real_sleep(seconds: float) -> None:
    """The default sleeper for :meth:`BrowserRun.scroll`. A named wrapper so the
    parameter's type stays one-argument (``asyncio.sleep`` also takes an optional
    result to return)."""
    await asyncio.sleep(seconds)


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
        self._observations: list[Observation] = []
        #: Whether :meth:`scroll` has already moved the pointer to rest over this
        #: tab's content (#192). Cleared by :meth:`_ensure_page` whenever the tab
        #: itself is reopened, so a recovered tab gets its pointer rested again
        #: rather than inheriting a stale reading from the one that was lost.
        self._pointer_rested = False

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

    async def scroll(
        self,
        plan: ScrollPlan,
        *,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
        cancelled: Callable[[], bool] | None = None,
        rng: random.Random | None = None,
    ) -> ScrollOutcome:
        """Rest the pointer over the content, then replay ``plan``: one ``mouse.wheel``
        per step, then the dwell.

        #152's decision: :mod:`netkeeper.linkedin.pacing` builds the plan as plain
        data, with no browser in sight, and this is where it is spent, on the one tab
        this run owns. :class:`PageLike` gains nothing from it -- this method borrows
        the wider :class:`_ScrollablePage` slice locally rather than widening the
        protocol every other caller shares.

        Calls :meth:`ensure_page` first, so a tab that was *already* closed (or a
        browser that had already gone away) before this call started is reopened the
        same way a navigation would recover it. That recovery happens once, up
        front: a tab lost *during* the replay itself -- between one wheel event and
        the next -- is not detected or reopened mid-loop, the same as a navigation's
        own failure belongs to the caller once the tab is confirmed present (see
        :meth:`goto`'s docstring for the parallel case). The returned
        :class:`ScrollOutcome` names the page actually scrolled either way -- a
        caller tracking the previous one (a request listener, say) should re-attach
        to it if it differs.

        **The pointer rests over the content first (#192).** Playwright's
        ``mouse.wheel`` fires at the virtual pointer's position, which starts at
        (0, 0) and never moves until something moves it -- and on a page whose
        fixed header sits at (0, 0), a wheel replay that never moved the pointer
        scrolls the header, not the list beneath it, which is exactly the bug: a
        supervised run whose tab never scrolled at all. :meth:`_rest_pointer_over_content`
        moves it there once per open tab (see :meth:`_ensure_page`, which clears the
        flag whenever the tab is reopened) before the first wheel event of the first
        call on that tab; a later call on the same tab does not repeat it.

        ``sleep`` stands in for the wait after each step and the final dwell, and
        the pause after each hop of the pointer-rest walk; inject a fake in an
        offline test so it takes zero real time and records what it was asked to
        wait, or a scaled one to divide every wait for a sped-up demo. Real time
        (``asyncio.sleep``) is the default.

        ``rng`` shapes the pointer-rest walk's jitter and pacing the same way a
        caller's own :class:`random.Random` shapes ``plan``
        (:func:`~netkeeper.linkedin.pacing.scroll_like_a_person`) -- pass the same
        instance for a run that should replay identically from one seed. Defaults to
        a fresh, unseeded one, spent only if this tab's pointer still needs resting.

        ``cancelled``, when given, is polled once before the pointer-rest walk
        begins, then again before every wheel event and again before the final
        dwell, so a caller wired to spec 9.9's cooperative cancel ("checked
        between profiles and inside sliced cooldowns") has somewhere to plug one
        in; nothing here reads a database or a settings flag itself (spec 9.10
        keeps that off this side of the boundary), so the check is the caller's
        to supply. A cancelled replay stops before moving the pointer at all, or
        before sending its remaining wheel events, or before waiting out the
        dwell, and :attr:`ScrollOutcome.cancelled` says so -- the caller does not
        have to re-poll its own ``cancelled`` callback, or compare how many wheel
        events landed against how many the plan had, to find out (#168 review,
        F8). It is not polled *during* the pointer-rest walk once that walk has
        started: letting it finish keeps the pointer from being left mid-hop.
        """
        page = cast(_ScrollablePage, await self.ensure_page())
        if cancelled is not None and cancelled():
            return ScrollOutcome(page=page, cancelled=True)
        rest_rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        await self._rest_pointer_over_content(page, sleep=sleep, rng=rest_rng)
        for step in plan.steps:
            if cancelled is not None and cancelled():
                return ScrollOutcome(page=page, cancelled=True)
            await page.mouse.wheel(0, step.delta_px)
            await sleep(step.pause_s)
        if cancelled is not None and cancelled():
            return ScrollOutcome(page=page, cancelled=True)
        await sleep(plan.dwell_s)
        return ScrollOutcome(page=page, cancelled=False)

    async def _rest_pointer_over_content(
        self,
        page: PageLike,
        *,
        sleep: Callable[[float], Awaitable[None]],
        rng: random.Random,
    ) -> None:
        """Move the pointer to rest over the page's content, once per tab (#192).

        A few short hops with small jitter, paced like a hand coming to rest. This
        is ``mouse.move`` alone -- a bare point, not a click, and not a hover
        resolved against a particular element the way ``locator.hover()`` would
        be -- so it needs no exception to spec 9.1's "scroll is the only
        automation" the way ADR 0006's Contact info click does: resting the
        pointer somewhere over the content before scrolling is already part of
        what spec 9.5's "scroll like a person" means. Where a real hand would
        come to rest is wherever the content actually is -- most likely on a
        card, not blank space -- and if that happens to trip some hover-triggered
        request the *page's own script* sends on its own account, ADR 0006
        already allows it: the seam only ever reads what the page decides to
        send, at its own pace; nothing here sends, routes, or alters a request
        either way.

        **The target, in order of preference (#192 review, F1):**

        1. A point near the top of :data:`CONTENT_LANDMARK_SELECTOR`'s box
           (:func:`_content_box`), when the page has one -- within
           :data:`REST_VISIBLE_SPAN_PX` of it, never past the box's own vertical
           midpoint for a short box. This is real, on-screen geometry Playwright
           already computed from the page's actual layout -- correct at any
           window size, because it was never a guess. Not the box's *center*: a
           box read from an in-flow ``<main>`` whose ancestor does the actual
           scrolling reports the full list's height there, which keeps growing,
           so its center can sit far below the real window (#192 review round 2,
           N1). Jitter stays inside the box, and its upper bound is capped at the
           tab's own known viewport height too, when that is known.
        2. Failing that, the center of the tab's own known ``viewport_size``
           (:func:`_viewport_size`), or :data:`DEFAULT_VIEWPORT_WIDTH` /
           :data:`DEFAULT_VIEWPORT_HEIGHT` when even that is unknown -- true for
           most tabs this run attaches to (attach mode never sets one; spec 9.1
           forbids mutating the context to do so). A last resort: a page with no
           content landmark and an unknown viewport gets a guess, not a failure.

        Either way, the final target and every waypoint on the way to it are held
        at or below :data:`REST_MIN_Y_PX` from the top -- an absolute pixel
        clearance, not a fraction of whichever height estimate was in play, so it
        holds regardless of which one that was (#192 review, F5).
        """
        if self._pointer_rested:
            return
        mouse = cast(_ScrollablePage, page).mouse
        box = await _content_box(page)
        if box is not None:
            box_x, box_y = box["x"], box["y"]
            box_w, box_h = box["width"], box["height"]
            box_top = max(box_y, REST_MIN_Y_PX)
            target_x = box_x + box_w / 2
            target_y = box_top + min(box_h / 2, REST_VISIBLE_SPAN_PX)
            jitter_x = (max(box_x, 0.0), box_x + box_w)
            # A backstop: today's ±40px wobble never comes near it, but a wider one
            # would stop here (pinned by a test, #196 item 5).
            jitter_y_high = box_top + min(box_h, REST_VISIBLE_SPAN_PX * 2)
            known_height = _known_viewport_height(page)
            if known_height is not None:
                jitter_y_high = min(jitter_y_high, known_height)
            jitter_y = (box_top, jitter_y_high)
        else:
            width, height = _viewport_size(page)
            target_x = width / 2
            target_y = height * REST_Y_FRACTION
            jitter_x = (0.0, width)
            jitter_y = (REST_MIN_Y_PX, height)
        target_x = max(target_x, 0.0)
        target_y = max(target_y, REST_MIN_Y_PX)
        plan = rest_pointer_like_a_person(rng)
        for step in plan.steps:
            x = _clamp(target_x + step.dx, *jitter_x)
            y = _clamp(target_y + step.dy, *jitter_y)
            await mouse.move(x, y)
            await sleep(step.pause_s)
        self._pointer_rested = True

    async def observe(
        self,
        match: ResponseMatch,
        *,
        limits: ObservationLimits | None = None,
        tap: bool | ResponseMatch = False,
    ) -> Observation:
        """Start keeping the responses this run's tab receives that ``match`` names (ADR 0006).

        Passive and read-only: the returned :class:`~netkeeper.linkedin.observe.Observation`
        listens to the tab's ``response`` events and keeps the matching bodies, in
        arrival order, within ``limits``. It cannot hold, change, answer, or cancel a
        request; nothing in this package can (``tests/test_browser_safety.py``).

        Start it *before* the navigation or scroll whose responses it should see: a
        listener hears only what arrives after it. It listens to the tab that is open
        now (reopened first if it was lost, like :meth:`ensure_page`); a tab reopened
        *later* is a different tab, not listened to, so a caller compares
        :attr:`~netkeeper.linkedin.observe.Observation.page` with the page a
        :meth:`goto` or :meth:`scroll` returns and stops trusting the observation when
        they differ. :meth:`close` closes every observation still open, before the tab.

        ``tap`` also opens the read-only body tap for this observation (#200,
        :meth:`_open_body_tap`), so an answer whose body Chrome could not keep can
        still come with the copy streamed as it arrived. ``True`` taps every answer
        ``match`` names; a :class:`~netkeeper.linkedin.observe.ResponseMatch` taps only
        the answers it names, and must be a narrowing of ``match`` (the same origin,
        rules among ``match``'s) or this raises ``ValueError`` before anything opens
        (#203: enrichment taps its lazy cards and overlay, not the profile's document).
        Without a tap, or when one cannot start, the observation reads exactly as
        before.
        """
        tapped = _tap_match(match, tap)
        page = cast(_ObservablePage, await self.ensure_page())
        body_tap = None
        if tapped is not None:
            body_tap = await self._open_body_tap(
                page, tapped, (limits or ObservationLimits()).max_body_bytes
            )
        observation = Observation(match, page, limits, body_tap)
        observation.start()
        self._observations.append(observation)
        return observation

    async def _open_body_tap(
        self, page: PageLike, match: ResponseMatch, max_body_bytes: int
    ) -> BodyTap | None:
        """One read-only CDP session on this run's tab, for :class:`BodyTap` (#200).

        The only CDP session in the package, and the only two things it ever sends
        (ADR 0006's amendment for #200; ``tests/test_browser_safety.py`` pins both):

        - ``Network.enable``, with its own bounded buffers: this session hears the
          tab's network events. It changes no request, and nothing the page can see.
        - ``Network.streamResourceContent``, for an answer the tap's match names, when
          its response arrives: Chrome then forwards that answer's data to this session
          as it arrives. It changes nothing about the request or what the page gets.

        No request is held, changed, answered, blocked, or added, and the added delay
        is negligible (each session has its own agent; there is no backpressure on the
        page's loader). ``None`` when the
        session cannot start (a browser without the method, a fake tab): the
        observation then reads the way it always has.
        """
        context = cast(_TapContext, self._attachment.context)
        try:
            session = cast(_CdpSessionLike, await context.new_cdp_session(page))
        except Exception as exc:
            log.info("observation: no body tap on this tab (%s)", type(exc).__name__)
            return None
        tap = BodyTap(
            match,
            stream=lambda request_id: session.send(
                "Network.streamResourceContent", {"requestId": request_id}
            ),
            detach=session.detach,
            max_body_bytes=max_body_bytes,
        )
        try:
            for event, handler in tap.handlers():
                session.on(event, handler)
            await session.send(
                "Network.enable",
                {
                    "maxTotalBufferSize": TAP_TOTAL_BUFFER_BYTES,
                    "maxResourceBufferSize": TAP_RESOURCE_BUFFER_BYTES,
                },
            )
        except BaseException as exc:
            # Whatever stopped the start -- a refusal, a listener that raised, or a
            # cancellation -- the session is detached before anything else happens,
            # so no half-started tap stays attached to the user's tab (#202 review).
            await tap.close()
            if not isinstance(exc, Exception):
                raise
            log.info("observation: the body tap could not start (%s)", type(exc).__name__)
            return None
        return tap

    async def click_contact_info(
        self,
        profile_path: str,
        *,
        pause_s: float,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    ) -> ContactInfoClick:
        """ADR 0006's one click: **Contact info**, once, on the profile the tab is on (#190).

        The only input netkeeper gives a LinkedIn page other than navigation and
        :meth:`scroll`'s wheel replay, and the only method in the package that clicks
        (``tests/test_browser_safety.py`` allows this one call and no other). It:

        1. refuses when the run's tab is gone -- it never reopens one here, because a
           reopened tab would have to navigate, and a navigation is a page view nobody
           planned (:class:`BrowserUnavailable`);
        2. refuses when the tab is not on ``profile_path`` (``/in/<slug>/``), so a
           redirect or a stale tab never gets a click meant for another profile;
        3. waits ``pause_s``, the pause a person takes before reaching for the link;
        4. finds the control by its accessible role and name (a link named exactly
           "Contact info") and refuses unless there is exactly one, and unless its
           href is this profile's ``overlay/contact-info/``;
        5. clicks it once, at the control's own box, with a press as long as a
           person's. Playwright waits for the control to be clickable, up to
           :data:`CONTACT_INFO_CLICK_TIMEOUT_MS`, moves the pointer to the control's
           center, and sends one press and one release there -- never at a pointer
           position nothing placed; its strict mode refuses the click outright if a
           second match appeared meanwhile. A click that fails is not tried again.

        Refusals come back as :class:`ContactInfoClick` with ``clicked`` false; the
        caller counts the profile unreadable. Nothing here reads the answer: the
        caller's :meth:`observe` does. The overlay is left open: the next step of a
        run is a navigation, which leaves the page the way a person's next click on
        a link would, so there is nothing to close.
        """
        page = self._page
        if page is None or page.is_closed():
            raise BrowserUnavailable(
                "the run's tab went away before the Contact info click; aborting the run"
                " rather than reopen it, which would be a page view nobody planned"
            )
        if not _on_path(page.url, profile_path):
            return ContactInfoClick(page, False, "the tab is not on the profile")
        await sleep(pause_s)
        if page.is_closed():
            raise BrowserUnavailable("the run's tab went away before the Contact info click")
        if not _on_path(page.url, profile_path):
            return ContactInfoClick(page, False, "the tab left the profile before the click")
        control = cast(_ClickablePage, page).get_by_role(
            CONTACT_INFO_ROLE, name=CONTACT_INFO_NAME, exact=True
        )
        try:
            matches = await control.count()
            href = await control.get_attribute("href", timeout=1_000) if matches == 1 else None
        except Exception as exc:
            if self._lost(page):
                raise BrowserUnavailable("lost the tab while finding Contact info") from exc
            return ContactInfoClick(page, False, "the control could not be read")
        if matches == 0:
            return ContactInfoClick(page, False, "no Contact info control on the page")
        if matches > 1:
            return ContactInfoClick(page, False, "more than one Contact info control")
        expected = f"{_trim_path(profile_path)}/{CONTACT_INFO_HREF_SUFFIX}"
        if href is None or not _on_path(href, expected, base=page.url):
            return ContactInfoClick(page, False, "the control opens something else")
        try:
            await control.click(delay=CONTACT_INFO_PRESS_MS, timeout=CONTACT_INFO_CLICK_TIMEOUT_MS)
        except Exception as exc:
            if self._lost(page):
                raise BrowserUnavailable("lost the tab during the Contact info click") from exc
            log.warning("the Contact info control could not be clicked (%s)", type(exc).__name__)
            return ContactInfoClick(page, False, "the control could not be clicked")
        return ContactInfoClick(page, True)

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
        observations, self._observations = self._observations, []
        for observation in observations:
            await observation.close()
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
        # A new tab's pointer is wherever Playwright's virtual one starts -- not
        # wherever the lost tab's happened to be rested (#192).
        self._pointer_rested = False
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
        self._attachment = await self._provider._attach()
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

    async def _attach(self) -> Attachment:
        """Connect to the user's Chrome and reuse the context that is already open.

        Private on purpose: it opens a CDP client and takes no lock, so it may only
        run under the one :meth:`run` holds -- from :meth:`run` itself, or from the
        reattach inside :class:`BrowserRun`, which exists only inside :meth:`run`.
        A public ``attach`` would be a route around the activity lock.

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
            attachment = await self._attach()
            run = BrowserRun(self, account, attachment)
            try:
                yield run
            finally:
                await run.close()


async def _content_box(page: PageLike) -> Mapping[str, float] | None:
    """:data:`CONTENT_LANDMARK_SELECTOR`'s box, or ``None`` when there is nothing to read.

    A passive geometry read (see :class:`_LocatorLike`), not a page input.
    ``None`` covers every way there is nothing to rest on: no landmark on the
    page, one present but not laid out (``bounding_box`` itself returns ``None``
    for a detached or invisible element), or the read simply took too long
    (:data:`CONTENT_BOX_TIMEOUT_MS`) to be worth waiting on -- a page whose
    layout is still settling is not worth blocking a run's pacing over, and
    :meth:`BrowserRun._rest_pointer_over_content` falls back to a viewport guess
    either way.
    """
    locator = cast(_ScrollablePage, page).locator(CONTENT_LANDMARK_SELECTOR).first
    try:
        return await locator.bounding_box(timeout=CONTENT_BOX_TIMEOUT_MS)
    except Exception as exc:
        log.debug("could not read a content box to rest the pointer over: %s", exc)
        return None


def _viewport_size(page: PageLike) -> tuple[float, float]:
    """This tab's viewport, or a conservative default when Playwright does not know it.

    See :attr:`_ScrollablePage.viewport_size`: a passive read, commonly ``None`` for
    a tab this run attaches to, in which case :data:`DEFAULT_VIEWPORT_WIDTH` and
    :data:`DEFAULT_VIEWPORT_HEIGHT` stand in.
    """
    size = cast(_ScrollablePage, page).viewport_size
    if size is None:
        return float(DEFAULT_VIEWPORT_WIDTH), float(DEFAULT_VIEWPORT_HEIGHT)
    return float(size["width"]), float(size["height"])


def _known_viewport_height(page: PageLike) -> float | None:
    """This tab's real viewport height, only when Playwright actually knows it.

    Unlike :func:`_viewport_size`, no default stands in here: a *guessed* height
    used to cap a content box's jitter (#192 review round 2, N1) could clip a
    real, taller window for no reason, which is exactly the class of bug this
    round of the fix exists to get rid of. ``None`` when unknown -- the common
    case for a tab this run attaches to -- means the caller's own box-derived
    bound stands unchanged.
    """
    size = cast(_ScrollablePage, page).viewport_size
    return float(size["height"]) if size is not None else None


def _clamp(value: float, low: float, high: float) -> float:
    """``value``, pinned inside ``[low, high]``."""
    return max(low, min(value, high))


def _trim_path(path: str) -> str:
    return path[:-1] if len(path) > 1 and path.endswith("/") else path


def _on_path(url: str, path: str, *, base: str | None = None) -> bool:
    """Whether ``url`` (absolute, or relative to ``base``) is at ``path``, same origin.

    Compared after percent-decoding and case-folding, as LinkedIn's routing reads a
    slug, and after dropping one trailing ``/``. A relative ``url`` takes ``base``'s
    origin; an absolute one must have it.
    """
    try:
        split = urlsplit(urljoin(base, url) if base is not None else url)
        if base is not None:
            want = urlsplit(base)
            if (split.scheme, split.hostname, split.port) != (
                want.scheme,
                want.hostname,
                want.port,
            ):
                return False
    except ValueError:
        return False
    return _trim_path(unquote(split.path)).casefold() == _trim_path(unquote(path)).casefold()


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


def is_navigation_timeout(exc: BaseException) -> bool:
    """Whether ``exc`` is Playwright's own ``TimeoutError`` (a navigation that never loaded).

    Not the builtin ``TimeoutError`` (Playwright's does not derive from it), and not
    any other Playwright error: a caller that forgives a slow page must not forgive a
    refused one. Imported here, lazily, like the rest of Playwright in this module.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    return isinstance(exc, PlaywrightTimeoutError)


def _tap_match(match: ResponseMatch, tap: bool | ResponseMatch) -> ResponseMatch | None:
    """What an observation's body tap streams: nothing, all of ``match``, or a narrowing.

    A narrower match must name the same origin and only rules ``match`` names, so a tap
    never streams an answer its observation does not keep.
    """
    if isinstance(tap, bool):
        return match if tap else None
    if tap.origin != match.origin or not set(tap.rules) <= set(match.rules):
        raise ValueError("a body tap may only narrow its observation's match")
    return tap
