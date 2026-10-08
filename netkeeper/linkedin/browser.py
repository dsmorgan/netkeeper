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
import enum
import inspect
import logging
import os
import random
import re
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast, runtime_checkable
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from netkeeper.linkedin import activity_lock
from netkeeper.linkedin.activity_lock import LEGACY_SHARED_KEY
from netkeeper.linkedin.activity_lock import SINGLE_ACCOUNT_KEY as SINGLE_ACCOUNT_KEY
from netkeeper.linkedin.body_tap import BodyTap
from netkeeper.linkedin.errors import BrowserBusy as BrowserBusy
from netkeeper.linkedin.errors import BrowserError as BrowserError
from netkeeper.linkedin.errors import BrowserUnavailable as BrowserUnavailable
from netkeeper.linkedin.messaging import (
    SendPermit,
    composer_text,
    composer_text_matches,
    plan_control_characters,
)
from netkeeper.linkedin.observe import (
    ListenablePage,
    Observation,
    ObservationLimits,
    ResponseMatch,
)
from netkeeper.linkedin.pacing import ScrollPlan, TypingPlan, rest_pointer_like_a_person

log = logging.getLogger(__name__)

#: The only browser mode there is (ADR 0002).
ATTACH = "attach"

#: Directory under the data directory that the user points Chrome's ``--user-data-dir``
#: at. Chrome 136 and later refuse a debug port on the default profile directory, so
#: the sidecar's Chrome has its own (spec 9.1). netkeeper never creates or writes it:
#: Chrome does, when the user runs the command `netkeeper browser launch` prints.
CHROME_PROFILE_DIRNAME = "chrome-profile"


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

    def nth(self, index: int) -> _LocatorLike: ...

    async def count(self) -> int: ...

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
    """The slice of a Playwright ``CDPSession`` the body tap (#200), the Message
    click's geometry read (#444), and the background tab's opening (#195) use, and
    nothing more.

    ``send`` is called only in those methods, each with its own methods named as
    literals (``tests/test_browser_safety.py``); ``on`` only listens.
    """

    async def send(self, method: str, params: Mapping[str, Any] | None = None) -> Any: ...

    def on(self, event: str, handler: Callable[[Any], None]) -> None: ...

    async def detach(self) -> None: ...


class _TapContext(Protocol):
    """``new_cdp_session``, borrowed by :meth:`BrowserRun._open_body_tap` (#200),
    :meth:`BrowserRun._read_click_geometry` (#444), and :meth:`BrowserRun._target_id`
    (#195) alone.

    :class:`ContextLike` leaves every context mutator out so that reaching for one is
    a type error; this protocol hands the one method those three need to them, the way
    :class:`_ObservablePage` borrows the listener methods.
    """

    async def new_cdp_session(self, page: Any) -> Any: ...


class _TargetBrowser(Protocol):
    """``new_browser_cdp_session``, borrowed by :meth:`BrowserRun._open_tab` alone (#195).

    :class:`BrowserLike` leaves it out for the reason :class:`_TapContext` exists: a
    browser-level session reaches every target. :meth:`BrowserRun._open_tab` sends it
    ``Target.createTarget`` and ``Target.closeTarget`` and nothing else.
    """

    async def new_browser_cdp_session(self) -> Any: ...


#: #195: how long :meth:`BrowserRun._open_tab` waits for the tab it created in the
#: background to show up in the context's ``pages``, and how often it looks. Playwright
#: adopts a new target within milliseconds; the wait only bounds a browser that never
#: reports it.
BACKGROUND_TAB_WAIT_S: Final = 5.0
BACKGROUND_TAB_POLL_S: Final = 0.05


#: The Network buffers the body tap's own session asks Chrome for (#200): one answer
#: up to the observation's body limit, and a few of them at once. Chrome needs a
#: buffer to stream from; it is the tap's session's own, not the one Playwright reads.
TAP_RESOURCE_BUFFER_BYTES: Final = 8 * 1024 * 1024
TAP_TOTAL_BUFFER_BYTES: Final = 32 * 1024 * 1024
#: The longest the dwell at the end of :meth:`BrowserRun.scroll` is waited out in one
#: sleep when the caller supplies ``cancelled``: it polls between slices (#177).
SCROLL_CANCEL_SLICE_S: Final = 1.0


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

#: How many matches of a caller's ``rest_over`` selector :func:`_first_visible_box`
#: looks at before it gives up (#439). The first on-screen one is nearly always the
#: first or second; a bound keeps a page with hundreds of hidden matches from costing a
#: read each.
REST_MAX_CANDIDATES: Final = 8

#: How long to wait for each match after the first. The first match has the full
#: :data:`CONTENT_BOX_TIMEOUT_MS` (a list may still be drawing); a later one is already
#: in the page or not at all.
REST_CANDIDATE_TIMEOUT_MS: Final = 200.0

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


# --- ADR 0007: the prefill's two inputs and its ending ---------------------------------


class _MessagingLocator(Protocol):
    """The slice of a Playwright ``Locator`` the prefill's two methods use (ADR 0007).

    Local to :meth:`BrowserRun.click_message` and :meth:`BrowserRun.type_into_composer`:
    finding controls by role and name, narrowing, counting, and reading an attribute
    or the rendered text. The one click is the only input here; the keys go through
    :class:`_KeyboardLike`. Nothing here runs script in the page.
    """

    @property
    def first(self) -> _MessagingLocator: ...

    @property
    def last(self) -> _MessagingLocator: ...

    def nth(self, index: int) -> _MessagingLocator: ...

    def and_(self, locator: _MessagingLocator) -> _MessagingLocator: ...

    def filter(
        self, *, has: _MessagingLocator | None = None, visible: bool | None = None
    ) -> _MessagingLocator: ...

    def locator(self, selector: str) -> _MessagingLocator: ...

    def get_by_role(
        self,
        role: str,
        *,
        name: str | re.Pattern[str] | None = None,
        exact: bool | None = None,
        include_hidden: bool | None = None,
        level: int | None = None,
    ) -> _MessagingLocator: ...

    async def count(self) -> int: ...

    async def get_attribute(
        self,
        name: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> str | None: ...

    async def bounding_box(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> Mapping[str, float] | None: ...

    async def inner_text(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> str: ...

    async def text_content(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> str | None: ...

    async def click(
        self,
        *,
        delay: float | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> None: ...

    async def focus(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> None: ...

    async def is_enabled(
        self,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- Playwright's own signature
    ) -> bool: ...


class _KeyboardLike(Protocol):
    """The three keyboard calls ADR 0007 allows, in :meth:`BrowserRun.type_into_composer`
    alone. No ``down``, no ``up``: a modifier is never held."""

    async def type(self, text: str) -> None: ...

    async def insert_text(self, text: str) -> None: ...

    async def press(self, key: str) -> None: ...


class _MessagingPage(PageLike, Protocol):
    """A tab the prefill finds its controls on, types into, and brings to the front once."""

    @property
    def keyboard(self) -> _KeyboardLike: ...

    def locator(self, selector: str) -> _MessagingLocator: ...

    def get_by_role(
        self,
        role: str,
        *,
        name: str | re.Pattern[str] | None = None,
        exact: bool | None = None,
        include_hidden: bool | None = None,
        level: int | None = None,
    ) -> _MessagingLocator: ...

    async def bring_to_front(self) -> None: ...


#: The Message control (ADR 0007, "One click on Message"): role and name, exact.
MESSAGE_CONTROL_ROLE: Final = "link"
MESSAGE_CONTROL_NAME: Final = "Message"
#: The compose link's path; its host, when absolute, is LinkedIn's own.
MESSAGE_COMPOSE_PATH: Final = "/messaging/compose/"
MESSAGE_COMPOSE_HOST: Final = "www.linkedin.com"
#: How long Playwright may wait for the Message control to be clickable, in milliseconds.
MESSAGE_CLICK_TIMEOUT_MS: Final = 10_000.0
#: Time between the press and the release, as a person's click takes, in milliseconds.
MESSAGE_PRESS_MS: Final = 90.0
#: The composer: role and name, exact. The last character is U+2026, the ellipsis.
COMPOSER_ROLE: Final = "textbox"
COMPOSER_NAME: Final = "Write a message…"
#: The existing conversation's bubble: role and name, exact.
BUBBLE_ROLE: Final = "dialog"
BUBBLE_NAME: Final = "Messaging"
#: Where the existing conversation's bubble names its recipient: its header's ``h2`` link.
BUBBLE_HEADER_LINK: Final = "header h2 a"
#: The never-messaged bubble: its heading, its recipient field, and its recipient chip.
NEW_MESSAGE_ROLE: Final = "heading"
NEW_MESSAGE_NAME: Final = "New message"
RECIPIENTS_FIELD_ROLE: Final = "combobox"
RECIPIENTS_FIELD_NAME: Final = "Enter message recipients"
CHIP_ROLE: Final = "button"
CHIP_NAME: Final = re.compile("^Remove ")
#: The chip's name: this prefix, then the recipient's name.
CHIP_NAME_PREFIX: Final = "Remove "
#: The composer's paragraphs, and what in it the text rule can't read: any element that
#: is not a paragraph, or anything but a ``<br>`` inside one.
COMPOSER_PARAGRAPH: Final = "p"
COMPOSER_UNREADABLE: Final = ":scope > :not(p), p :not(br)"
#: The profile page's own heading, read once before the click for the chip check.
PROFILE_HEADING: Final = "h1"
#: The never-messaged bubble's scope: the composer's nearest ancestor that holds the
#: ``New message`` heading (ADR 0007), found upward from the verified composer.
NEW_MESSAGE_SCOPE: Final = "xpath=ancestor::*[.//h2[normalize-space()='New message']][1]"
#: The refusal a chip whose name isn't ``Remove <the profile's h1>`` gives (ADR 0007).
RECIPIENT_NAME_MISMATCH: Final = "recipient_name_mismatch"
#: The refusal a compose option that arrived after the verified one gives (ADR 0007).
ANOTHER_COMPOSE: Final = "another_compose"
#: The refusal a chip whose accessible name the matcher can't confirm gives.
RECIPIENT_NAME_UNREADABLE: Final = "recipient_name_unreadable"
#: Elements whose text is not part of an accessible name.
ARIA_HIDDEN: Final = '[aria-hidden="true"]'
#: A focus match, read through Playwright's selector engine, never through script.
FOCUSED: Final = ":focus"
#: A profile's own path prefix, as the bubbles link to it.
PROFILE_PATH_PREFIX: Final = "/in/"
#: How long the prefill waits, reading only, for the bubble's composer to be drawn after
#: the compose option arrived, in seconds, and in how many reads.
COMPOSER_WAIT_S: Final = 5.0
COMPOSER_WAIT_POLLS: Final = 50
#: ADR 0007's decision 5: the maintainer chose option B (2026-10-06). The composer wait
#: may reach :meth:`BrowserRun._focus_seam` once, which makes one ``Locator.focus()`` on
#: the verified composer, never a click. ``False`` is option A: no focusing input.
FOCUS_INPUT_AUTHORIZED: Final = True
#: How long the one ``focus()`` call may wait for the composer, in milliseconds.
FOCUS_TIMEOUT_MS: Final = 1_000.0
#: How long one read of an attribute or the composer's text may wait, in milliseconds.
MESSAGING_READ_TIMEOUT_MS: Final = 1_000.0
#: ADR 0008, auto-send's one click: the composer form's submit button, by role and name,
#: exact (``Open send options`` beside it never matches), and the ``type`` it must carry.
SEND_CONTROL_ROLE: Final = "button"
SEND_CONTROL_NAME: Final = "Send"
SEND_CONTROL_TYPE: Final = "submit"
#: The verified composer's own form, found upward from it, never by its ``msg-form-`` id.
COMPOSER_FORM: Final = "xpath=ancestor::form[1]"
#: How long Playwright may wait for Send to be clickable, in milliseconds.
SEND_CLICK_TIMEOUT_MS: Final = 1_000.0
#: Time between the press and the release of the Send click, in milliseconds.
SEND_PRESS_MS: Final = 90.0
#: ADR 0008 (decision D1): after a landed Send, the existing-conversation bubble's own
#: close control, ``Close your conversation with <the header link's name>`` (the
#: 2026-10-05 capture, ``docs/linkedin-messaging-shapes.md``), by role and exact name.
CLOSE_CONTROL_ROLE: Final = "button"
CLOSE_CONTROL_PREFIX: Final = "Close your conversation with "
#: How long the run waits, reading only, for the composer to empty after Send, and then
#: for the bubble to go after its close click, in seconds, and in how many reads.
CLOSE_WAIT_S: Final = 5.0
CLOSE_WAIT_POLLS: Final = 25
#: How long Playwright may wait for the close control to be clickable, in milliseconds.
CLOSE_CLICK_TIMEOUT_MS: Final = 1_000.0


class BubbleLayout(enum.StrEnum):
    """Which bubble the compose option named (ADR 0007, "The recipient", 3)."""

    EXISTING = "existing"
    """``REPLY``: one ``Messaging`` dialog whose header links to ``/in/<profile id>/``."""

    NEVER_MESSAGED = "never_messaged"
    """``CONNECTION_MESSAGE``: ``New message``, one chip, and a card for ``/in/<slug>/``."""


@dataclass(frozen=True, slots=True)
class BubbleRecipient:
    """Who the bubble must be for: the layout, the bare profile id, and the slug."""

    layout: BubbleLayout
    profile_id: str
    public_id: str | None
    #: The profile page's one ``h1`` text, read before the click, when there was exactly
    #: one: the never-messaged bubble's chip must then be named ``Remove <it>``.
    profile_name: str | None = None


class ClickFailure(enum.StrEnum):
    """Why the one Message click raised, as a fixed category (#444).

    Read from Playwright's own actionability log by :func:`classify_click_failure`.
    Only the category is recorded or logged, never the exception's text, which quotes
    the page's markup."""

    INTERCEPTED = "intercepted"
    """Another element sat over the control's click point."""

    OUTSIDE_VIEWPORT = "outside_viewport"
    """The control could not be scrolled into the viewport (a fixed copy off screen)."""

    NOT_VISIBLE = "not_visible"
    """The control stopped being visible."""

    NOT_STABLE = "not_stable"
    """The control kept moving (an animation)."""

    DETACHED = "detached"
    """The control left the page, or no element matched any more."""

    TIMEOUT = "timeout"
    """The click timed out for a reason the log doesn't name."""

    OTHER = "other"
    """Any other exception."""


class ClickTarget(enum.StrEnum):
    """Which Message control :meth:`BrowserRun.click_message` chose, and why (#444)."""

    TOP_CARD = "top_card"
    """The profile's top card control: on screen, and nothing over its click point."""

    ON_SCREEN = "on_screen"
    """Another control for the contact, on screen, and nothing over its click point."""

    TOP_CARD_OFF_SCREEN = "top_card_off_screen"
    """No control was on screen and clear; the top card's control lay outside the
    viewport, so Playwright scrolls it into view as part of the click."""

    UNCHECKED = "unchecked"
    """The geometry couldn't be read: the top card's control, else the first visible one."""


@dataclass(frozen=True, slots=True)
class MessageClick:
    """What :meth:`BrowserRun.click_message` did. ``attempted`` is true once the click
    was sent to Playwright, whether or not it landed: from then on a bubble may be open
    and the run never navigates again. ``refusal`` is fixed words. ``target`` is which
    control was chosen, and ``failure`` why an attempted click raised (#444)."""

    clicked: bool
    attempted: bool
    refusal: str | None = None
    target: ClickTarget | None = None
    failure: ClickFailure | None = None


@dataclass(frozen=True, slots=True)
class _MessageControls:
    """What :meth:`BrowserRun._find_message_controls` found: decision 4's refusal (if
    any), the visible controls with their boxes, which of them is the top card's, and
    the verified compose ``href`` values the geometry read looks up."""

    refusal: str | None
    candidates: list[_MessagingLocator]
    boxes: list[Mapping[str, float] | None]
    top_card: int | None
    verified: list[str]


#: The markers of Playwright's actionability log (the call log a failed click's error
#: carries), each with its category. The last marker line in the log wins: it's the
#: state the click was waiting on when it gave up.
_CLICK_FAILURE_MARKERS: Final = (
    ("intercepts pointer events", ClickFailure.INTERCEPTED),
    ("outside of the viewport", ClickFailure.OUTSIDE_VIEWPORT),
    ("element is not visible", ClickFailure.NOT_VISIBLE),
    ("element is not stable", ClickFailure.NOT_STABLE),
    ("detached from the dom", ClickFailure.DETACHED),
    ("not attached to the dom", ClickFailure.DETACHED),
)
#: What Playwright's call log says once the locator matched an element.
_LOCATOR_RESOLVED: Final = "locator resolved to"


def classify_click_failure(exc: BaseException) -> ClickFailure:
    """The fixed category for an exception the Message click raised (#444).

    Reads the actionability log in Playwright's error text, latest line first. A
    timeout whose locator never matched is ``detached``: the control was found before
    the click and was gone by then. The text itself is never returned or logged."""
    lines = str(exc).casefold().splitlines()
    for line in reversed(lines):
        for marker, category in _CLICK_FAILURE_MARKERS:
            if marker in line:
                return category
    if type(exc).__name__ == "TimeoutError":
        resolved = any(_LOCATOR_RESOLVED in line for line in lines)
        return ClickFailure.TIMEOUT if resolved else ClickFailure.DETACHED
    return ClickFailure.OTHER


#: The profile's top card control: the first Message link after the profile's one
#: ``h1`` in document order, read relative to that ``h1`` (#444). The capture shows no
#: landmark for the top card (ADR 0007), but the name heading opens it, and its actions
#: follow the name; a sticky copy or a hidden layout variant sits elsewhere.
MESSAGE_TOP_CARD: Final = "xpath=following::a"
#: How many visible Message controls the click looks at. The capture rendered three.
MESSAGE_MAX_CANDIDATES: Final = 8
#: How many links with a verified compose ``href`` the geometry read describes. Hidden
#: copies count here (the read can't tell them apart before it asks), so the cap sits far
#: above the three the capture rendered; every one already names the contact (decision 4).
MESSAGE_MAX_LINKS: Final = 32
#: How long the whole geometry read may take, in seconds. A renderer that hangs makes the
#: read fail, and the click chooses without geometry.
GEOMETRY_TIMEOUT_S: Final = 3.0
#: How far a hit element's box may stick out of the control's box and still count as
#: the control's own (a rounding of sub-pixel layout), in CSS pixels.
HIT_TOLERANCE_PX: Final = 1.0


@dataclass(frozen=True, slots=True)
class ClickGeometry:
    """The page's own geometry, read over CDP before the Message click (#444).

    ``viewport`` is the layout viewport's width and height in CSS pixels. ``hits``
    holds, for each candidate box asked about, the box of the contact's Message link
    that holds the element the page would hit at that box's center (the link itself,
    or its icon or label), or ``None`` when that element is in no such link (something
    covers the point) or wasn't read (the center was off screen)."""

    viewport: tuple[float, float]
    hits: tuple[Mapping[str, float] | None, ...]
    #: Where Playwright's click would press each candidate (:func:`_click_point`), or
    #: ``None`` when that wasn't read. Only :func:`not_clear_reason` reads it (#470).
    points: tuple[tuple[float, float] | None, ...] = ()


def _on_screen(box: Mapping[str, float] | None, viewport: tuple[float, float]) -> bool:
    """Whether ``box`` has an area and lies wholly inside the viewport, so Playwright's
    click needn't scroll and the geometry read before it still holds."""
    if box is None or box["width"] <= 0 or box["height"] <= 0:
        return False
    width, height = viewport
    return (
        box["x"] >= 0
        and box["y"] >= 0
        and box["x"] + box["width"] <= width
        and box["y"] + box["height"] <= height
    )


def _inside(inner: Mapping[str, float], outer: Mapping[str, float]) -> bool:
    slack = HIT_TOLERANCE_PX
    return (
        inner["x"] >= outer["x"] - slack
        and inner["y"] >= outer["y"] - slack
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + slack
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + slack
    )


def _clear(box: Mapping[str, float], hit: Mapping[str, float] | None) -> bool:
    """Whether the element at ``box``'s center belongs to this control: the Message link
    holding it has this control's box, give or take :data:`HIT_TOLERANCE_PX`. An element
    in no Message link (a bubble, the Messaging bar, a sticky header) covers the click
    point, and so does another copy of the link over this one."""
    return hit is not None and _inside(hit, box) and _inside(box, hit)


#: The refusal, before the click, when a message bubble is already on the page (#444).
BUBBLE_ALREADY_OPEN: Final = (
    "a message bubble is already open in Chrome, minimized ones included; close it, then try again"
)
#: The refusal, before the click, when that check couldn't read the page (#444).
BUBBLE_UNREADABLE: Final = "whether a message bubble is open could not be read"
#: The refusal when no control is on screen and clear, and none can be scrolled to.
MESSAGE_NOT_ON_SCREEN: Final = (
    "no Message control is on screen with nothing over it; close or move what covers it"
)


def choose_message_target(
    boxes: Sequence[Mapping[str, float] | None],
    top_card: int | None,
    geometry: ClickGeometry | None,
) -> tuple[int, ClickTarget] | None:
    """Which visible Message control to click, or ``None`` for none (#444, ADR 0007).

    ``boxes`` are the visible controls' boxes, in the order they're tried after the top
    card; ``top_card`` is the index of the top card's control among them, if there is
    one. Every one already names the contact (decision 4). In order:

    1. The top card's control, if it is on screen (:func:`_on_screen`) and nothing is
       over its center (:func:`_clear`).
    2. Otherwise the first other control that is on screen and clear.
    3. Otherwise the top card's control, if it isn't wholly on screen: Playwright scrolls
       it into view as part of the click. One that is on screen but covered is refused.
    4. Otherwise ``None``: the caller refuses with :data:`MESSAGE_NOT_ON_SCREEN`.

    Without ``geometry`` (the read failed), the top card's control, else the first.
    """
    if not boxes:
        return None
    if geometry is None:
        return (top_card if top_card is not None else 0), ClickTarget.UNCHECKED
    order = list(range(len(boxes)))
    if top_card is not None:
        order.remove(top_card)
        order.insert(0, top_card)
    for index in order:
        box = boxes[index]
        hit = geometry.hits[index] if index < len(geometry.hits) else None
        if box is not None and _on_screen(box, geometry.viewport) and _clear(box, hit):
            return index, ClickTarget.TOP_CARD if index == top_card else ClickTarget.ON_SCREEN
    if (
        top_card is not None
        and boxes[top_card] is not None
        and not _on_screen(boxes[top_card], geometry.viewport)
    ):
        return top_card, ClickTarget.TOP_CARD_OFF_SCREEN
    return None


class NotClear(enum.StrEnum):
    """Why no Message control was clear to click, as a fixed category (#470).

    Logged when the click refuses, never with a name, a slug, a url, or the page's text.
    What covers a control is inferred from where its click point sits, not read from the
    page: the hit test learns only that the element there isn't the control's own."""

    NO_CANDIDATES = "no_candidates"
    """No visible Message link for the contact."""

    NO_TOP_CARD = "no_top_card"
    """No top card (no single ``h1``), and no other control on screen and clear."""

    TOP_CARD_OFF_SCREEN = "top_card_off_screen"
    """The top card's control has no box, so there's nothing to scroll to."""

    COVERED_BY_STICKY_HEADER = "covered_by_sticky_header"
    """The top card's control is on screen, and its click point is covered within
    :data:`STICKY_HEADER_BAND_PX` of the viewport's top: under LinkedIn's global
    navigation or the profile's sticky header."""

    COVERED_BY_BUBBLE = "covered_by_bubble"
    """The top card's control is on screen, and its click point is covered within
    :data:`BUBBLE_BAND_PX` of the viewport's bottom: under the Messaging bar or a
    minimized bubble."""

    COVERED_BY_OTHER = "covered_by_other"
    """The top card's control is on screen, and something else covers its click point."""


#: The categories in which the top card's control is on screen but covered: the prefill
#: scrolls back up once before the click when its probe reads one of these (#470).
TOP_CARD_COVERED: Final = frozenset(
    {NotClear.COVERED_BY_STICKY_HEADER, NotClear.COVERED_BY_BUBBLE, NotClear.COVERED_BY_OTHER}
)
#: How far down from the viewport's top a covered click point counts as under a sticky
#: header, in CSS pixels: LinkedIn's global navigation (about 52) and the profile's
#: sticky header below it, whose height was never captured (#429). Used for the log's
#: category only, never for a choice.
STICKY_HEADER_BAND_PX: Final = 160.0
#: How far up from the viewport's bottom a covered click point counts as under the
#: Messaging bar or a minimized bubble, in CSS pixels. Used for the log's category only.
BUBBLE_BAND_PX: Final = 64.0


def not_clear_reason(
    boxes: Sequence[Mapping[str, float] | None],
    top_card: int | None,
    geometry: ClickGeometry,
) -> NotClear:
    """Why :func:`choose_message_target` chose nothing, for the log (#470).

    Call it only when the choice was ``None`` with ``geometry`` read. The cover is
    placed by the top card's click point, or its box's center when that wasn't read."""
    if not boxes:
        return NotClear.NO_CANDIDATES
    if top_card is None:
        return NotClear.NO_TOP_CARD
    box = boxes[top_card]
    if box is None or not _on_screen(box, geometry.viewport):
        return NotClear.TOP_CARD_OFF_SCREEN
    point = geometry.points[top_card] if top_card < len(geometry.points) else None
    y = point[1] if point is not None else box["y"] + box["height"] / 2
    if y < STICKY_HEADER_BAND_PX:
        return NotClear.COVERED_BY_STICKY_HEADER
    if y >= geometry.viewport[1] - BUBBLE_BAND_PX:
        return NotClear.COVERED_BY_BUBBLE
    return NotClear.COVERED_BY_OTHER


def _quad_box(quad: Sequence[float]) -> Mapping[str, float]:
    """A CDP quad (four x, y corners) as a box with ``x``, ``y``, ``width``, ``height``."""
    xs, ys = quad[0::2], quad[1::2]
    return {"x": min(xs), "y": min(ys), "width": max(xs) - min(xs), "height": max(ys) - min(ys)}


@dataclass(frozen=True, slots=True)
class _Link:
    """One of the contact's Message links, as the geometry read saw it: its box, the
    point Playwright's click would press (``None`` when none is on screen), and the
    ``backendNodeId`` of it and everything inside it."""

    box: Mapping[str, float]
    point: tuple[float, float] | None
    owned: frozenset[int]


def _click_point(
    quads: Sequence[Sequence[float]], viewport: tuple[float, float]
) -> tuple[float, float] | None:
    """Where Playwright's click presses: the middle of the first of the element's content
    quads, clipped to the viewport, whose area is over 0.99 square pixels. An inline link
    that wraps, or holds a tall icon, has more than one quad, and its box's center may
    lie in none of them."""
    width, height = viewport
    for quad in quads:
        points = [
            (min(max(quad[i], 0.0), width), min(max(quad[i + 1], 0.0), height))
            for i in range(0, 8, 2)
        ]
        area = abs(
            sum(
                points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1]
                for i in range(4)
            )
            / 2
        )
        if area > 0.99:
            return sum(x for x, _ in points) / 4, sum(y for _, y in points) / 4
    return None


def _backend_ids(node: Mapping[str, Any]) -> list[int]:
    """Every ``backendNodeId`` in a ``DOM.describeNode`` answer's subtree, its own first."""
    ids = [int(node["backendNodeId"])]
    for child in node.get("children", ()):
        ids.extend(_backend_ids(child))
    return ids


async def _box(locator: _MessagingLocator) -> Mapping[str, float] | None:
    """A control's box, a passive geometry read; ``None`` when it has none."""
    return await locator.bounding_box(timeout=MESSAGING_READ_TIMEOUT_MS)


class TypingEnd(enum.StrEnum):
    """How :meth:`BrowserRun.type_into_composer` ended."""

    TYPED = "typed"
    """Every step was typed and the composer holds the whole body."""

    NOT_TYPED = "not_typed"
    """Refused before the first key."""

    PARTIALLY_TYPED = "partially_typed"
    """A check failed, or a cancel came, after the first key: typing stopped."""

    UNKNOWN = "unknown"
    """The tab, the browser, or a key call failed after the first key."""


@dataclass(frozen=True, slots=True)
class _SendControl:
    """The Send control's locators: the composer's form, every button named Send on the
    page, and the one of those in that form."""

    forms: _MessagingLocator
    on_page: _MessagingLocator
    control: _MessagingLocator


@dataclass(frozen=True, slots=True)
class SendClick:
    """What :meth:`BrowserRun.click_send` did (ADR 0008).

    ``attempted`` is true once the click was sent to Playwright, whether or not it
    landed: from then on nobody knows whether the message went, and nothing clicks
    again. ``clicked_at`` is taken just before the click. A refusal before the click is
    fixed words in ``refusal``; ``composer_changed`` says whether it was the composer
    itself (its text, its recipient, its focus, or another composer) that no longer
    held, rather than a gate or the Send control."""

    clicked: bool
    attempted: bool
    refusal: str | None = None
    composer_changed: bool = False
    clicked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BubbleClose:
    """What :meth:`BrowserRun.close_sent_bubble` did (ADR 0008, D1). ``closed`` is true
    only when the close click landed and the composer then went from the page.
    ``refusal`` is fixed words."""

    closed: bool
    attempted: bool
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class TypingResult:
    """What :meth:`BrowserRun.type_into_composer` did. ``started_at`` is taken before the
    first key, and is ``None`` when no key was attempted. ``typed_chars`` counts the
    characters of the steps whose key call was attempted. ``reason`` is fixed words."""

    end: TypingEnd
    reason: str
    typed_chars: int
    started_at: datetime | None


def message_control_refusal(hrefs: Sequence[str | None], profile_id: str) -> str | None:
    """ADR 0007's Message click rule (decision 4), as a pure check of every control's href.

    ``None`` when there is at least one control and **every** control named Message
    opens ``/messaging/compose/`` (relative, or on :data:`MESSAGE_COMPOSE_HOST`) for this
    contact: ``profileUrn`` (decoded) ``urn:li:fsd_profile:<id>`` and ``recipient``
    ``<id>``, with each query parameter present exactly once. Otherwise a fixed phrase.
    """
    if not hrefs:
        return "no Message control on the page"
    for href in hrefs:
        if href is None or not _is_contact_compose(href, profile_id):
            return "a Message control opens something other than this contact's compose"
    return None


def _is_contact_compose(href: str, profile_id: str) -> bool:
    try:
        split = urlsplit(href)
    except ValueError:
        return False
    if (split.scheme or split.netloc) and (
        split.scheme != "https" or split.netloc != MESSAGE_COMPOSE_HOST
    ):
        return False
    if split.path != MESSAGE_COMPOSE_PATH or split.fragment:
        return False
    try:
        pairs = parse_qsl(split.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return False
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        return False
    values = dict(pairs)
    return (
        values.get("profileUrn") == f"urn:li:fsd_profile:{profile_id}"
        and values.get("recipient") == profile_id
    )


def _css_string(value: str) -> str:
    """``value`` as a double-quoted CSS string, for an attribute selector."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_profile_link(href: str | None, path: str, *, fold_case: bool) -> bool:
    """Whether ``href`` (relative, or on LinkedIn's own host) is exactly ``path``,
    percent-decoded, with one trailing ``/`` dropped. ``fold_case`` compares a vanity
    slug as LinkedIn's routing reads it, case-folded; a profile id is compared exactly."""
    if href is None:
        return False
    try:
        split = urlsplit(href)
    except ValueError:
        return False
    if (split.scheme or split.netloc) and (
        split.scheme != "https" or split.netloc != MESSAGE_COMPOSE_HOST
    ):
        return False
    seen, want = _trim_path(unquote(split.path)), _trim_path(unquote(path))
    return seen.casefold() == want.casefold() if fold_case else seen == want


def _profile_path(href: str | None) -> bool:
    """Whether ``href`` points at a profile (``/in/...``), on any host or none."""
    if href is None:
        return False
    try:
        return urlsplit(href).path.startswith(PROFILE_PATH_PREFIX)
    except ValueError:
        return False


def _one_composer(count: int) -> bool:
    """ADR 0007's decision 3: the page holds exactly one composer, hidden ones counted.

    Any other composer (a minimized bubble from an earlier prefill, or one the person
    opened) refuses the prefill, and the person closes it. The looser alternative the
    ADR names, allowing other people's minimized bubbles, would change only this."""
    return count == 1


async def _read_composer(composer: _MessagingLocator) -> str | None:
    """The composer's text under ADR 0007's rule (``messaging.composer_text``), or ``None``
    when it holds something the rule can't read: an element that is not a
    paragraph, or anything but a ``<br>`` inside one."""
    if await composer.locator(COMPOSER_UNREADABLE).count():
        return None
    paragraphs = composer.locator(COMPOSER_PARAGRAPH)
    texts: list[str] = []
    contents: list[str] = []
    for index in range(await paragraphs.count()):
        paragraph = paragraphs.nth(index)
        texts.append(await paragraph.inner_text(timeout=MESSAGING_READ_TIMEOUT_MS))
        contents.append(await paragraph.text_content(timeout=MESSAGING_READ_TIMEOUT_MS) or "")
    # Text outside every paragraph (a bare text node, as Chrome leaves after select-all,
    # Backspace, and typing) is invisible to the paragraph reads: the whole composer's
    # text must be exactly its paragraphs' text, or the composer is unreadable.
    whole = await composer.text_content(timeout=MESSAGING_READ_TIMEOUT_MS)
    if whole is None or whole != "".join(contents):
        return None
    return composer_text(texts)


def _name(text: str | None) -> str | None:
    """An accessible name as ADR 0007 compares it: whitespace collapsed to single spaces,
    trimmed, and normalized to NFC. ``None`` for no name."""
    if text is None:
        return None
    name = unicodedata.normalize("NFC", " ".join(text.split()))
    return name or None


async def _accessible_name(
    page: _MessagingPage,
    element: _MessagingLocator,
    role: str,
    *,
    level: int | None = None,
) -> str | None:
    """``element``'s accessible name, as :func:`_name` normalizes it, or ``None``.

    Playwright has no getter for an accessible name, so the candidates are read, in
    order: the ``aria-label``, the rendered text, and the rendered text without the
    text of its ``aria-hidden`` descendants (pronouns in a span, say). The first one
    that Playwright's own role-and-name matcher confirms, exact, is the name; when none
    is confirmed (an ``aria-labelledby``, for one), the name is unreadable. A visible
    element is matched with ``include_hidden=False``, so ``aria-hidden`` text is never
    part of its name. Reads only."""
    label = await element.get_attribute("aria-label", timeout=MESSAGING_READ_TIMEOUT_MS)
    text = await element.inner_text(timeout=MESSAGING_READ_TIMEOUT_MS)
    hidden_parts = element.locator(ARIA_HIDDEN)
    shown = text
    for index in range(await hidden_parts.count()):
        part = await hidden_parts.nth(index).inner_text(timeout=MESSAGING_READ_TIMEOUT_MS)
        if part:
            shown = shown.replace(part, " ", 1)
    visible = await element.filter(visible=True).count() == 1
    for raw in dict.fromkeys(c for c in (label, text, shown) if c is not None):
        candidate = " ".join(raw.split())
        if not candidate:
            continue
        matcher = page.get_by_role(
            role, name=candidate, exact=True, include_hidden=not visible, level=level
        )
        if await element.and_(matcher).count() == 1:
            return _name(candidate)
    return None


def _keyed(chunk: str) -> bool:
    """Whether a chunk is typed with ``keyboard.type``: one printable ASCII character
    other than a space. A space is inserted as text, so no Space key ever reaches a
    page whose focus might have moved to a button (ADR 0007 revision)."""
    return len(chunk) == 1 and 0x21 <= ord(chunk) <= 0x7E


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

    The tab is this run's alone. It is opened lazily, in the background (#195),
    reopened if the user closes it, and closed at the end of the run; the context and
    the browser are left exactly as they were found.
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
        #: ADR 0007: set once the Message click is sent. From then on the run never
        #: opens, reopens, or navigates a tab (:meth:`_ensure_page` refuses).
        self._message_clicked = False
        #: Whether that click landed (the page took it without an error).
        self._message_click_landed = False
        #: Which control the click chose, and why it raised, if it did (#444).
        self._click_target: ClickTarget | None = None
        self._click_failure: ClickFailure | None = None
        #: The tab's url when the Message click was sent; any change stops typing.
        self._click_url: str | None = None
        #: How many times :meth:`bring_tab_forward` ran: at most once per run.
        self._fronted = 0
        #: #195: whether a tab had to be opened in front because a background one
        #: couldn't be (:meth:`_open_tab`). The run's notes say so.
        self._opened_in_front = False
        #: How many key calls were attempted (ADR 0007's point of no return).
        self._keys_sent = 0
        self._handed_over = False
        #: Whether :meth:`_focus_seam` ran (ADR 0007, option B): at most once per run.
        self._focus_used = False
        #: ADR 0008: set once the Send click is sent. Nothing clicks Send twice.
        self._send_attempted = False
        #: Whether :meth:`type_into_composer` ended with the whole body in the composer.
        self._typed_whole = False
        #: ADR 0008: whether the Send click landed (sent, and the page took it).
        self._send_landed = False
        #: ADR 0008, D1 and D3: the one close click on the sent bubble, whether the bubble
        #: then went, and whether the run closed its own tab after that.
        self._close_attempted = False
        self._bubble_closed = False
        self._tab_closed = False

    @property
    def keys_sent(self) -> int:
        """How many key calls this run attempted on its tab (ADR 0007)."""
        return self._keys_sent

    @property
    def message_click_attempted(self) -> bool:
        """Whether this run sent its Message click (ADR 0007): from then on its tab is
        handed over, never closed, whatever the click's result."""
        return self._message_clicked

    @property
    def message_clicked(self) -> bool:
        """Whether the Message click landed: sent, and the page took it without an error."""
        return self._message_click_landed

    @property
    def message_click_diagnostics(self) -> dict[str, str | None]:
        """Which Message control was chosen, and why an attempted click raised, as fixed
        categories (#444); empty when no control was chosen."""
        if self._click_target is None:
            return {}
        failure = self._click_failure.value if self._click_failure is not None else None
        return {"message_click_target": self._click_target.value, "message_click_failure": failure}

    @property
    def bubble_closed(self) -> bool:
        """Whether :meth:`close_sent_bubble` closed the sent message's bubble (ADR 0008)."""
        return self._bubble_closed

    @property
    def tab_closed(self) -> bool:
        """Whether :meth:`close_sent_tab` closed this run's tab after a sent message."""
        return self._tab_closed

    @property
    def send_attempted(self) -> bool:
        """Whether this run sent its one Send click (ADR 0008), whatever its result."""
        return self._send_attempted

    @property
    def fronted(self) -> int:
        """How many times this run brought its tab to the front: 0 or 1."""
        return self._fronted

    @property
    def handed_over(self) -> bool:
        """Whether :meth:`hand_over` gave the tab to the person."""
        return self._handed_over

    @property
    def browser(self) -> BrowserLike:
        """The attached browser. Read-only as far as this package is concerned."""
        return self._attachment.browser

    @property
    def context(self) -> ContextLike:
        """The context this run reuses: the one the user's browsing already lives in."""
        return self._attachment.context

    @property
    def opened_in_front(self) -> bool:
        """Whether this run had to open a tab in front because a background one couldn't
        be opened (#195): Chrome may have taken focus. Never for a run that brought its
        tab forward (a prefill or an auto-send): taking focus is what that run does."""
        return self._opened_in_front and not self._fronted

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
        cancelled: Callable[[], bool | Awaitable[bool]] | None = None,
        rng: random.Random | None = None,
        rest_over: str | None = None,
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

        ``rest_over`` (#439) is a CSS selector for the element to rest the pointer
        over instead of the page's ``<main>``. A wheel scrolls the nearest scrollable
        ancestor of what is under the pointer, and on a split layout ``<main>``'s
        center can be over a different pane than the one the caller means to scroll.
        The first match with a box on screen is used (:func:`_first_visible_box`); the
        pointer goes to the center of that box. When nothing matches, or no match has a
        box, the rest falls back to ``<main>`` exactly as without it. ``None`` (the
        default) is that existing behavior, for every other caller. It is a selector,
        not a script: the only page reads are geometry reads, and the only inputs are
        still the pointer move and the wheel. Like the rest over ``<main>``, it happens
        once per open tab, on its first ``scroll``: a later call's ``rest_over`` (or a
        later navigation on the same tab) does not move the pointer again.

        ``cancelled``, when given (a plain callable, or one that returns an
        awaitable), is polled once before the pointer-rest walk begins, then
        again before every wheel event and again before the final dwell. The
        dwell is waited out in slices of at most :data:`SCROLL_CANCEL_SLICE_S`
        and polled after each, so a cancel stops it within one slice (#177). A
        caller wired to spec 9.9's cooperative cancel ("checked between profiles
        and inside sliced cooldowns") has somewhere to plug one in; nothing here
        reads a database or a settings flag itself (spec 9.10
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

        async def stopped() -> bool:
            if cancelled is None:
                return False
            answer = cancelled()
            return bool(await answer) if inspect.isawaitable(answer) else bool(answer)

        if await stopped():
            return ScrollOutcome(page=page, cancelled=True)
        rest_rng = rng if rng is not None else random.Random()  # noqa: S311 -- pacing, not crypto
        await self._rest_pointer_over_content(page, sleep=sleep, rng=rest_rng, rest_over=rest_over)
        for step in plan.steps:
            if await stopped():
                return ScrollOutcome(page=page, cancelled=True)
            await page.mouse.wheel(0, step.delta_px)
            await sleep(step.pause_s)
        if await stopped():
            return ScrollOutcome(page=page, cancelled=True)
        if cancelled is None:
            await sleep(plan.dwell_s)
            return ScrollOutcome(page=page, cancelled=False)
        # #177: a cancel during the dwell is heard within one slice, not after the whole wait.
        remaining = plan.dwell_s
        while remaining > 0:
            step_s = min(SCROLL_CANCEL_SLICE_S, remaining)
            await sleep(step_s)
            remaining -= step_s
            if remaining > 0 and await stopped():
                return ScrollOutcome(page=page, cancelled=True)
        return ScrollOutcome(page=page, cancelled=False)

    async def _rest_pointer_over_content(
        self,
        page: PageLike,
        *,
        sleep: Callable[[float], Awaitable[None]],
        rng: random.Random,
        rest_over: str | None = None,
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

        **The target, in order of preference (#192 review, F1; #439):**

        0. When the caller gave ``rest_over``, the center of the first match's box that
           is on screen (:func:`_first_visible_box`), with the jitter held inside that
           box. Still one bare point: ``mouse.move`` is given coordinates, never an
           element, and the box comes from the same passive geometry read as below.
           With no usable match this step is skipped, not an error.
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
        element = await _first_visible_box(page, rest_over) if rest_over is not None else None
        box = await _content_box(page) if element is None else None
        if element is not None:
            target_x = element["x"] + element["width"] / 2
            target_y = element["y"] + element["height"] / 2
            known_width = _known_viewport_width(page)
            jitter_x = (
                max(element["x"], 0.0),
                element["x"] + element["width"]
                if known_width is None
                else min(element["x"] + element["width"], known_width),
            )
            jitter_y = (max(element["y"], REST_MIN_Y_PX), element["y"] + element["height"])
        elif box is not None:
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

        One of the package's two CDP sessions (the other is
        :meth:`_read_click_geometry`'s, #444), and the only two things it ever sends
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

    # --- ADR 0007: the prefill ------------------------------------------------------

    async def bring_tab_forward(self) -> None:
        """Bring this run's tab to the front, once per run, at the prefill's start.

        ADR 0007, "Bringing the tab to the front" (decision 1): the only
        ``bring_to_front`` in the package, before the Message click, while the person
        is watching. Nothing later changes focus, :meth:`hand_over` included. A second
        call, or one after the click, raises ``RuntimeError``."""
        if self._fronted or self._message_clicked:
            raise RuntimeError("the tab is brought to the front once, at the prefill's start")
        page = cast(_MessagingPage, await self.ensure_page())
        self._fronted += 1
        await page.bring_to_front()

    async def read_profile_heading(self) -> str | None:
        """The profile page's ``h1`` text when it has exactly one, else ``None``: LinkedIn's
        own name for the profile, read before the click for the never-messaged chip
        check (ADR 0007, "One click on Message"). A read, not an input."""
        page = self._page
        if page is None or page.is_closed() or self._message_clicked:
            return None
        tab = cast(_MessagingPage, page)
        heading = tab.locator(PROFILE_HEADING)
        try:
            if await heading.count() != 1:
                return None
            return await _accessible_name(tab, heading, "heading", level=1)
        except Exception as exc:
            log.info("the profile's heading could not be read (%s)", type(exc).__name__)
            return None

    async def click_message(
        self,
        profile_path: str,
        profile_id: str,
        *,
        pause_s: float,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    ) -> MessageClick:
        """ADR 0007's first input: one click on **Message**, on the contact's profile.

        1. Refuses when the run's tab is gone (:class:`BrowserUnavailable`; it never
           reopens one), when the Message click was already sent this run, and when
           the tab isn't on ``profile_path`` (``/in/<slug>/``).
        2. Waits ``pause_s``, the pause a person takes before reaching for the control.
        3. Finds every control by role and name (a link named exactly "Message"),
           hidden or visible, and refuses unless each one opens this contact's compose
           (:func:`message_control_refusal`, decision 4).
        4. Chooses one visible control (#444, :func:`choose_message_target`): the top
           card's (:data:`MESSAGE_TOP_CARD`) when it's on screen with nothing over its
           click point, else another that is, by the page's own geometry
           (:meth:`_read_click_geometry`). None that is, and no top card to scroll to,
           refuses with no click, and logs why as a fixed category (:class:`NotClear`,
           #470), never the page's text.
        5. Clicks it once, through a locator bound to the contact's verified href (never
           by position alone), at the control's own box, with a person's press length.
           A click that fails isn't tried again; its category
           (:func:`classify_click_failure`) is logged and returned, never its text.

        From the moment the click is sent the run never opens or navigates a tab again
        (:meth:`_ensure_page`), whatever the click's result.
        """
        page = self._page
        if page is None or page.is_closed():
            raise BrowserUnavailable(
                "the run's tab went away before the Message click; aborting rather than reopen it"
            )
        if self._message_clicked:
            return MessageClick(False, False, "the Message control was already clicked")
        if not _on_path(page.url, profile_path):
            return MessageClick(False, False, "the tab is not on the contact's profile")
        await sleep(pause_s)
        if page.is_closed():
            raise BrowserUnavailable("the run's tab went away before the Message click")
        if not _on_path(page.url, profile_path):
            return MessageClick(False, False, "the tab left the profile before the click")
        tab = cast(_MessagingPage, page)
        # Decision 3 before the click (#444): a bubble already on the page, minimized ones
        # included, would make the check after the click refuse anyway, at the cost of a
        # click and a second bubble. The same queries, hidden elements counted.
        try:
            leftover = await self._bubble_already_open(tab)
        except Exception as exc:
            if self._lost(page):
                raise BrowserUnavailable("lost the tab while looking for open bubbles") from exc
            log.warning(
                "prefill: whether a message bubble is open could not be read (%s); nothing"
                " was clicked",
                type(exc).__name__,
            )
            return MessageClick(False, False, BUBBLE_UNREADABLE)
        if leftover:
            log.info("prefill: a message bubble was already on the page; nothing was clicked")
            return MessageClick(False, False, BUBBLE_ALREADY_OPEN)
        try:
            found = await self._find_message_controls(tab, profile_id)
        except Exception as exc:
            if self._lost(page):
                raise BrowserUnavailable("lost the tab while finding Message") from exc
            return MessageClick(False, False, "the Message control could not be read")
        if found.refusal is not None:
            return MessageClick(False, False, found.refusal)
        if not found.candidates:
            log.warning(
                "prefill: no Message control was clear to click (%s); nothing was clicked",
                NotClear.NO_CANDIDATES.value,
            )
            return MessageClick(False, False, "no Message control is visible")
        geometry = await self._read_click_geometry(page, found.boxes, found.verified)
        choice = choose_message_target(found.boxes, found.top_card, geometry)
        if choice is None:
            # A choice of None means the geometry was read (without it the top card, or
            # the first control, is chosen unchecked).
            reason = (
                not_clear_reason(found.boxes, found.top_card, geometry)
                if geometry is not None
                else NotClear.NO_CANDIDATES
            )
            log.warning(
                "prefill: no Message control was clear to click (%s); nothing was clicked",
                reason.value,
            )
            return MessageClick(False, False, MESSAGE_NOT_ON_SCREEN)
        index, chosen = choice
        target = found.candidates[index]
        self._click_target = chosen
        self._message_clicked = True
        self._click_url = page.url
        try:
            await target.click(delay=MESSAGE_PRESS_MS, timeout=MESSAGE_CLICK_TIMEOUT_MS)
        except Exception as exc:
            failure = classify_click_failure(exc)
            self._click_failure = failure
            log.warning(
                "the Message control could not be clicked (%s; %s; chose %s)",
                type(exc).__name__,
                failure.value,
                chosen.value,
            )
            return MessageClick(
                False, True, "the Message control could not be clicked", chosen, failure
            )
        self._message_click_landed = True
        return MessageClick(True, True, target=chosen)

    async def message_cover(self, profile_path: str, profile_id: str) -> NotClear | None:
        """Why :meth:`click_message` would refuse right now for want of a clear control,
        or ``None`` (#470). A read before the click, for the prefill's one scroll back up.

        The same reads as :meth:`click_message`'s, in the same order, with no pause and
        no input: decision 3's bubble check, decision 4's href check, the visible
        controls, and the page's own geometry (:meth:`_read_click_geometry`). ``None``
        whenever :meth:`click_message` wouldn't refuse for this reason: a control is
        clear, the top card is off screen (Playwright scrolls it in), the geometry
        couldn't be read, a bubble is open, a link names someone else, the tab left the
        profile, or anything raised. :meth:`click_message` then reads everything again
        and decides as before; this never clicks, and never stands in for its checks."""
        page = self._page
        if page is None or page.is_closed() or self._message_clicked:
            return None
        if not _on_path(page.url, profile_path):
            return None
        tab = cast(_MessagingPage, page)
        try:
            if await self._bubble_already_open(tab):
                return None
            found = await self._find_message_controls(tab, profile_id)
        except Exception as exc:
            log.info("prefill: the Message controls could not be read (%s)", type(exc).__name__)
            return None
        if found.refusal is not None or not found.candidates:
            return None
        geometry = await self._read_click_geometry(page, found.boxes, found.verified)
        if geometry is None or choose_message_target(found.boxes, found.top_card, geometry):
            return None
        return not_clear_reason(found.boxes, found.top_card, geometry)

    async def _find_message_controls(
        self, tab: _MessagingPage, profile_id: str
    ) -> _MessageControls:
        """The contact's Message controls, as :meth:`click_message` chooses among them.

        Every link named exactly "Message", hidden or visible, must open this contact's
        compose (decision 4, :func:`message_control_refusal`); otherwise ``refusal`` says
        why and there are no candidates. The candidates are the visible ones, each
        through a locator bound to its verified ``href``, the top card's control
        (:data:`MESSAGE_TOP_CARD`) first when there is one. Reads only; it raises what
        the page's reads raise."""
        controls = tab.get_by_role(
            MESSAGE_CONTROL_ROLE, name=MESSAGE_CONTROL_NAME, exact=True, include_hidden=True
        )
        matches = await controls.count()
        hrefs = [
            await controls.nth(index).get_attribute("href", timeout=MESSAGING_READ_TIMEOUT_MS)
            for index in range(matches)
        ]
        refusal = message_control_refusal(hrefs, profile_id)
        candidates: list[_MessagingLocator] = []
        boxes: list[Mapping[str, float] | None] = []
        top_card: int | None = None
        if refusal is None:
            headings = tab.locator(PROFILE_HEADING)
            after_heading = (
                headings.locator(MESSAGE_TOP_CARD) if await headings.count() == 1 else None
            )
            for href in dict.fromkeys(h for h in hrefs if h is not None):
                bound = controls.and_(tab.locator(f"[href={_css_string(href)}]"))
                if top_card is None and after_heading is not None:
                    top = bound.and_(after_heading).filter(visible=True)
                    if await top.count() > 0:
                        top_card = len(candidates)
                        candidates.append(top.first)
                        boxes.append(await _box(top.first))
                visible = bound.filter(visible=True)
                for index in range(await visible.count()):
                    if len(candidates) >= MESSAGE_MAX_CANDIDATES:
                        break
                    box = await _box(visible.nth(index))
                    if top_card is not None and box is not None and box == boxes[top_card]:
                        continue  # the top card's control again, in document order
                    candidates.append(visible.nth(index))
                    boxes.append(box)
        verified = [h for h in dict.fromkeys(hrefs) if h is not None]
        return _MessageControls(refusal, candidates, boxes, top_card, verified)

    async def _bubble_already_open(self, tab: _MessagingPage) -> bool:
        """Whether any message composer or ``Messaging`` dialog is on the page, hidden ones
        counted: decision 3's own queries, read before the click (#444)."""
        composers = tab.get_by_role(
            COMPOSER_ROLE, name=COMPOSER_NAME, exact=True, include_hidden=True
        )
        if await composers.count():
            return True
        dialogs = tab.get_by_role(BUBBLE_ROLE, name=BUBBLE_NAME, exact=True, include_hidden=True)
        return await dialogs.count() > 0

    async def _read_click_geometry(
        self,
        page: PageLike,
        boxes: Sequence[Mapping[str, float] | None],
        hrefs: Sequence[str],
    ) -> ClickGeometry | None:
        """The page's own geometry for the Message click (#444): passive DevTools reads.

        One CDP session on the run's tab, detached before this returns, which sends
        only these read-only methods (ADR 0007's amendment for #444;
        ``tests/test_browser_safety.py`` pins them):

        - ``Page.getLayoutMetrics``: the layout viewport's size, which Playwright doesn't
          know for a tab this run attaches to, and how far the page is scrolled.
        - ``DOM.getDocument`` (depth 0), then ``DOM.querySelectorAll`` for the links
          with each verified compose ``href``, ``DOM.describeNode`` for each one's
          subtree, ``DOM.getBoxModel`` for its box, and ``DOM.getContentQuads`` for
          where Playwright's click would press it (:func:`_click_point`): which elements
          belong to which Message link, and where each link is.
        - ``DOM.getNodeForLocation``: the element the page would hit at a point, as
          ``document.elementFromPoint`` answers it (``pointer-events: none`` skipped),
          at the click point of each control that is wholly on screen. The quads are in
          viewport coordinates and this method takes document ones (Chrome subtracts the
          scroll offset), so the point is sent with the page's scroll added (#470).
          Without it, a scrolled page is hit-tested where the control isn't.

        A hit counts for a control when it is the link or inside it, matched by node, not
        by size: an icon may overflow its link's box. No script runs in the page,
        nothing is input, and nothing the page can see changes. ``None`` when the
        session can't start or a read fails: the click then chooses without geometry
        (:class:`ClickTarget` ``UNCHECKED``).
        """
        context = cast(_TapContext, self._attachment.context)
        try:
            session = cast(_CdpSessionLike, await context.new_cdp_session(page))
        except Exception as exc:
            log.info("prefill: the page's geometry could not be read (%s)", type(exc).__name__)
            return None
        try:
            async with asyncio.timeout(GEOMETRY_TIMEOUT_S):
                metrics = await session.send("Page.getLayoutMetrics")
                layout = metrics["cssLayoutViewport"]
                viewport = (float(layout["clientWidth"]), float(layout["clientHeight"]))
                # #470: how far the page is scrolled, to turn a viewport point (the quads')
                # into the document point DOM.getNodeForLocation takes.
                scroll_x, scroll_y = float(layout["pageX"]), float(layout["pageY"])
                document = await session.send("DOM.getDocument", {"depth": 0})
                root = document["root"]["nodeId"]
                links: list[_Link] = []
                for href in hrefs:
                    found = await session.send(
                        "DOM.querySelectorAll",
                        {"nodeId": root, "selector": f"a[href={_css_string(href)}]"},
                    )
                    for node_id in found["nodeIds"][:MESSAGE_MAX_LINKS]:
                        described = await session.send(
                            "DOM.describeNode", {"nodeId": node_id, "depth": -1}
                        )
                        try:
                            model = await session.send("DOM.getBoxModel", {"nodeId": node_id})
                            quads = await session.send("DOM.getContentQuads", {"nodeId": node_id})
                        except Exception as exc:
                            # A hidden copy has no box, and nothing can hit it.
                            log.debug("prefill: a Message link has no box (%s)", type(exc).__name__)
                            continue
                        links.append(
                            _Link(
                                _quad_box(model["model"]["border"]),
                                _click_point(quads["quads"], viewport),
                                frozenset(_backend_ids(described["node"])),
                            )
                        )
                hits: list[Mapping[str, float] | None] = []
                points: list[tuple[float, float] | None] = []
                for box in boxes:
                    link = next((k for k in links if box is not None and _clear(box, k.box)), None)
                    if box is None or link is None or link.point is None:
                        hits.append(None)
                        points.append(None)
                        continue
                    if not _on_screen(box, viewport):
                        hits.append(None)
                        points.append(None)
                        continue
                    x, y = link.point
                    points.append(link.point)
                    hit = await session.send(
                        "DOM.getNodeForLocation",
                        {
                            "x": round(x + scroll_x),
                            "y": round(y + scroll_y),
                            "ignorePointerEventsNone": True,
                        },
                    )
                    hits.append(link.box if hit["backendNodeId"] in link.owned else None)
                return ClickGeometry(viewport, tuple(hits), tuple(points))
        except Exception as exc:
            log.info("prefill: the page's geometry could not be read (%s)", type(exc).__name__)
            return None
        finally:
            try:
                await session.detach()
            except Exception as exc:
                log.info("prefill: the geometry session could not detach (%s)", type(exc).__name__)

    async def type_into_composer(
        self,
        plan: TypingPlan,
        recipient: BubbleRecipient,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
        cancelled: Callable[[], Awaitable[bool]] | None = None,
        another_compose: Callable[[], bool] | None = None,
    ) -> TypingResult:
        """ADR 0007's second input: type ``plan`` into the one verified composer, then stop.

        Before the first key it refuses (``not_typed``, zero keys) a plan with a control
        character in any chunk, and a page where any check fails: exactly one composer
        (role and name, hidden ones counted), the bubble the compose option named and
        its recipient (:meth:`_bubble_refusal`), focus in that composer, and an empty
        composer.

        Each step is then **delay, check, key**, with nothing in between: wait the
        step's delay, run every check again (the composer's text must equal what was
        typed so far, the tab's url must be the one the click left it on), and send one
        key call. A newline step is ``press("Shift+Enter")``; one printable ASCII
        character other than a space is ``type``; anything else, spaces included, is
        ``insert_text``. Enter, NumpadEnter and any modifier+Enter are never pressed,
        and no modifier is held. ``cancelled`` is asked before each step's delay.

        The first key call *attempted* is the point of no return: ``started_at`` is
        taken just before it. After it, a failed check or a cancel ends
        ``partially_typed``, and a lost tab or a key call that raised ends ``unknown``.
        At the end the composer's text must be the whole body, or the result is
        ``partially_typed``. Nothing here reopens, navigates, or clears anything.
        """
        page = self._page
        if page is None or page.is_closed() or not self._message_clicked:
            return TypingResult(
                TypingEnd.NOT_TYPED, "no tab with a clicked Message control", 0, None
            )
        if plan_control_characters(plan):
            return TypingResult(TypingEnd.NOT_TYPED, "the plan holds a control character", 0, None)
        if not plan:
            return TypingResult(TypingEnd.NOT_TYPED, "the plan is empty", 0, None)
        tab = cast(_MessagingPage, page)
        composer = tab.get_by_role(
            COMPOSER_ROLE, name=COMPOSER_NAME, exact=True, include_hidden=True
        )
        refusal = await self._await_bubble(
            tab,
            composer,
            recipient,
            sleep,
            another_compose,
            # Decision 5, option B: the seam's one call site.
            focus_seam=self._focus_seam if FOCUS_INPUT_AUTHORIZED else None,
        )
        if refusal is not None:
            return TypingResult(TypingEnd.NOT_TYPED, refusal, 0, None)
        keyboard = tab.keyboard
        started_at: datetime | None = None
        so_far = ""
        for index, step in enumerate(plan):
            stopped = TypingEnd.NOT_TYPED if index == 0 else TypingEnd.PARTIALLY_TYPED
            await sleep(step.delay_before_s)
            # The step's checks: the cancel, then the page. Nothing else is awaited
            # between them and the key.
            if cancelled is not None and await cancelled():
                return TypingResult(stopped, "cancelled", len(so_far), started_at)
            refusal = await self._composer_refusal(
                tab, composer, recipient, so_far, another_compose=another_compose
            )
            if refusal is not None:
                if index > 0 and self._lost(page):
                    return TypingResult(TypingEnd.UNKNOWN, refusal, len(so_far), started_at)
                return TypingResult(stopped, refusal, len(so_far), started_at)
            if started_at is None:
                started_at = clock()
            self._keys_sent += 1
            try:
                if step.newline:
                    await keyboard.press("Shift+Enter")
                elif _keyed(step.chunk):
                    await keyboard.type(step.chunk)
                else:
                    await keyboard.insert_text(step.chunk)
            except Exception as exc:
                log.warning("a key call failed while typing (%s)", type(exc).__name__)
                return TypingResult(
                    TypingEnd.UNKNOWN,
                    "a key call failed",
                    len(so_far) + max(len(step.chunk), 1),
                    started_at,
                )
            so_far += "\n" if step.newline else step.chunk
        body = so_far
        final = await self._composer_refusal(tab, composer, recipient, body)
        if final is not None:
            end = TypingEnd.UNKNOWN if self._lost(page) else TypingEnd.PARTIALLY_TYPED
            return TypingResult(end, f"after typing: {final}", len(body), started_at)
        self._typed_whole = True
        return TypingResult(TypingEnd.TYPED, "typed", len(body), started_at)

    async def click_send(
        self,
        body: str,
        recipient: BubbleRecipient,
        *,
        permit: SendPermit,
        dwell_s: float,
        clock: Callable[[], datetime],
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
        another_compose: Callable[[], bool] | None = None,
    ) -> SendClick:
        """ADR 0008's one input: one click on **Send**, for auto-send alone.

        Reached only from :meth:`PagePrefill.prefill
        <netkeeper.linkedin.page_messaging.PagePrefill.prefill>`, for an ``auto_send``
        spec, with the :class:`~netkeeper.linkedin.messaging.SendPermit` only the runner
        builds, after :meth:`type_into_composer` typed the whole body. In order, and
        nothing is clicked when any step refuses:

        1. Refuses without a permit, without a landed Message click and a whole body
           typed this run, and once a Send click was already sent: never twice.
        2. Waits ``dwell_s``, the pause a person takes to read what they wrote.
        3. Asks the permit's ``recheck`` (the flag, arming, a pause, active hours, the
           session flag, heat, the auto-send hold, a cancel) again.
        4. Checks the composer again as before every key (ADR 0007), focus aside: one
           composer on the page, the bubble and its recipient, the tab's url, no later
           compose option, and the text exactly ``body`` (what typing verified).
        5. Finds Send: exactly one button named exactly "Send" on the page, hidden
           ones counted; it sits in the verified composer's own form, carries
           ``type="submit"``, and is visible and enabled.
        6. Checks the composer once more, every check of step 4 and, last, focus.
        7. Clicks once, at the control's own box, with nothing awaited between the
           focus read and the click. A click that fails is never tried again.
        """
        page = self._page
        # Checked at run time too: the type alone binds nothing a caller can't ignore.
        if not isinstance(cast(object, permit), SendPermit):
            return SendClick(False, False, "no send permit")
        if self._send_attempted:
            return SendClick(False, False, "Send was already clicked")
        if page is None or page.is_closed() or not self._message_click_landed:
            return SendClick(False, False, "no tab with a clicked Message control", True)
        if not self._typed_whole or not body:
            return SendClick(False, False, "the whole body was not typed", True)
        await sleep(dwell_s)
        try:
            gate = await permit.recheck()
        except Exception as exc:
            log.warning("a send gate could not be checked (%s)", type(exc).__name__)
            return SendClick(False, False, "the send gates could not be checked")
        if gate is not None:
            return SendClick(False, False, gate)
        tab = cast(_MessagingPage, page)
        composer = tab.get_by_role(
            COMPOSER_ROLE, name=COMPOSER_NAME, exact=True, include_hidden=True
        )
        # The composer first, so a page that changed under it reads as the composer's
        # change (``partially_typed``), not as an odd Send control.
        changed = await self._composer_refusal(
            tab, composer, recipient, body, focus=False, another_compose=another_compose
        )
        if changed is not None:
            return SendClick(False, False, changed, True)
        send = self._send_control(tab, composer)
        try:
            refusal = await self._send_refusal(send)
        except Exception as exc:
            log.info("the Send control could not be read (%s)", type(exc).__name__)
            return SendClick(False, False, "the Send control could not be read")
        if refusal is not None:
            return SendClick(False, False, refusal)
        # Last, the composer as before every key: focus is the last read before the click.
        changed = await self._composer_refusal(
            tab, composer, recipient, body, another_compose=another_compose
        )
        if changed is not None:
            return SendClick(False, False, changed, True)
        clicked_at = clock()
        self._send_attempted = True
        try:
            await send.control.click(delay=SEND_PRESS_MS, timeout=SEND_CLICK_TIMEOUT_MS)
        except Exception as exc:
            log.warning("the Send control could not be clicked (%s)", type(exc).__name__)
            return SendClick(
                False, True, "the Send control could not be clicked", clicked_at=clicked_at
            )
        self._send_landed = True
        return SendClick(True, True, clicked_at=clicked_at)

    async def close_sent_bubble(
        self,
        recipient: BubbleRecipient,
        *,
        confirmed: bool,
        sleep: Callable[[float], Awaitable[None]] = _real_sleep,
    ) -> BubbleClose:
        """ADR 0008, decision D1: close the bubble a landed, proven Send left, by its own
        close control, once. Reached only from ``PagePrefill.prefill``.

        Refuses, clicking nothing, unless all of these hold:

        - the Send click landed this run, and nothing was closed yet;
        - ``confirmed``: the caller holds the page's own ``createMessage`` answer
          proving the message went;
        - the composer reads empty within :data:`CLOSE_WAIT_S`, and again right before
          the click (LinkedIn clears it once the message went);
        - exactly one ``Messaging`` dialog is on the page, hidden ones counted, holding
          the composer, whose header ``h2`` holds exactly one link, to
          ``/in/<the contact's profile id>/``;
        - exactly one button in that dialog is named exactly
          ``Close your conversation with <that link's name>``, and it is visible.

        Then one click, never retried; closed only when the composer then leaves the
        page."""
        page = self._page
        if page is None or page.is_closed() or not self._send_landed:
            return BubbleClose(False, False, "Send did not land")
        if confirmed is not True:
            return BubbleClose(False, False, "the send was not confirmed")
        if self._close_attempted:
            return BubbleClose(False, False, "the bubble's close control was already clicked")
        tab = cast(_MessagingPage, page)
        composer = tab.get_by_role(
            COMPOSER_ROLE, name=COMPOSER_NAME, exact=True, include_hidden=True
        )
        try:
            emptied = False
            for _ in range(CLOSE_WAIT_POLLS):
                if _one_composer(await composer.count()) and await _read_composer(composer) == "":
                    emptied = True
                    break
                await sleep(CLOSE_WAIT_S / CLOSE_WAIT_POLLS)
            if not emptied:
                return BubbleClose(False, False, "the composer did not empty after Send")
            control = await self._close_control(tab, composer, recipient)
        except Exception as exc:
            log.info("the sent bubble could not be read (%s)", type(exc).__name__)
            return BubbleClose(False, False, "the sent bubble could not be read")
        if isinstance(control, str):
            return BubbleClose(False, False, control)
        try:
            # Last, the composer again: text back in it means the send didn't hold.
            if await _read_composer(composer) != "":
                return BubbleClose(False, False, "the composer did not empty after Send")
        except Exception as exc:
            log.info("the sent bubble could not be read (%s)", type(exc).__name__)
            return BubbleClose(False, False, "the sent bubble could not be read")
        self._close_attempted = True
        try:
            await control.click(delay=SEND_PRESS_MS, timeout=CLOSE_CLICK_TIMEOUT_MS)
        except Exception as exc:
            log.warning("the bubble's close control could not be clicked (%s)", type(exc).__name__)
            return BubbleClose(False, True, "the bubble's close control could not be clicked")
        try:
            for _ in range(CLOSE_WAIT_POLLS):
                if await composer.count() == 0:
                    self._bubble_closed = True
                    return BubbleClose(True, True)
                await sleep(CLOSE_WAIT_S / CLOSE_WAIT_POLLS)
        except Exception as exc:
            log.info("the closed bubble could not be read (%s)", type(exc).__name__)
        return BubbleClose(False, True, "the bubble did not close")

    async def _close_control(
        self, tab: _MessagingPage, composer: _MessagingLocator, recipient: BubbleRecipient
    ) -> _MessagingLocator | str:
        """The sent bubble's one close control, or why there isn't one. Reads only."""
        dialogs = tab.get_by_role(BUBBLE_ROLE, name=BUBBLE_NAME, exact=True, include_hidden=True)
        if await dialogs.count() != 1 or await dialogs.filter(has=composer).count() != 1:
            return "the sent message is not in one conversation bubble"
        links = dialogs.locator(BUBBLE_HEADER_LINK)
        if await links.count() != 1:
            return "the sent bubble's header does not name one person"
        href = await links.get_attribute("href", timeout=MESSAGING_READ_TIMEOUT_MS)
        if not _is_profile_link(
            href, f"{PROFILE_PATH_PREFIX}{recipient.profile_id}/", fold_case=False
        ):
            return "the sent bubble is for someone else"
        name = await _accessible_name(tab, links, "link")
        if name is None:
            return "the sent bubble's name could not be read"
        closes = dialogs.get_by_role(
            CLOSE_CONTROL_ROLE,
            name=f"{CLOSE_CONTROL_PREFIX}{name}",
            exact=True,
            include_hidden=True,
        )
        if await closes.count() != 1:
            return "the sent bubble does not have one close control for this person"
        visible = closes.filter(visible=True)
        if await visible.count() != 1:
            return "the sent bubble's close control is not visible"
        return visible

    async def close_sent_tab(self) -> None:
        """ADR 0008, decision D3: close this run's tab once its sent message's bubble
        closed. Reached only from ``page_messaging.py``, and only after
        :meth:`close_sent_bubble` closed the bubble; anything else hands the tab over
        instead (:meth:`hand_over`). Detaches either way; the context and the browser
        are left as they were."""
        if not self._bubble_closed:
            raise RuntimeError("the tab is closed only after its sent bubble closed")
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
                self._tab_closed = True
            except Exception as exc:
                log.info("could not close the sent message's tab (%s)", type(exc).__name__)
        await _detach_quietly(self._attachment.detach)

    def _send_control(self, tab: _MessagingPage, composer: _MessagingLocator) -> _SendControl:
        """The locators for the verified composer's Send control (ADR 0008). Builds only:
        the one place a locator names Send (``tests/test_browser_safety.py``)."""
        forms = composer.locator(COMPOSER_FORM)
        sends = tab.get_by_role(
            SEND_CONTROL_ROLE, name=SEND_CONTROL_NAME, exact=True, include_hidden=True
        )
        in_form = forms.get_by_role(
            SEND_CONTROL_ROLE, name=SEND_CONTROL_NAME, exact=True, include_hidden=True
        )
        return _SendControl(forms, sends, sends.and_(in_form))

    @staticmethod
    async def _send_refusal(send: _SendControl) -> str | None:
        """Why ``send`` isn't the one Send control to click, or ``None``. Reads only."""
        if await send.forms.count() != 1:
            return "the composer is not in one message form"
        if await send.on_page.count() != 1:
            return "the page does not show exactly one Send control"
        control = send.control
        if await control.count() != 1:
            return "the Send control is not in the composer's form"
        kind = await control.get_attribute("type", timeout=MESSAGING_READ_TIMEOUT_MS)
        if kind != SEND_CONTROL_TYPE:
            return "the Send control is not the form's submit button"
        if await control.filter(visible=True).count() != 1:
            return "the Send control is not visible"
        if not await control.is_enabled(timeout=MESSAGING_READ_TIMEOUT_MS):
            return "the Send control is disabled"
        return None

    async def _composer_refusal(
        self,
        tab: _MessagingPage,
        composer: _MessagingLocator,
        recipient: BubbleRecipient,
        expected: str,
        *,
        focus: bool = True,
        another_compose: Callable[[], bool] | None = None,
    ) -> str | None:
        """Every check ADR 0007 makes before a key, as one fixed phrase, or ``None``.

        The tab and the browser are still there and on the url the click left; the
        bubble and its recipient hold (:meth:`_bubble_refusal`); the composer's text is
        ``expected``; and, last, the composer holds focus. A read that raises is a
        refusal."""
        page = cast(PageLike, tab)
        if another_compose is not None and another_compose():
            return ANOTHER_COMPOSE
        if self._lost(page):
            return "the tab or the browser went away"
        if page.url != self._click_url:
            return "the tab's url changed"
        try:
            bubble = await self._bubble_refusal(tab, composer, recipient)
            if bubble is not None:
                return bubble
            text = await _read_composer(composer)
            if not composer_text_matches(text, expected):
                return (
                    "the composer is not empty" if not expected else "the composer's text changed"
                )
            # Again after the page reads, so a compose option that arrived during them
            # refuses this pass (it isn't a page read: focus stays the last one).
            if another_compose is not None and another_compose():
                return ANOTHER_COMPOSE
            # Last, so the window between this read and the key is as short as it can be.
            if focus and await composer.and_(tab.locator(FOCUSED)).count() != 1:
                return "the composer does not hold focus"
        except Exception as exc:
            log.info("a composer check could not read the page (%s)", type(exc).__name__)
            return "the composer could not be read"
        return None

    async def _await_bubble(
        self,
        tab: _MessagingPage,
        composer: _MessagingLocator,
        recipient: BubbleRecipient,
        sleep: Callable[[float], Awaitable[None]],
        another_compose: Callable[[], bool] | None,
        *,
        focus_seam: Callable[[_MessagingPage, _MessagingLocator], Awaitable[None]] | None,
    ) -> str | None:
        """The composer wait (ADR 0007): up to :data:`COMPOSER_WAIT_S`, poll the full
        read-only check, focus included, until every check passes in one pass, and
        answer that pass (``None``) or the last refusal.

        The bubble, and focus in its composer, can land a moment after the compose
        option. Under option B of decision 5 (``focus_seam`` given, the default since
        the maintainer chose it) it first polls until one pass holds every check but
        focus, then calls ``focus_seam`` once, its one input, then polls the full pass,
        focus included, for the rest of the wait. Under option A (``focus_seam`` None) it
        polls the full pass from the start and gives no input. A lost tab ends the wait at once."""
        refusal: str | None = "the bubble was not drawn"
        # Option B only: first a pass of every check but focus, then the seam, once.
        seam_pending = focus_seam is not None
        for _ in range(COMPOSER_WAIT_POLLS):
            if seam_pending:
                refusal = await self._composer_refusal(
                    tab, composer, recipient, "", focus=False, another_compose=another_compose
                )
                if refusal is None and focus_seam is not None:
                    seam_pending = False
                    await focus_seam(tab, composer)
            if not seam_pending:
                # The authorizing pass: every check, focus last, in one pass.
                refusal = await self._composer_refusal(
                    tab, composer, recipient, "", another_compose=another_compose
                )
                if refusal is None:
                    return None
            if self._lost(cast(PageLike, tab)):
                return refusal
            await sleep(COMPOSER_WAIT_S / COMPOSER_WAIT_POLLS)
        return refusal

    async def _focus_seam(self, tab: _MessagingPage, composer: _MessagingLocator) -> None:
        """ADR 0007's third input (decision 5, option B): one ``Locator.focus()`` on the
        verified composer, never a click.

        Reached only from the composer wait, after a pass in which every check but focus
        held, and at most once per run (a second call raises ``RuntimeError``). When the
        composer already holds focus it does nothing. A ``focus()`` that fails is not
        retried: the full pass that follows refuses a composer without focus."""
        if self._focus_used:
            raise RuntimeError("the composer is focused at most once per run")
        self._focus_used = True
        if await composer.and_(tab.locator(FOCUSED)).count() == 1:
            return
        try:
            await composer.focus(timeout=FOCUS_TIMEOUT_MS)
        except Exception as exc:
            log.warning("the composer could not be focused (%s)", type(exc).__name__)

    async def _bubble_refusal(
        self, tab: _MessagingPage, composer: _MessagingLocator, recipient: BubbleRecipient
    ) -> str | None:
        """The bubble the compose option named, for this contact (ADR 0007, "The recipient", 3).

        Page-wide: exactly one composer, hidden ones counted (decision 3:
        :func:`_one_composer`), and at most one ``Messaging`` dialog.

        - Existing conversation: exactly one ``Messaging`` dialog, holding the composer,
          no ``New message`` heading and no recipient field anywhere, and the dialog's
          header ``h2`` holds exactly one link, to ``/in/<profile id>/``.
        - Never messaged: exactly one ``New message`` heading; in the innermost element
          holding both it and the composer, exactly one chip (a button named
          ``Remove …``), one recipient field, and one ``/in/`` link, to the contact's
          slug. A dialog, if there is one, holds that heading. Without a slug, refused.
        """
        composers = await composer.count()
        if not _one_composer(composers):
            if composers == 0:
                return "no message composer is on the page"
            # Hidden ones count: a minimized bubble keeps its composer (#444).
            return (
                "another message composer is on the page, in an open or minimized bubble;"
                " close the other bubbles"
            )
        dialogs = tab.get_by_role(BUBBLE_ROLE, name=BUBBLE_NAME, exact=True, include_hidden=True)
        dialog_count = await dialogs.count()
        if dialog_count > 1:
            return "another message bubble is on the page, open or minimized; close the others"
        heading = tab.get_by_role(
            NEW_MESSAGE_ROLE, name=NEW_MESSAGE_NAME, exact=True, include_hidden=True
        )
        field = tab.get_by_role(
            RECIPIENTS_FIELD_ROLE, name=RECIPIENTS_FIELD_NAME, exact=True, include_hidden=True
        )
        if recipient.layout is BubbleLayout.EXISTING:
            if dialog_count != 1:
                return "the conversation's bubble is not open"
            if await heading.count() or await field.count():
                return "the page shows a new-message bubble, not the conversation"
            if await dialogs.filter(has=composer).count() != 1:
                return "the composer is not in the conversation's bubble"
            links = dialogs.locator(BUBBLE_HEADER_LINK)
            if await links.count() != 1:
                return "the bubble's header does not name one person"
            href = await links.get_attribute("href", timeout=MESSAGING_READ_TIMEOUT_MS)
            if not _is_profile_link(
                href, f"{PROFILE_PATH_PREFIX}{recipient.profile_id}/", fold_case=False
            ):
                return "the bubble is for someone else"
            return None
        if recipient.public_id is None:
            return "the contact has no public profile id to check the new-message bubble by"
        if await heading.count() != 1:
            return "the new-message bubble is not open, or more than one is"
        if dialog_count == 1 and await dialogs.filter(has=heading).count() != 1:
            return "the page shows the conversation's bubble, not a new message"
        scope = composer.locator(NEW_MESSAGE_SCOPE)
        # Belt and braces: the XPath already found an ancestor holding the heading; the
        # filter checks it holds the role-and-name heading counted above, too.
        if await scope.count() != 1 or await scope.filter(has=heading).count() != 1:
            return "the composer is not in the new-message bubble"
        chips = scope.get_by_role(CHIP_ROLE, name=CHIP_NAME, include_hidden=True)
        if await chips.count() != 1:
            return "the new-message bubble does not name exactly one recipient"
        if recipient.profile_name is not None:
            chip = await _accessible_name(tab, chips, CHIP_ROLE)
            if chip is None:
                # A name given through aria-labelledby can't be read without script.
                labelled = await chips.get_attribute(
                    "aria-labelledby", timeout=MESSAGING_READ_TIMEOUT_MS
                )
                return RECIPIENT_NAME_UNREADABLE if labelled else RECIPIENT_NAME_MISMATCH
            if chip != _name(f"{CHIP_NAME_PREFIX}{recipient.profile_name}"):
                return RECIPIENT_NAME_MISMATCH
        if (
            await scope.get_by_role(
                RECIPIENTS_FIELD_ROLE, name=RECIPIENTS_FIELD_NAME, exact=True, include_hidden=True
            ).count()
            != 1
        ):
            return "the new-message bubble's recipient field is missing"
        links = scope.get_by_role("link", include_hidden=True)
        hrefs = [
            await links.nth(index).get_attribute("href", timeout=MESSAGING_READ_TIMEOUT_MS)
            for index in range(await links.count())
        ]
        cards = [href for href in hrefs if _profile_path(href)]
        if len(cards) != 1 or not _is_profile_link(
            cards[0], f"{PROFILE_PATH_PREFIX}{recipient.public_id}/", fold_case=True
        ):
            return "the new-message bubble is for someone else"
        return None

    async def hand_over(self) -> None:
        """End the run by giving the tab to the person (ADR 0007, "Handing the tab over").

        Drops the run's reference to its tab and detaches without closing it, so the
        provider's later :meth:`close` closes nothing. It changes no focus. From then on
        the tab isn't netkeeper's: nothing reuses, navigates, or closes it. Reached only
        from :mod:`netkeeper.linkedin.page_messaging`. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._handed_over = True
        observations, self._observations = self._observations, []
        for observation in observations:
            await observation.close()
        self._page = None
        await _detach_quietly(self._attachment.detach)

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
        if self._message_clicked:
            # ADR 0007: an attempted Message click may have opened a bubble; the tab is
            # the person's now, whatever ended the run. It is left open, never closed.
            page = None
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
        if self._message_clicked:
            # ADR 0007, "No reattach after the click": a reopened or navigated tab has
            # no verified composer, and keys could land somewhere nobody checked.
            raise BrowserUnavailable(
                "the Message click was sent; this run never opens or navigates a tab again"
            )
        page = self._page
        if page is not None and not page.is_closed():
            return page
        if page is not None:
            log.warning("the run's tab was closed; reopening it in the same context")
        try:
            page = await self._open_tab()
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
            return await self._open_tab()
        except Exception as exc:
            raise BrowserUnavailable(
                "reattached to Chrome but still cannot open a tab; aborting the run"
            ) from exc

    async def _open_tab(self) -> PageLike:
        """Open this run's tab behind the tab you're on, without activating Chrome (#195).

        ``context.new_page()`` sends ``Target.createTarget`` without ``background``,
        which opens a foreground tab, and on macOS Chrome then activates its window and
        takes keyboard focus from the app you're using. This sends it with
        ``background: true`` instead, on a browser-level session, and never sends
        ``Page.bringToFront`` or ``Target.activateTarget``. Navigating the tab later
        doesn't activate it either. The tab still renders, scrolls, and paginates:
        Playwright turns on focus emulation for every tab it attaches to, which keeps
        a background tab visible to its page.

        The tab is the run's because its target id matches the one Chrome answered,
        read from each new tab's own ``Target.getTargetInfo``
        (:meth:`_target_id`), never because it's the newest. A tab you open at the
        same moment is left alone.

        When a background tab can't be opened while the browser is still there (a
        browser without the session, or one that never reports the tab), the run
        opens its tab the way it used to, with ``new_page()``, logs it, and sets
        :attr:`opened_in_front` so the run's notes say so: in front, but working. A tab
        this created and couldn't identify is closed by its target id
        (:meth:`_close_unclaimed_tab`), on a cancel too. A browser that went away
        raises, for :meth:`_ensure_page`'s reattach.

        Only a prefill or an auto-send brings its tab forward, once, at its start
        (:meth:`bring_tab_forward`). Once it has, a tab it reopens opens in front with
        ``new_page()``, as before #195, so the Message click, the typing, and the Send
        never happen in a tab behind the one you see (ADR 0007, "while the person is
        watching").
        """
        context = self._attachment.context
        if self._fronted:
            return await context.new_page()
        browser = self._attachment.browser
        before = {id(page) for page in context.pages}
        session: _CdpSessionLike | None = None
        target_id: str | None = None
        try:
            session = cast(
                _CdpSessionLike,
                await cast(_TargetBrowser, browser).new_browser_cdp_session(),
            )
            created = await session.send(
                "Target.createTarget", {"url": "about:blank", "background": True}
            )
            target_id = str(created["targetId"])
            async with asyncio.timeout(BACKGROUND_TAB_WAIT_S):
                while True:
                    for page in context.pages:
                        if id(page) in before or page.is_closed():
                            continue
                        found = await self._target_id(page)
                        if found is None:
                            continue  # not attached yet: asked again on the next pass
                        before.add(id(page))
                        if found == target_id:
                            return page
                    await asyncio.sleep(BACKGROUND_TAB_POLL_S)
        except asyncio.CancelledError:
            if session is not None and target_id is not None:
                await asyncio.shield(self._close_unclaimed_tab(session, target_id))
            raise
        except Exception as exc:
            if not browser.is_connected():
                raise
            if session is not None and target_id is not None:
                await self._close_unclaimed_tab(session, target_id)
            log.warning(
                "could not open the run's tab in the background (%s); opening it in front",
                type(exc).__name__,
            )
        finally:
            if session is not None:
                await _detach_quietly(session.detach)
        page = await context.new_page()
        self._opened_in_front = True
        return page

    @staticmethod
    async def _close_unclaimed_tab(session: _CdpSessionLike, target_id: str) -> None:
        """Close the tab :meth:`_open_tab` created and never claimed, by its own target
        id and nothing else (#195). Logged, never raised."""
        try:
            await session.send("Target.closeTarget", {"targetId": target_id})
        except Exception as exc:
            log.warning("could not close the background tab the run opened: %s", exc)

    async def _target_id(self, page: PageLike) -> str | None:
        """A tab's own CDP target id, or ``None`` when it can't be read (#195).

        One page-level session, detached before this returns, that sends only
        ``Target.getTargetInfo`` with no params: the session's own target. It reads;
        the page sees nothing."""
        context = cast(_TapContext, self._attachment.context)
        try:
            session = cast(_CdpSessionLike, await context.new_cdp_session(page))
        except Exception as exc:
            log.debug("could not read a new tab's target id: %s", exc)
            return None
        try:
            info = await session.send("Target.getTargetInfo")
            return str(info["targetInfo"]["targetId"])
        except Exception as exc:
            log.debug("could not read a new tab's target id: %s", exc)
            return None
        finally:
            await _detach_quietly(session.detach)


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


async def _first_visible_box(page: PageLike, selector: str) -> Mapping[str, float] | None:
    """The box of the first match of ``selector`` that is on screen, or ``None`` (#439).

    Passive geometry reads only (``bounding_box``, ``count``), like
    :func:`_content_box`: no script runs in the page and nothing is input. A match
    is skipped when it has no box (``display: none``, detached), a box with no area,
    or a center above :data:`REST_MIN_Y_PX`, left of the screen, or past the tab's known
    viewport height or width (scrolled or positioned out of view). The first match may
    still be drawing, so it gets the full :data:`CONTENT_BOX_TIMEOUT_MS`; later ones get
    :data:`REST_CANDIDATE_TIMEOUT_MS`.
    At most :data:`REST_MAX_CANDIDATES` matches are read. ``None`` on any error.
    """
    matches = cast(_ScrollablePage, page).locator(selector)
    height = _known_viewport_height(page)
    width = _known_viewport_width(page)

    def usable(box: Mapping[str, float] | None) -> bool:
        if box is None or box["width"] <= 0 or box["height"] <= 0:
            return False
        center_x = box["x"] + box["width"] / 2
        center_y = box["y"] + box["height"] / 2
        return (
            center_y >= REST_MIN_Y_PX
            and (height is None or center_y <= height)
            and center_x >= 0
            and (width is None or center_x <= width)
        )

    try:
        box = await matches.first.bounding_box(timeout=CONTENT_BOX_TIMEOUT_MS)
        if usable(box):
            return box
        total = min(await matches.count(), REST_MAX_CANDIDATES)
        for index in range(1, total):
            box = await matches.nth(index).bounding_box(timeout=REST_CANDIDATE_TIMEOUT_MS)
            if usable(box):
                return box
    except Exception as exc:
        log.debug("could not read a box for %r to rest the pointer over: %s", selector, exc)
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


def _known_viewport_width(page: PageLike) -> float | None:
    """This tab's real viewport width, only when Playwright knows it (see
    :func:`_known_viewport_height`)."""
    size = cast(_ScrollablePage, page).viewport_size
    return float(size["width"]) if size is not None else None


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
