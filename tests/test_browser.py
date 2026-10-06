"""The attach path: one context, one tab, one reattach, one lock (spec 9.1, 9.9).

Everything here runs offline against the fakes in ``tests/browser_fakes.py``. The
smoke suite in ``tests/smoke/`` drives the same code against a real Chrome, and
``tests/test_browser_safety.py`` is what keeps a launch out of the package.
"""

from __future__ import annotations

import asyncio
import random
from typing import cast

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage

from netkeeper.linkedin import browser
from netkeeper.linkedin.browser import (
    ATTACH,
    SINGLE_ACCOUNT_KEY,
    ActivityLocks,
    AttachBrowserProvider,
    BrowserBusy,
    BrowserProvider,
    BrowserRun,
    BrowserUnavailable,
)
from netkeeper.linkedin.pacing import RestPlan, RestStep, ScrollPlan, ScrollStep

CDP_URL = "http://127.0.0.1:9222"
LOCAL_PAGE = "http://127.0.0.1:8123/replica/profile.html"
OTHER_PAGE = "http://127.0.0.1:8123/replica/other.html"
BUSY_TIMEOUT_S = 1.0


def make_provider(
    connector: FakeConnector | None = None, locks: ActivityLocks | None = None
) -> AttachBrowserProvider:
    return AttachBrowserProvider(
        CDP_URL, connector=connector or FakeConnector(), locks=locks or ActivityLocks()
    )


def only_page(context: FakeContext) -> FakePage:
    assert len(context.pages) == 1, f"the run opened {len(context.pages)} tabs"
    return context.pages[0]


# --- attaching ---------------------------------------------------------------


async def test_attach_reuses_the_context_that_is_already_open() -> None:
    """Spec 9.1: ``contexts[0]``, so LinkedIn keeps seeing the profile it knows."""
    first, second = FakeContext(), FakeContext()
    connector = FakeConnector([FakeBrowser([first, second])])
    provider = make_provider(connector)

    async with provider.run() as run:
        assert run.context is first
    assert connector.connect_calls == [CDP_URL]


async def test_attach_without_a_context_gives_up_rather_than_making_one() -> None:
    connector = FakeConnector([FakeBrowser([])])
    provider = make_provider(connector)

    with pytest.raises(BrowserUnavailable, match="no open browser context"):
        async with provider.run():
            pass
    assert connector.detaches == 1, "a failed attach still lets go of the connection"


async def test_a_browser_that_is_not_there_is_browser_unavailable() -> None:
    """There is no fallback: the run fails and the scheduler tries later (ADR 0002)."""
    connector = FakeConnector(error=OSError("connection refused"))
    provider = make_provider(connector)

    with pytest.raises(BrowserUnavailable, match="cannot attach to Chrome"):
        async with provider.run():
            pass


async def test_the_provider_can_only_attach() -> None:
    """The interface ADR 0002 keeps open has exactly one member."""
    provider: BrowserProvider = make_provider()

    assert provider.mode == ATTACH
    for verb in ("launch", "launch_persistent_context", "start", "spawn"):
        assert not hasattr(provider, verb), f"a provider with {verb}() is a second identity"
    # Connecting without the activity lock is not something a caller can ask for:
    # run() is the only public way to a browser (#158 review).
    assert not hasattr(provider, "attach"), "a public attach() is a route around the lock"


# --- the run's tab -----------------------------------------------------------


async def test_a_run_opens_one_tab_and_closes_only_that_tab() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        first = await run.ensure_page()
        again = await run.ensure_page()
        await run.goto(LOCAL_PAGE)
        assert again is first

    page = only_page(context)
    assert page.is_closed()
    assert context.new_page_calls == 1
    assert connector.detaches == 1, "the run lets go of the connection when it ends"


async def test_the_tab_closes_and_the_connection_detaches_even_when_the_run_raises() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    with pytest.raises(ValueError, match="the job failed"):
        async with provider.run() as run:
            await run.ensure_page()
            raise ValueError("the job failed")

    assert only_page(context).is_closed()
    assert connector.detaches == 1


async def test_a_tab_the_user_closed_is_reopened_at_the_last_url() -> None:
    """Spec 9.9's ``_ensure_page``: reopen in the same context, where the run was.

    The in-page fetch helper reads from the tab it navigated, so a run that lost its
    tab has to be put back on the same page before it can carry on.
    """
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        await run.goto(LOCAL_PAGE)
        lost = only_page(context)
        lost.user_closed_it()

        recovered = await run.ensure_page()

        assert recovered is not lost, "the closed tab was handed back"
        assert not recovered.is_closed()
        assert context.new_page_calls == 2
        assert recovered.url == LOCAL_PAGE, "the run did not get back to where it was"
        assert connector.attaches == 1, "a closed tab does not cost a reattach"


async def test_reopening_for_a_navigation_does_not_visit_the_old_page_twice() -> None:
    """A restore before a navigation would spend a profile visit nobody asked for."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        await run.goto(LOCAL_PAGE)
        only_page(context).user_closed_it()

        await run.goto(OTHER_PAGE)

    reopened = context.pages[1]
    assert reopened.goto_calls == [OTHER_PAGE]


async def test_a_finished_run_has_no_tab_left_to_hand_out() -> None:
    provider = make_provider()
    async with provider.run() as run:
        await run.ensure_page()

    with pytest.raises(BrowserUnavailable, match="this run is over"):
        await run.ensure_page()


# --- losing the browser ------------------------------------------------------


async def test_losing_the_browser_costs_one_reattach() -> None:
    gone = FakeContext(new_page_error=RuntimeError("browser has been closed"))
    healthy = FakeContext()
    connector = FakeConnector([FakeBrowser([gone]), FakeBrowser([healthy])])
    provider = make_provider(connector)

    async with provider.run() as run:
        page = await run.ensure_page()

        assert run.reattached
        assert connector.attaches == 2
        assert page in healthy.pages
        assert run.context is healthy


async def test_the_second_lost_browser_ends_the_run() -> None:
    """One reattach per run, then :class:`BrowserUnavailable` (spec 9.9)."""
    lost = RuntimeError("Target page, context or browser has been closed")
    first = FakeContext(new_page_error=lost)
    second = FakeContext()
    connector = FakeConnector([FakeBrowser([first]), FakeBrowser([second])])
    provider = make_provider(connector)

    async with provider.run() as run:
        page = await run.ensure_page()  # spends the reattach on the first loss
        assert run.reattached
        second.pages[0].user_closed_it()
        second.new_page_error = lost

        with pytest.raises(BrowserUnavailable, match="went away twice"):
            await run.ensure_page()
        assert page.is_closed()

    assert connector.attaches == 2, "the run did not keep reattaching"


async def test_a_reattach_that_cannot_attach_ends_the_run() -> None:
    gone = FakeContext(new_page_error=RuntimeError("browser has been closed"))
    connector = FakeConnector([FakeBrowser([gone]), FakeBrowser([])])
    provider = make_provider(connector)

    async with provider.run() as run:
        with pytest.raises(BrowserUnavailable, match="no open browser context"):
            await run.ensure_page()


async def test_a_tab_that_dies_mid_navigation_is_reopened_and_the_visit_finishes() -> None:
    """The loss can arrive during the navigation, not only before it (spec 9.9).

    Without this, the run hands the caller a raw driver error, the reattach is never
    spent, and the scheduler sees an unclassified crash instead of a parked retry.
    """
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        first = await run.ensure_page()
        only_page(context).fail_next_goto(
            RuntimeError("Target page, context or browser has been closed")
        )

        page = await run.goto(LOCAL_PAGE)

        assert not page.is_closed()
        assert page is not first
        assert context.new_page_calls == 2
        assert context.pages[1].goto_calls == [LOCAL_PAGE]
        assert run.last_url == LOCAL_PAGE
        assert not run.reattached, "a lost tab is not a lost browser"
        assert connector.attaches == 1


async def test_a_browser_that_disconnects_mid_navigation_spends_the_reattach() -> None:
    """The tab stays open and the socket is gone: ``is_connected()`` is the tell."""
    first = FakeContext()
    healthy = FakeContext()
    dying = FakeBrowser([first])
    connector = FakeConnector([dying, FakeBrowser([healthy])])
    provider = make_provider(connector)

    async with provider.run() as run:
        await run.ensure_page()
        first.pages[0].fail_next_goto(RuntimeError("Connection closed"), closes=False)
        dying.connected = False
        first.new_page_error = RuntimeError("browser has been closed")

        recovered = await run.goto(LOCAL_PAGE)

        assert run.reattached
        assert connector.attaches == 2
        assert recovered in healthy.pages
        assert healthy.pages[0].goto_calls == [LOCAL_PAGE]


async def test_a_navigation_that_loses_the_tab_twice_ends_the_run() -> None:
    lost = RuntimeError("Target page, context or browser has been closed")
    context = FakeContext(page_goto_error=lost)
    connector = FakeConnector([FakeBrowser([context])])
    provider = make_provider(connector)

    async with provider.run() as run:
        with pytest.raises(BrowserUnavailable, match="went away again"):
            await run.goto(LOCAL_PAGE)

    assert context.new_page_calls == 2, "the run reopened the tab once, not forever"


async def test_a_navigation_that_merely_fails_belongs_to_the_caller() -> None:
    """A tab that is still there means the site answered. That is spec 9.7's business.

    Recovering here would hide a throttle or a checkpoint behind a page reload, and
    reloading it would spend the budget twice.
    """
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        await run.ensure_page()
        only_page(context).fail_next_goto(TimeoutError("Timeout 30000ms exceeded"), closes=False)

        with pytest.raises(TimeoutError, match="Timeout"):
            await run.goto(LOCAL_PAGE)

        assert context.new_page_calls == 1, "nothing was reopened"
        assert not run.reattached
        assert run.last_url is None, "a failed navigation is not where the run is"


# --- the activity lock -------------------------------------------------------


async def test_one_run_at_a_time_for_an_account() -> None:
    """Spec 9.9: two CDP clients on one browser drop each other's connection."""
    connector = FakeConnector()
    provider = make_provider(connector)

    async with provider.run("account-7"):
        assert provider.locks.is_busy("account-7")
        # The timeout turns "the second run waits forever" into a failure rather than
        # a hung suite, for the day someone drops the busy check.
        with pytest.raises(BrowserBusy, match="account-7"):
            async with asyncio.timeout(BUSY_TIMEOUT_S), provider.run("account-7"):
                pass

    assert connector.attaches == 1, "the busy run never reached the browser"
    assert not provider.locks.is_busy("account-7")


async def test_a_second_account_runs_while_the_first_is_busy() -> None:
    """ADR 0005: the lock is keyed by ``linkedin_account``, so accounts do not queue."""
    provider = make_provider()

    async with provider.run("account-1") as first, provider.run("account-2") as second:
        assert first.account == "account-1"
        assert second.account == "account-2"
        assert provider.locks.is_busy("account-1")
        assert provider.locks.is_busy("account-2")
        assert provider.locks.lock_for("account-1") is not provider.locks.lock_for("account-2")


async def test_one_registry_gates_every_provider_that_shares_it() -> None:
    """One registry, handed to everything that attaches in a process, is one gate."""
    locks = ActivityLocks()
    scheduler = make_provider(FakeConnector(), locks)
    dashboard = make_provider(FakeConnector(), locks)

    async with scheduler.run("account-7"):
        with pytest.raises(BrowserBusy, match="account-7"):
            async with asyncio.timeout(BUSY_TIMEOUT_S), dashboard.run("account-7"):
                pass


async def test_a_second_registry_is_gated_by_the_file_lock() -> None:
    """Issue #153: a registry of its own is what `netkeeper preflight` in a terminal has.

    Before the file lock, this second provider attached alongside the first. The file
    lock is per open file description, so it refuses a second registry in the same
    process exactly as it refuses another process (``test_activity_lock_processes``).
    """
    connector = FakeConnector()
    held = make_provider(FakeConnector(), ActivityLocks())
    separate = make_provider(connector, ActivityLocks())

    async with held.run("account-7"):
        with pytest.raises(BrowserBusy, match=r"account-7.*in use by .*pid \d+"):
            async with asyncio.timeout(BUSY_TIMEOUT_S), separate.run("account-7"):
                pass
        assert separate.locks.is_busy("account-7"), "the other registry sees the holder"

    assert connector.attaches == 0, "the refused provider never reached the browser"
    async with separate.run("account-7"):
        pass
    assert connector.attaches == 1, "released, the account is free for the next registry"


async def test_a_failed_run_still_releases_the_lock() -> None:
    provider = make_provider()

    with pytest.raises(BrowserUnavailable):
        async with provider.run():
            raise BrowserUnavailable("the browser went away")

    assert not provider.locks.is_busy(SINGLE_ACCOUNT_KEY)
    async with provider.run():
        pass


async def test_a_waiting_run_queues_behind_the_holder() -> None:
    provider = make_provider()
    order: list[str] = []
    holder_has_it = asyncio.Event()

    async def holder() -> None:
        async with provider.run("shared", wait=True):
            holder_has_it.set()
            await asyncio.sleep(0)
            order.append("holder")

    async def waiter() -> None:
        await holder_has_it.wait()
        async with provider.run("shared", wait=True):
            order.append("waiter")

    await asyncio.gather(holder(), waiter())
    assert order == ["holder", "waiter"]


async def test_locks_are_made_once_per_account() -> None:
    locks = ActivityLocks()
    assert locks.lock_for("a") is locks.lock_for("a")
    assert locks.lock_for("a") is not locks.lock_for("b")
    assert not locks.is_busy("never-used")


# --- replaying a scroll plan (#152) -------------------------------------------


class Sleeper:
    """Records what it was asked to wait and waits none of it."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make_plan(*steps: tuple[int, float], dwell_s: float = 2.5) -> ScrollPlan:
    return ScrollPlan(
        steps=tuple(ScrollStep(delta_px=delta, pause_s=pause) for delta, pause in steps),
        dwell_s=dwell_s,
    )


async def _no_sleep(seconds: float) -> None:
    return None


async def _prime_pointer(run: BrowserRun) -> None:
    """Spend this tab's one-time pointer rest (#192) so a test can isolate the wheel
    replay that follows it.

    ``BrowserRun.scroll`` moves the pointer to rest over the content once per tab,
    before its first wheel event (see the dedicated ``test_scroll_rests_the_pointer_*``
    tests below), and does not repeat it on a later call against the same tab. Priming
    it here first, on a throwaway empty plan with its own no-op sleep, keeps every
    other scroll test's wheel and sleep assertions exactly what they were before #192
    without needing to hand-compute a pointer-rest walk's random jitter and pauses.
    """
    await run.scroll(make_plan(dwell_s=0.0), sleep=_no_sleep)


async def test_scroll_sends_one_wheel_per_step_then_sleeps_each_pause_and_the_dwell() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((120, 0.4), (-90, 0.2), (500, 0.9), dwell_s=3.0)
    sleeper = Sleeper()

    async with provider.run() as run:
        await _prime_pointer(run)
        outcome = await run.scroll(plan, sleep=sleeper)

    page = only_page(context)
    assert outcome.page is page
    assert outcome.cancelled is False
    assert page.mouse.wheels == [(0, 120), (0, -90), (0, 500)]
    # Order matters as much as the values: a plan replayed out of order, or with
    # the dwell folded into a step's own pause, would still sum to the same total.
    assert sleeper.waits == [0.4, 0.2, 0.9, 3.0]


async def test_scroll_sends_each_steps_wheel_event_before_its_own_sleep() -> None:
    """N15: a step's wheel event must land before its sleep, not after.

    ``wheels`` and ``waits`` are recorded on two separate objects, so a plan
    replayed as "sleep, then wheel" instead of "wheel, then sleep" still produces
    the identical two lists in the identical order -- neither list alone can tell
    the two apart. This merges both into one interleaved, tagged log instead.
    """
    connector = FakeConnector()
    provider = make_provider(connector)
    plan = make_plan((1, 0.1), (2, 0.2), dwell_s=9.0)
    events: list[str] = []

    async def logging_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")

    async with provider.run() as run:
        page = await run.ensure_page()
        real_page = connector.browsers[0].context_list[0].pages[0]
        assert page is real_page
        await _prime_pointer(run)
        real_wheel = real_page.mouse.wheel

        async def logging_wheel(delta_x: float, delta_y: float) -> None:
            events.append(f"wheel:{delta_y}")
            await real_wheel(delta_x, delta_y)

        real_page.mouse.wheel = logging_wheel  # type: ignore[method-assign]
        await run.scroll(plan, sleep=logging_sleep)

    assert events == ["wheel:1", "sleep:0.1", "wheel:2", "sleep:0.2", "sleep:9.0"]


async def test_scroll_with_no_steps_still_sleeps_the_dwell() -> None:
    provider = make_provider()
    plan = make_plan(dwell_s=1.5)
    sleeper = Sleeper()

    async with provider.run() as run:
        await _prime_pointer(run)
        await run.scroll(plan, sleep=sleeper)

    assert sleeper.waits == [1.5]


async def test_scroll_defaults_to_a_real_sleep() -> None:
    """No ``sleep`` given at all still works -- the default is ``asyncio.sleep``."""
    provider = make_provider()
    plan = make_plan((10, 0.0), dwell_s=0.0)

    async with provider.run() as run:
        await run.scroll(plan)


async def test_scroll_reopens_a_lost_tab_and_scrolls_the_recovered_one() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((100, 0.1))

    async with provider.run() as run:
        await run.goto(LOCAL_PAGE)
        lost = only_page(context)
        lost.user_closed_it()

        outcome = await run.scroll(plan, sleep=Sleeper())

        recovered = context.pages[-1]
        assert outcome.page is recovered
        assert outcome.cancelled is False
        assert recovered is not lost
        assert not recovered.is_closed()
        assert recovered.mouse.wheels == [(0, 100)]
        assert lost.mouse.wheels == [], "the closed tab's own mouse recorded nothing"
        assert context.new_page_calls == 2
        # #192: the recovered tab is a *new* tab as far as the pointer goes -- it
        # must be rested again, not treated as already resting because the run's
        # earlier (lost) tab once was.
        assert recovered.mouse.moves, "the recovered tab's pointer must be rested too"
        assert recovered.mouse.moves[-1] != (0.0, 0.0), "never teleport back to (0, 0)"
        assert lost.mouse.moves == [], "the closed tab's own mouse recorded no movement either"


async def test_a_cancelled_scroll_stops_before_its_next_wheel_event() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((1, 0.1), (2, 0.1), (3, 0.1), dwell_s=9.0)
    sleeper = Sleeper()
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        # #192 review, F6: `scroll` now polls once before the pointer-rest walk
        # too, ahead of the loop's own first check -- 2 calls happen before the
        # first wheel event now, not 1.
        return calls > 2  # let the first wheel event through, then stop

    async with provider.run() as run:
        await _prime_pointer(run)
        outcome = await run.scroll(plan, sleep=sleeper, cancelled=cancelled)

    page = only_page(context)
    assert outcome.page is page
    assert outcome.cancelled is True, "F8: the outcome must say so, not leave the caller to guess"
    assert page.mouse.wheels == [(0, 1)], "a cancelled replay must not send the rest of the plan"
    assert sleeper.waits == [0.1], "nor wait out the steps or the dwell it never reached"


async def test_a_scroll_cancelled_only_before_the_dwell_still_sends_every_wheel_event() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((1, 0.1), (2, 0.1), dwell_s=9.0)
    sleeper = Sleeper()

    def cancelled_after_steps() -> bool:
        return len(sleeper.waits) >= 2  # both steps have paused; only the dwell is left

    async with provider.run() as run:
        await _prime_pointer(run)
        outcome = await run.scroll(plan, sleep=sleeper, cancelled=cancelled_after_steps)

    page = only_page(context)
    assert outcome.cancelled is True
    assert page.mouse.wheels == [(0, 1), (0, 2)], "cancelling before the dwell must not skip a step"
    assert sleeper.waits == [0.1, 0.1], "the dwell itself must not run once cancelled"


async def test_an_uncancelled_scroll_ignores_a_cancelled_callback_that_says_no() -> None:
    provider = make_provider()
    plan = make_plan((10, 0.05), dwell_s=0.25)
    sleeper = Sleeper()

    async with provider.run() as run:
        await _prime_pointer(run)
        outcome = await run.scroll(plan, sleep=sleeper, cancelled=lambda: False)

    assert outcome.cancelled is False
    assert sleeper.waits == [0.05, 0.25]


# --- resting the pointer over content before a scroll (#192) --------------------------


async def test_scroll_moves_the_pointer_before_any_wheel_event_lands() -> None:
    """Playwright's virtual pointer starts at (0, 0), over a fixed header on the real
    page (#192). Pin the order, not just that both a move and a wheel happened -- a
    rest that landed *after* the first wheel event would not have fixed anything."""
    connector = FakeConnector()
    provider = make_provider(connector)
    plan = make_plan((100, 0.05), (50, 0.05), dwell_s=0.0)
    events: list[str] = []

    async with provider.run() as run:
        await run.ensure_page()
        real_page = only_page(connector.browsers[0].context_list[0])
        real_move = real_page.mouse.move
        real_wheel = real_page.mouse.wheel

        async def logging_move(x: float, y: float) -> None:
            events.append("move")
            await real_move(x, y)

        async def logging_wheel(delta_x: float, delta_y: float) -> None:
            events.append("wheel")
            await real_wheel(delta_x, delta_y)

        real_page.mouse.move = logging_move  # type: ignore[method-assign]
        real_page.mouse.wheel = logging_wheel  # type: ignore[method-assign]
        await run.scroll(plan, sleep=Sleeper(), rng=random.Random(3))

    first_wheel = events.index("wheel")
    assert first_wheel > 0, "the pointer must move at least once before the first wheel event"
    assert set(events[:first_wheel]) == {"move"}, events
    assert events[first_wheel:] == ["wheel", "wheel"], events


async def test_scroll_does_not_rest_the_pointer_again_on_the_same_tab() -> None:
    """Once per run/tab, not once per call -- a caller that scrolls the same tab
    repeatedly (one call per page of the connections list) must not see the pointer
    walk back across the screen before every step (#192)."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(1))
        page = only_page(context)
        first_moves = list(page.mouse.moves)
        assert first_moves

        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(2))

    assert page.mouse.moves == first_moves, "the pointer rests once per tab, not once per call"


async def test_scroll_rests_the_pointer_again_after_the_tab_is_lost_and_reopened() -> None:
    """The once-per-tab rule is keyed to the tab, not the run: a tab that was already
    rested and is then lost must have its *replacement* rested too, not skipped as if
    the new tab already had it (#192)."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(1))
        first = only_page(context)
        assert first.mouse.moves, "the first tab must have been rested"

        first.user_closed_it()
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(2))

    recovered = context.pages[-1]
    assert recovered is not first
    assert recovered.mouse.moves, "the recovered tab must be rested again, not skipped"


async def test_scroll_rests_the_pointer_near_the_top_of_the_content_box_when_found() -> None:
    """#192 review, F1: real, on-screen geometry from a passive ``bounding_box``
    read -- not a guess at the viewport -- is what the pointer actually targets.
    A centered 800px column on an otherwise much wider window (the reviewer's
    2200px-ultrawide reproduction) is exactly the case a viewport-center guess
    missed: the old code aimed at (640, 400) regardless of where the content
    actually was.

    Horizontally it still centers (the box's width does not grow with the list).
    Vertically it does not: #192 review round 2, N1 found that centering
    vertically too aims below the real window once the box is tall enough --
    this box (1200px) is exactly such a case, so the target lands
    ``REST_VISIBLE_SPAN_PX`` below the box's top, not at its true center (900)."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 700.0,
            "y": 300.0,
            "width": 800.0,
            "height": 1200.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    x, y = only_page(context).mouse.moves[-1]
    assert x == 1100.0  # the box's own horizontal center
    assert y == 300.0 + browser.REST_VISIBLE_SPAN_PX  # near its top, not its center (900)
    assert only_page(context).locator_calls == [browser.CONTENT_LANDMARK_SELECTOR]


LINK = 'a[href*="/messaging/thread/"]'
MAIN_BOX = {"x": 700.0, "y": 300.0, "width": 800.0, "height": 1200.0}


async def _scroll_resting_over(
    boxes: list[dict[str, float] | None] | None,
    *,
    viewport: dict[str, int] | None = None,
    seed: int = 4,
) -> tuple[list[tuple[float, float]], list[str]]:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.viewport_size = viewport
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = MAIN_BOX
        if boxes is not None:
            page.match_boxes[LINK] = list(boxes)
        await run.scroll(
            make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(seed), rest_over=LINK
        )
    page = only_page(context)
    return page.mouse.moves, page.locator_calls


async def test_scroll_rests_over_the_center_of_the_rest_over_element() -> None:
    """#439: a conversation link in a narrow list pane is the target, not ``<main>``'s
    center (x=1100 here, over the thread pane)."""
    link = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 100.0}
    moves, calls = await _scroll_resting_over([link])
    assert calls == [LINK]  # <main> is never read when the link has a box
    assert moves
    assert all(100.0 <= x <= 400.0 and 200.0 <= y <= 300.0 for x, y in moves), moves
    assert moves[-1] == pytest.approx((250.0, 250.0), abs=60.0)


async def test_scroll_falls_back_to_main_when_the_rest_over_element_is_absent() -> None:
    moves, calls = await _scroll_resting_over(None)
    assert calls == [LINK, browser.CONTENT_LANDMARK_SELECTOR]
    assert moves[-1][0] > 700.0  # near <main>'s horizontal center, 1100


async def test_scroll_falls_back_to_main_when_every_match_is_hidden() -> None:
    hidden = [None, {"x": 5.0, "y": 5.0, "width": 0.0, "height": 0.0}]
    moves, calls = await _scroll_resting_over(hidden)
    assert calls[-1] == browser.CONTENT_LANDMARK_SELECTOR
    assert moves[-1][0] > 700.0


async def test_scroll_skips_a_hidden_match_for_the_next_visible_one() -> None:
    visible = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, calls = await _scroll_resting_over([None, visible])
    assert calls == [LINK]
    assert all(100.0 <= x <= 400.0 and 400.0 <= y <= 500.0 for x, y in moves), moves


async def test_scroll_skips_a_match_scrolled_below_the_known_viewport() -> None:
    below = {"x": 100.0, "y": 2000.0, "width": 300.0, "height": 100.0}
    visible = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([below, visible], viewport={"width": 1400, "height": 800})
    assert all(400.0 <= y <= 500.0 for _, y in moves), moves


async def test_scroll_stays_clear_of_the_nav_over_a_link_that_starts_under_it() -> None:
    link = {"x": 100.0, "y": 56.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([link])
    assert all(y >= browser.REST_MIN_Y_PX for _, y in moves), moves


async def test_scroll_skips_a_match_positioned_left_of_the_screen() -> None:
    """#441 review: a link at ``left: -9999px`` has a real box with a center on y."""
    offscreen = {"x": -9999.0, "y": 400.0, "width": 300.0, "height": 100.0}
    visible = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([offscreen, visible])
    assert all(x >= 0 for x, _ in moves), moves
    assert all(100.0 <= x <= 400.0 for x, _ in moves), moves


async def test_scroll_skips_a_match_right_of_the_known_viewport_width() -> None:
    right = {"x": 3000.0, "y": 400.0, "width": 300.0, "height": 100.0}
    visible = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([right, visible], viewport={"width": 1400, "height": 800})
    assert all(100.0 <= x <= 400.0 for x, _ in moves), moves


async def test_scroll_holds_the_rest_over_jitter_inside_the_screen() -> None:
    """A link mostly off the left edge, its center just on screen: no wobble may leave."""
    half = {"x": -60.0, "y": 400.0, "width": 140.0, "height": 100.0}  # center x = 10
    wide = {"x": 1400.0, "y": 400.0, "width": 400.0, "height": 100.0}  # center x = 1600
    for seed in range(30):
        moves, _ = await _scroll_resting_over([half], seed=seed)
        assert all(0.0 <= x <= 80.0 for x, _ in moves), (seed, moves)
        moves, _ = await _scroll_resting_over(
            [wide], viewport={"width": 1600, "height": 800}, seed=seed
        )
        assert all(1400.0 <= x <= 1600.0 for x, _ in moves), (seed, moves)


async def test_scroll_skips_a_zero_area_match_below_the_nav() -> None:
    flat = {"x": 100.0, "y": 400.0, "width": 0.0, "height": 100.0}
    thin = {"x": 100.0, "y": 400.0, "width": 300.0, "height": 0.0}
    visible = {"x": 600.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([flat, thin, visible])
    assert all(600.0 <= x <= 900.0 for x, _ in moves), moves


async def test_scroll_skips_a_link_that_sits_entirely_under_the_nav() -> None:
    under = {"x": 100.0, "y": 10.0, "width": 300.0, "height": 40.0}  # center y 30
    visible = {"x": 600.0, "y": 400.0, "width": 300.0, "height": 100.0}
    moves, _ = await _scroll_resting_over([under, visible])
    assert all(600.0 <= x <= 900.0 for x, _ in moves), moves


async def test_scroll_falls_back_when_reading_the_rest_over_element_raises() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = MAIN_BOX
        page.locator_error = TimeoutError("slow")
        await run.scroll(
            make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4), rest_over=LINK
        )
    assert only_page(context).mouse.moves[-1][0] > 700.0


async def test_scroll_rests_near_the_true_center_of_a_short_content_box() -> None:
    """A box short enough that half its height is under ``REST_VISIBLE_SPAN_PX``
    still centers, the same as before #192 review round 2 -- the visible-span cap
    only matters once a box is tall enough to reach past the real window."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 700.0,
            "y": 300.0,
            "width": 800.0,
            "height": 400.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    x, y = only_page(context).mouse.moves[-1]
    assert (x, y) == (1100.0, 500.0)  # the box's own center: 300 + 400 / 2


async def test_scroll_stays_within_the_visible_span_of_a_tall_content_box() -> None:
    """#192 review round 2, N1: reproduces the actual failure -- an in-flow
    ``<main>`` whose *ancestor* does the scrolling reports its own full content
    height here (the whole list, ~2300px in the reviewer's report), not the
    sliver the viewport shows. Centering on that, or letting jitter roam across
    it, put the pointer hundreds of pixels below any real window and brought
    back the #31 symptom. Every move must land within an ordinary window's reach
    of the box's top, not deep inside a list that keeps growing."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 0.0,
            "y": 56.0,
            "width": 800.0,
            "height": 2300.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(y <= 400 for _, y in moves), moves
    assert all(y >= browser.REST_MIN_Y_PX for _, y in moves), moves


@pytest.mark.parametrize(
    ("height", "high"),
    [(2300.0, 100.0 + 2 * 250), (60.0, 100.0 + 60)],
    ids=["tall-box", "short-box"],
)
async def test_the_rest_jitter_never_reaches_past_its_upper_bound(
    monkeypatch: pytest.MonkeyPatch, height: float, high: float
) -> None:
    """#196 item 5: the ordinary ±40px jitter never comes near the upper bound on the
    rest point's waypoints, so a plan with a far wider wobble pins it: never more than
    twice :data:`REST_VISIBLE_SPAN_PX` below the box's top, and never past the bottom
    of a short box -- and never above the box's top either."""
    wide = RestPlan(
        steps=(
            RestStep(dx=0, dy=5000, pause_s=0.0),
            RestStep(dx=0, dy=-5000, pause_s=0.0),
            RestStep(dx=0, dy=0, pause_s=0.0),
        )
    )
    monkeypatch.setattr(browser, "rest_pointer_like_a_person", lambda rng: wide)
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 0.0,
            "y": 100.0,
            "width": 800.0,
            "height": height,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    ys = [y for _, y in only_page(context).mouse.moves]
    assert ys[:2] == [high, 100.0], ys
    assert browser.REST_VISIBLE_SPAN_PX == 250


async def test_scroll_caps_a_tall_box_jitter_at_the_known_viewport_height() -> None:
    """When Playwright *does* know the real viewport height, it caps the tall-box
    bound even tighter than :data:`REST_VISIBLE_SPAN_PX` alone would (#192
    review round 2, N1, point 2)."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.viewport_size = {"width": 800, "height": 300}
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 0.0,
            "y": 56.0,
            "width": 800.0,
            "height": 2300.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(y == 300.0 for _, y in moves), moves


async def test_scroll_handles_a_box_whose_top_is_at_or_above_the_page_origin() -> None:
    """#192 review round 2, N1, point 4: a zero or negative ``box.y`` (a box
    already partly scrolled past, or one CSS places above the fold) must not
    push the rest point above the header floor either."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 0.0,
            "y": -50.0,
            "width": 800.0,
            "height": 2300.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(y >= browser.REST_MIN_Y_PX for _, y in moves), moves


async def test_scroll_keeps_the_pointer_on_screen_when_the_box_starts_off_screen() -> None:
    """#192 review round 2, N1, point 3: a box partly (or wholly) off-screen to
    the left must not send the pointer to a negative ``x``."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": -500.0,
            "y": 300.0,
            "width": 800.0,
            "height": 400.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(4))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(x >= 0.0 for x, _ in moves), moves


async def test_scroll_keeps_jitter_inside_the_content_box() -> None:
    """Every waypoint, not just the final one, across many seeds."""
    box = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 400.0}
    for seed in range(30):
        connector = FakeConnector()
        provider = make_provider(connector)
        context = connector.browsers[0].context_list[0]
        async with provider.run() as run:
            page = cast(FakePage, await run.ensure_page())
            page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = box
            await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(seed))
        moves = only_page(context).mouse.moves
        assert moves, seed
        for x, y in moves:
            assert box["x"] <= x <= box["x"] + box["width"], (seed, x, y)
            assert box["y"] <= y <= box["y"] + box["height"], (seed, x, y)


async def test_scroll_falls_back_to_the_viewport_guess_with_no_content_box() -> None:
    """No ``<main>`` landmark (an empty ``content_boxes``, the fake's default): the
    pointer still rests somewhere sane, from the viewport guess -- never at
    (0, 0), which is where Playwright's virtual pointer starts and exactly the
    bug this fixes."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        assert page.content_boxes == {}
        assert page.viewport_size is None, "an attached tab commonly reports neither (#192)"
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(9))

    x, y = only_page(context).mouse.moves[-1]
    assert (x, y) != (0.0, 0.0)
    assert x == browser.DEFAULT_VIEWPORT_WIDTH / 2
    assert y == browser.DEFAULT_VIEWPORT_HEIGHT * browser.REST_Y_FRACTION
    assert y >= browser.REST_MIN_Y_PX


async def test_scroll_falls_back_to_the_viewport_guess_when_bounding_box_fails() -> None:
    """A ``bounding_box`` read that raises (a timeout, say) is treated the same as
    no box at all -- a best-effort read, never something a run fails over."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.locator_error = TimeoutError("no main landmark within 1000ms")
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(9))

    x, y = only_page(context).mouse.moves[-1]
    assert x == browser.DEFAULT_VIEWPORT_WIDTH / 2
    assert y == browser.DEFAULT_VIEWPORT_HEIGHT * browser.REST_Y_FRACTION


async def test_scroll_rests_the_pointer_using_the_tabs_own_viewport_when_known_and_no_box() -> None:
    """A passive read, used when Playwright does have it and there is no box --
    not always the default."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.viewport_size = {"width": 400, "height": 300}
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(2))

    x, y = only_page(context).mouse.moves[-1]
    assert x == 200.0
    assert y == 150.0  # 300 * REST_Y_FRACTION -- above REST_MIN_Y_PX, so the floor never bites


async def test_scroll_clamps_to_the_header_floor_on_a_tiny_viewport() -> None:
    """#192 review, F5: the floor has to be *reachable*, not just present in the
    formula -- a small enough viewport is exactly where it bites, proving a
    mutation that dropped it (or replaced it with 0) would be caught."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.viewport_size = {"width": 400, "height": 150}  # REST_Y_FRACTION * 150 = 75 < 96
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(2))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(y == browser.REST_MIN_Y_PX for _, y in moves), moves


async def test_scroll_clamps_to_the_header_floor_when_the_box_is_near_the_top() -> None:
    """The same floor, reached from the content-box path this time: a box that
    starts (implausibly, but defensively) above the floor is still never let
    above it, not just the viewport-guess fallback (#192 review, F5). Not exact
    equality any more (#192 review round 2, N1 changed the formula so a short
    box's own height also contributes) -- the invariant that still must hold
    unconditionally is the lower bound."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]

    async with provider.run() as run:
        page = cast(FakePage, await run.ensure_page())
        page.content_boxes[browser.CONTENT_LANDMARK_SELECTOR] = {
            "x": 0.0,
            "y": 0.0,
            "width": 300.0,
            "height": 40.0,
        }
        await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(2))

    moves = only_page(context).mouse.moves
    assert moves
    assert all(y >= browser.REST_MIN_Y_PX for _, y in moves), moves


async def test_scroll_never_jitters_the_pointer_above_the_minimum_header_clearance() -> None:
    """Every waypoint on the way to rest, not just the last one, across many seeds
    (fallback path: no content box)."""
    for seed in range(30):
        connector = FakeConnector()
        provider = make_provider(connector)
        context = connector.browsers[0].context_list[0]
        async with provider.run() as run:
            await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(seed))
        moves = only_page(context).mouse.moves
        assert moves, (seed, "the pointer must move at least once before a scroll (#192)")
        for x, y in moves:
            assert 0 <= x <= browser.DEFAULT_VIEWPORT_WIDTH, (seed, x, y)
            assert browser.REST_MIN_Y_PX <= y <= browser.DEFAULT_VIEWPORT_HEIGHT, (seed, x, y)


async def test_scroll_rests_the_pointer_identically_for_the_same_seed() -> None:
    """Same determinism guarantee as :func:`~netkeeper.linkedin.pacing.scroll_like_a_person`
    itself (spec P2-04 done-when): same seed, same walk, in this process or another."""

    async def moves_for(seed: int) -> list[tuple[float, float]]:
        connector = FakeConnector()
        provider = make_provider(connector)
        context = connector.browsers[0].context_list[0]
        async with provider.run() as run:
            await run.scroll(make_plan(dwell_s=0.0), sleep=Sleeper(), rng=random.Random(seed))
        return only_page(context).mouse.moves

    first = await moves_for(7)
    assert first, "the pointer must move at least once before a scroll (#192)"
    assert first == await moves_for(7)


async def test_a_cancelled_scroll_never_moves_the_pointer_or_sends_a_wheel_event() -> None:
    """#192 review, F6: cancelled before it starts means before *anything* starts,
    the pointer-rest walk included -- not just the wheel replay."""
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((100, 0.1), dwell_s=1.0)

    async with provider.run() as run:
        outcome = await run.scroll(plan, sleep=Sleeper(), cancelled=lambda: True)

    page = only_page(context)
    assert outcome.cancelled is True
    assert page.mouse.moves == []
    assert page.mouse.wheels == []


# --- #169 F: keyed by the account row, and never alongside an older process ------------


def test_the_lock_key_is_the_account_row() -> None:
    from netkeeper.linkedin import activity_lock

    assert activity_lock.account_key(1) == "account-1" == SINGLE_ACCOUNT_KEY
    assert activity_lock.account_key(12) == "account-12"
    assert activity_lock.LEGACY_SHARED_KEY == "local"
    with pytest.raises(ValueError):
        activity_lock.account_key(0)


async def test_an_older_process_holding_the_legacy_lock_blocks_account_one() -> None:
    """A pre-P2-10 netkeeper holds only ``browser-local.lock``; a new hold of account 1
    must not attach alongside it, and a new hold must keep an old one out too."""
    from netkeeper.linkedin import activity_lock

    old = activity_lock.try_claim(activity_lock.LEGACY_SHARED_KEY)
    assert old is not None
    connector = FakeConnector()
    provider = make_provider(connector)
    try:
        with pytest.raises(BrowserBusy):
            async with asyncio.timeout(BUSY_TIMEOUT_S), provider.run(SINGLE_ACCOUNT_KEY):
                pass
        assert connector.attaches == 0
        assert provider.locks.is_busy(SINGLE_ACCOUNT_KEY)
        # Another account never waits on the legacy file.
        async with provider.run("account-2"):
            pass
        # A refused hold of account 1 took nothing with it.
        assert not activity_lock.inspect(SINGLE_ACCOUNT_KEY).held
    finally:
        old.release()

    async with provider.run(SINGLE_ACCOUNT_KEY):
        assert activity_lock.try_claim(activity_lock.LEGACY_SHARED_KEY) is None
    assert not activity_lock.inspect(activity_lock.LEGACY_SHARED_KEY).held


async def test_the_legacy_lock_goes_with_the_named_partner_account() -> None:
    """#175 review F10: whichever account is the local user's co-claims the legacy lock."""
    from netkeeper.linkedin import activity_lock

    provider = make_provider(locks=ActivityLocks(legacy_partner="account-5"))
    async with provider.run("account-5"):
        assert activity_lock.inspect(activity_lock.LEGACY_SHARED_KEY).held
    async with provider.run(SINGLE_ACCOUNT_KEY):
        assert not activity_lock.inspect(activity_lock.LEGACY_SHARED_KEY).held
