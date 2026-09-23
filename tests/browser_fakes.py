"""A browser that is not a browser: fakes for the attach path's offline tests.

The real thing needs a Chrome the developer started, so everything netkeeper does to
a browser goes through the narrow protocols in ``netkeeper.linkedin.browser``, and
these fakes implement them. mypy checks the fakes against the same protocols the
Playwright objects satisfy, so a fake that drifts from the real API is a type error.

The fakes also carry the invariants: :meth:`FakeBrowser.new_context` and
:meth:`FakeBrowser.close` raise rather than pretend, so a code path that reaches for
a second identity or shuts the user's browser fails the test that runs it, not a
review three weeks later.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from netkeeper.linkedin.browser import Connection, ContextLike, PageLike


class FakePage:
    """One tab. Records what it was asked to do and can be closed behind the run's back."""

    def __init__(self, context: FakeContext) -> None:
        self.context = context
        self.goto_calls: list[str] = []
        self.evaluate_calls: list[str] = []
        self.close_calls = 0
        self._closed = False
        self._url = "about:blank"

    @property
    def url(self) -> str:
        return self._url

    def is_closed(self) -> bool:
        return self._closed

    async def goto(self, url: str) -> object:
        assert not self._closed, "navigated a closed tab"
        self.goto_calls.append(url)
        self._url = url
        return None

    async def evaluate(self, expression: str) -> Any:
        assert not self._closed, "evaluated on a closed tab"
        self.evaluate_calls.append(expression)
        return self.context.evaluate_result

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True

    def user_closed_it(self) -> None:
        """The tab going away without netkeeper noticing: the case ``_ensure_page`` exists for."""
        self._closed = True


class FakeContext:
    """The context the user's browsing already lives in. Hands out tabs and a cookie jar."""

    def __init__(
        self,
        *,
        cookies: Sequence[Mapping[str, Any]] = (),
        evaluate_result: Any = None,
        new_page_error: Exception | None = None,
    ) -> None:
        self.pages: list[FakePage] = []
        self.cookie_jar: list[Mapping[str, Any]] = list(cookies)
        self.cookie_error: Exception | None = None
        self.evaluate_result = evaluate_result
        self.new_page_error = new_page_error
        self.new_page_calls = 0
        self.cookie_calls = 0

    async def new_page(self) -> PageLike:
        self.new_page_calls += 1
        if self.new_page_error is not None:
            raise self.new_page_error
        page = FakePage(self)
        self.pages.append(page)
        return page

    async def cookies(self) -> Sequence[Mapping[str, Any]]:
        self.cookie_calls += 1
        if self.cookie_error is not None:
            raise self.cookie_error
        return list(self.cookie_jar)

    @property
    def open_pages(self) -> list[FakePage]:
        return [page for page in self.pages if not page.is_closed()]

    async def close(self) -> None:
        raise AssertionError("the context belongs to the user; netkeeper never closes it")


class FakeBrowser:
    """The attached Chrome. Creating a context or closing it is a test failure."""

    def __init__(
        self, contexts: Sequence[FakeContext] | None = None, version: str = "Chrome/140.0.7339.80"
    ) -> None:
        self.context_list = [FakeContext()] if contexts is None else list(contexts)
        self.connected = True
        self._version = version

    @property
    def contexts(self) -> Sequence[ContextLike]:
        return self.context_list

    @property
    def version(self) -> str:
        return self._version

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self) -> ContextLike:
        raise AssertionError("a second context is a second LinkedIn device (ADR 0002)")

    async def close(self) -> None:
        raise AssertionError("netkeeper neither starts nor stops the user's browser")


class FakeConnector:
    """Stands in for ``connect_over_cdp``. Counts attaches and detaches; cannot launch.

    Pass several browsers to describe a browser that goes away mid-run: the first
    attach gets the first, the reattach gets the next.
    """

    def __init__(
        self, browsers: Sequence[FakeBrowser] | None = None, error: Exception | None = None
    ) -> None:
        self.browsers = [FakeBrowser()] if browsers is None else list(browsers)
        self.error = error
        self.connect_calls: list[str] = []
        self.detaches = 0

    @property
    def attaches(self) -> int:
        return len(self.connect_calls)

    async def connect(self, cdp_url: str) -> Connection:
        self.connect_calls.append(cdp_url)
        if self.error is not None:
            raise self.error
        browser = self.browsers[min(self.attaches - 1, len(self.browsers) - 1)]

        async def detach() -> None:
            self.detaches += 1

        return Connection(browser=browser, detach=detach)
