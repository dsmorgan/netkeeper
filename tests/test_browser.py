"""The attach path: one context, one tab, one reattach, one lock (spec 9.1, 9.9).

Everything here runs offline against the fakes in ``tests/browser_fakes.py``. The
smoke suite in ``tests/smoke/`` drives the same code against a real Chrome, and
``tests/test_browser_safety.py`` is what keeps a launch out of the package.
"""

from __future__ import annotations

import asyncio

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext, FakePage

from netkeeper.linkedin.browser import (
    ATTACH,
    SINGLE_ACCOUNT_KEY,
    ActivityLocks,
    AttachBrowserProvider,
    BrowserBusy,
    BrowserProvider,
    BrowserUnavailable,
)
from netkeeper.linkedin.pacing import ScrollPlan, ScrollStep

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


async def test_scroll_sends_one_wheel_per_step_then_sleeps_each_pause_and_the_dwell() -> None:
    connector = FakeConnector()
    provider = make_provider(connector)
    context = connector.browsers[0].context_list[0]
    plan = make_plan((120, 0.4), (-90, 0.2), (500, 0.9), dwell_s=3.0)
    sleeper = Sleeper()

    async with provider.run() as run:
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
        return calls > 1  # let the first wheel event through, then stop

    async with provider.run() as run:
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
        outcome = await run.scroll(plan, sleep=sleeper, cancelled=lambda: False)

    assert outcome.cancelled is False
    assert sleeper.waits == [0.05, 0.25]


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
