"""The observation seam: read what the page loads, alter nothing, log no body (ADR 0006).

:class:`netkeeper.linkedin.observe.Observation` against :mod:`flagship_site`'s
listening tab, whose responses have a request side that can only be read. Nothing
here opens a socket.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

import pytest
from flagship_site import FakeRequest, FakeResponse, FlagshipSite, ListeningTab
from run_fakes import fake_provider

from netkeeper.linkedin import observe
from netkeeper.linkedin.observe import (
    FAILURE_REDIRECT,
    FAILURE_TIMEOUT,
    FAILURE_TOO_LARGE,
    FAILURE_UNREADABLE,
    Observation,
    ObservationFailed,
    ObservationLimits,
    ResponseMatch,
    ResponseRule,
)

ORIGIN = "https://www.linkedin.com"
PAGINATION = f"{ORIGIN}/flagship-web/rsc-action/actions/pagination?sduiid=fake"
SECRET = b"Priya Okafor, priya.okafor@example.test"
MATCH = ResponseMatch(
    origin=ORIGIN,
    rules=(
        ResponseRule("POST", "/flagship-web/rsc-action/actions/pagination"),
        ResponseRule("get", "/mynetwork/invite-connect/connections/"),
    ),
)


def _tab() -> ListeningTab:
    return ListeningTab(FlagshipSite())


def _response(
    url: str = PAGINATION,
    *,
    method: str = "POST",
    status: int = 200,
    body: bytes = SECRET,
    headers: Mapping[str, str] | None = None,
    body_error: Exception | None = None,
    body_delay: Callable[[], Awaitable[None]] | None = None,
) -> FakeResponse:
    return FakeResponse(
        url,
        status,
        body,
        FakeRequest(method, "fetch", '{"startIndex":10}'),
        headers=headers,
        body_error=body_error,
        body_delay=body_delay,
    )


async def _started(tab: ListeningTab, limits: ObservationLimits | None = None) -> Observation:
    observation = Observation(MATCH, tab, limits)
    observation.start()
    return observation


# --- what is kept ---------------------------------------------------------------------------


async def test_only_matching_responses_are_kept_in_arrival_order() -> None:
    tab = _tab()
    observation = await _started(tab)
    tab.emit(_response(body=b"one"))
    tab.emit(_response(f"{ORIGIN}/voyager/api/graphql"))  # another path
    tab.emit(_response(method="GET"))  # another method
    tab.emit(
        _response(
            "https://www.linkedin.com.evil.example.test/flagship-web/rsc-action/actions/pagination"
        )
    )
    tab.emit(_response("http://www.linkedin.com/flagship-web/rsc-action/actions/pagination"))
    tab.emit(_response(f"{ORIGIN}/mynetwork/invite-connect/connections", method="GET", body=b"two"))
    first = await observation.next(1.0)
    second = await observation.next(1.0)
    assert first is not None and first.body == b"one" and first.request_body == '{"startIndex":10}'
    assert second is not None and second.body == b"two"
    assert await observation.next(0.0) is None
    assert observation.kept == 2


async def test_a_slow_body_is_never_overtaken_by_a_later_one() -> None:
    tab = _tab()
    observation = await _started(tab)
    release = asyncio.Event()

    async def slow() -> None:
        await release.wait()

    tab.emit(_response(body=b"first", body_delay=slow))
    tab.emit(_response(body=b"second"))
    waiting = asyncio.create_task(observation.next(5.0))
    await asyncio.sleep(0.01)
    assert not waiting.done()
    release.set()
    assert (await waiting).body == b"first"  # type: ignore[union-attr]
    assert (await observation.next(1.0)).body == b"second"  # type: ignore[union-attr]


async def test_nothing_arriving_is_none_after_the_timeout() -> None:
    observation = await _started(_tab())
    assert await observation.next(0.01) is None


# --- bounded ---------------------------------------------------------------------------------


async def test_too_many_unread_responses_fail_the_observation_rather_than_drop_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tab = _tab()
    observation = await _started(tab, ObservationLimits(max_pending=2))
    for _ in range(3):
        tab.emit(_response())
    assert observation.overflowed
    with pytest.raises(ObservationFailed, match="dropped"):
        await observation.next(1.0)
    assert SECRET.decode() not in caplog.text


async def test_a_body_past_the_limit_is_not_kept() -> None:
    tab = _tab()
    observation = await _started(tab, ObservationLimits(max_body_bytes=8))
    tab.emit(_response(body=b"x" * 9))
    kept = await observation.next(1.0)
    assert kept is not None and kept.body is None and kept.failure == FAILURE_TOO_LARGE


async def test_a_body_that_never_arrives_times_out() -> None:
    tab = _tab()
    observation = await _started(tab, ObservationLimits(body_timeout_s=0.02))

    async def never() -> None:
        await asyncio.Event().wait()

    tab.emit(_response(body_delay=never))
    kept = await observation.next(1.0)
    assert kept is not None and kept.body is None and kept.failure == FAILURE_TIMEOUT


async def test_a_body_that_fails_is_a_fixed_phrase_never_the_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tab = _tab()
    observation = await _started(tab)
    tab.emit(_response(body_error=RuntimeError(f"failed for {PAGINATION} {SECRET!r}")))
    kept = await observation.next(1.0)
    assert kept is not None and kept.failure == FAILURE_UNREADABLE
    assert "example.test" not in caplog.text and "sduiid" not in caplog.text
    # #197: the cause is the class and a fixed category, never the message.
    assert kept.cause == "RuntimeError (network error)"
    assert "RuntimeError (network error)" in caplog.text


async def test_only_an_unreadable_body_carries_a_cause() -> None:
    tab = _tab()
    observation = await _started(tab, ObservationLimits(max_body_bytes=8))
    tab.emit(_response(body=b"short"))
    tab.emit(_response(body=b"x" * 9))
    read, too_large = await observation.next(1.0), await observation.next(1.0)
    assert read is not None and read.cause is None
    assert too_large is not None and too_large.cause is None


class ProtocolError(Exception):
    """Stands in for a Playwright error class: only its name is ever kept."""


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (
            "Protocol error (Network.getResponseBody): No resource with given identifier"
            " found for https://www.linkedin.com/in/fake-slug/",
            "no resource",
        ),
        ("No data found for resource with given identifier", "no data"),
        ("Request content was evicted from inspector cache", "evicted"),
        ("net::ERR_ABORTED; https://www.linkedin.com/in/fake-slug/", "aborted"),
        ("The request was canceled", "aborted"),
        ("Target page, context or browser has been closed", "closed"),
        ("Target closed", "closed"),
        ("net::ERR_CONTENT_LENGTH_MISMATCH", "network error"),
        ("Response body is unavailable", "unclassified"),
        ("", "unclassified"),
    ],
)
def test_an_unreadable_cause_is_a_class_and_a_fixed_category(message: str, category: str) -> None:
    cause = observe.unreadable_cause(ProtocolError(message))
    assert cause == f"ProtocolError ({category})"
    assert "linkedin" not in cause and "fake-slug" not in cause


def test_an_unreadable_cause_survives_an_exception_that_cannot_be_printed() -> None:
    class Unprintable(Exception):
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert observe.unreadable_cause(Unprintable()) == "Unprintable (unclassified)"


# --- #200: fixed diagnostics for a body that could not be read ------------------------------


def _ended(url: str = PAGINATION, failure: str | None = None) -> FakeRequest:
    return FakeRequest("POST", "fetch", '{"startIndex":10}', url=url, failure=failure)


async def test_a_body_lost_after_its_request_failed_says_so_in_fixed_words() -> None:
    """What the lab reproduced: the page's client aborted a streamed answer after
    reading it; Chrome says "No data found", and the request failed as aborted."""
    tab = _tab()
    observation = await _started(tab)
    request = _ended(failure="net::ERR_ABORTED https://www.linkedin.com/in/fake-slug/")

    async def aborted_first() -> None:
        tab.emit_request_end(observe.REQUEST_FAILED_EVENT, request)

    response = FakeResponse(
        PAGINATION,
        200,
        b"",
        request,
        headers={
            "content-type": "text/x-component; charset=utf-8",
            "transfer-encoding": "chunked",
        },
        body_error=ProtocolError("No data found for resource with given identifier"),
        body_delay=aborted_first,
        from_service_worker=False,
    )
    tab.emit(response)
    kept = await observation.next(1.0)
    assert kept is not None and kept.failure == FAILURE_UNREADABLE
    assert kept.cause == "ProtocolError (no data)"
    diagnostics = kept.diagnostics
    assert diagnostics is not None
    assert (
        diagnostics.from_service_worker,
        diagnostics.status,
        diagnostics.content_type,
        diagnostics.content_length,
        diagnostics.transfer_encoding,
        diagnostics.request_end,
        diagnostics.request_failure,
        diagnostics.reads_in_flight,
    ) == (False, 200, "x-component", None, "chunked", "failed", "aborted", 0)
    assert 0 <= diagnostics.read_after_ms <= diagnostics.failed_after_ms
    line = diagnostics.describe()
    assert "request=failed (aborted)" in line and "service_worker=no" in line
    assert "fake-slug" not in line and "linkedin" not in line
    assert observation.unreadable == [diagnostics]
    # #405: an observation without a tap says so.
    assert diagnostics.streamed_miss == observe.NO_TAP and "streamed_miss='no body tap'" in line


async def test_a_finished_request_and_one_with_no_event_are_told_apart() -> None:
    tab = _tab()
    observation = await _started(tab)
    finished = _ended()
    tab.emit_request_end(observe.REQUEST_FINISHED_EVENT, finished)
    tab.emit(
        FakeResponse(
            PAGINATION,
            200,
            b"",
            finished,
            headers={"content-type": "application/x-invented-type", "content-length": "1234"},
            body_error=ProtocolError("No resource with given identifier found"),
            from_service_worker=True,
        )
    )
    tab.emit(
        FakeResponse(
            PAGINATION,
            200,
            b"",
            _ended(),
            headers={"content-length": "not a number"},
            body_error=ProtocolError("gone"),
        )
    )
    first = await observation.next(1.0)
    second = await observation.next(1.0)
    assert first is not None and first.diagnostics is not None
    assert (
        first.diagnostics.request_end,
        first.diagnostics.request_failure,
        first.diagnostics.content_type,
        first.diagnostics.content_length,
        first.diagnostics.from_service_worker,
    ) == ("finished", None, "other", 1234, True)
    assert second is not None and second.diagnostics is not None
    assert (
        second.diagnostics.request_end,
        second.diagnostics.content_type,
        second.diagnostics.content_length,
        second.diagnostics.transfer_encoding,
    ) == ("neither", "none", None, "none")


async def test_reads_in_flight_counts_the_other_reads_under_way() -> None:
    tab = _tab()
    observation = await _started(tab)
    release = asyncio.Event()

    async def slow() -> None:
        await release.wait()

    tab.emit(_response(body=b"one", body_delay=slow))
    tab.emit(_response(body=b"two", body_delay=slow))
    tab.emit(_response(body_error=ProtocolError("No data found")))
    await asyncio.sleep(0.01)
    release.set()
    kept = [await observation.next(1.0) for _ in range(3)]
    lost = kept[2]
    assert lost is not None and lost.diagnostics is not None
    assert lost.diagnostics.reads_in_flight == 2


async def test_a_request_event_for_another_url_or_a_broken_request_is_ignored() -> None:
    tab = _tab()
    observation = await _started(tab)
    other = _ended(url=f"{ORIGIN}/voyager/api/graphql", failure="net::ERR_ABORTED")
    tab.emit_request_end(observe.REQUEST_FAILED_EVENT, other)

    class Broken:
        @property
        def method(self) -> str:
            raise RuntimeError("gone")

    tab.emit_request_end(observe.REQUEST_FAILED_EVENT, Broken())  # type: ignore[arg-type]
    tab.emit(FakeResponse(PAGINATION, 200, b"", other, body_error=ProtocolError("x")))
    kept = await observation.next(1.0)
    assert kept is not None and kept.diagnostics is not None
    assert kept.diagnostics.request_end == "neither"


async def test_the_remembered_request_ends_are_bounded() -> None:
    tab = _tab()
    observation = await _started(tab)
    requests = [_ended() for _ in range(observe.MAX_REMEMBERED_ENDS + 10)]
    for request in requests:
        tab.emit_request_end(observe.REQUEST_FINISHED_EVENT, request)
    assert len(observation._ends) == observe.MAX_REMEMBERED_ENDS
    assert requests[0] not in observation._ends and requests[-1] in observation._ends
    tab.emit(FakeResponse(PAGINATION, 200, b"ok", requests[-1]))
    await observation.next(1.0)
    assert requests[-1] not in observation._ends  # forgotten once its body was read


async def test_close_logs_one_summary_in_fixed_words(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    tab = _tab()
    observation = await _started(tab)
    request = _ended(failure="net::ERR_ABORTED https://www.linkedin.com/in/fake-slug/")
    tab.emit_request_end(observe.REQUEST_FAILED_EVENT, request)
    tab.emit(
        FakeResponse(
            PAGINATION,
            200,
            b"",
            request,
            headers={"transfer-encoding": "chunked"},
            body_error=ProtocolError("No data found for https://www.linkedin.com/in/fake-slug/"),
        )
    )
    tab.emit(_response(body=b"fine"))
    await observation.next(1.0)
    await observation.next(1.0)
    await observation.close()
    (summary,) = [r.getMessage() for r in caplog.records if "answers_kept=" in r.getMessage()]
    assert summary == (
        "observation: answers_kept=2 bodies_unreadable=1"
        " unreadable_requests=[failed (aborted): 1] unreadable_chunked=1"
        " unreadable_from_service_worker=0 unreadable_with_streamed_copy=0"
    )
    assert "fake-slug" not in caplog.text
    for event in (
        observe.RESPONSE_EVENT,
        observe.REQUEST_FINISHED_EVENT,
        observe.REQUEST_FAILED_EVENT,
    ):
        assert tab.listeners[event] == []


async def test_an_observation_that_kept_nothing_logs_no_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="netkeeper")
    observation = await _started(_tab())
    await observation.close()
    assert "answers_kept" not in caplog.text


# --- #200 Part B: the body tap's streamed copy -----------------------------------------------


class _Tap:
    """Stands in for BodyTap: records takes and discards, hands out given copies."""

    def __init__(self, copies: list[bytes | None]) -> None:
        self.copies = copies
        self.calls: list[tuple[str, str, str | None]] = []
        self.closed = False
        self.last_miss: str | None = None

    async def take(
        self, method: str, url: str, post_data: str | None, *, wait_s: float
    ) -> bytes | None:
        self.calls.append(("take", method, post_data))
        assert wait_s == observe.STREAMED_WAIT_S
        copy = self.copies.pop(0) if self.copies else None
        self.last_miss = "never matched" if copy is None else None
        return copy

    def discard(self, method: str, url: str, post_data: str | None) -> None:
        self.calls.append(("discard", method, post_data))

    async def close(self) -> None:
        self.closed = True


async def test_a_failed_read_hands_over_the_streamed_copy_never_as_the_body() -> None:
    tab = _tab()
    tap = _Tap([b"the copy"])
    observation = Observation(MATCH, tab, None, tap)  # type: ignore[arg-type]
    observation.start()
    tab.emit(_response(body=b"read fine"))
    tab.emit(_response(body_error=ProtocolError("No data found")))
    first = await observation.next(1.0)
    second = await observation.next(1.0)
    assert first is not None and first.body == b"read fine" and first.streamed is None
    assert second is not None and second.body is None and second.streamed == b"the copy"
    assert second.diagnostics is not None and second.diagnostics.streamed_bytes == 8
    assert "streamed_bytes=8" in second.diagnostics.describe()
    assert "the copy" not in repr(second)
    assert tap.calls == [
        ("discard", "POST", '{"startIndex":10}'),
        ("take", "POST", '{"startIndex":10}'),
    ]
    await observation.close()
    assert tap.closed


async def test_without_a_copy_the_diagnostics_say_none() -> None:
    tab = _tab()
    observation = Observation(MATCH, tab, None, _Tap([]))  # type: ignore[arg-type]
    observation.start()
    tab.emit(_response(body_error=ProtocolError("No data found")))
    kept = await observation.next(1.0)
    assert kept is not None and kept.streamed is None and kept.diagnostics is not None
    assert "streamed_bytes=none" in kept.diagnostics.describe()
    # #405: and why the tap had none, in its fixed words.
    assert kept.diagnostics.streamed_miss == "never matched"
    assert "streamed_miss='never matched'" in kept.diagnostics.describe()


async def test_a_redirect_keeps_its_location_and_reads_no_body() -> None:
    tab = _tab()
    observation = await _started(tab)
    response = _response(
        status=302,
        headers={"location": "https://www.linkedin.com/login"},
        body_error=AssertionError("a redirect's body was read"),
    )
    tab.emit(response)
    kept = await observation.next(1.0)
    assert kept is not None and (kept.status, kept.body, kept.failure) == (
        302,
        None,
        FAILURE_REDIRECT,
    )
    assert kept.location == "https://www.linkedin.com/login"


# --- read-only, and quiet ------------------------------------------------------------------


async def test_the_request_side_is_only_read() -> None:
    """The observation reads the method, the resource type, and the body the page sent.
    ``FakeRequest`` has nothing else -- no method at all -- and the reads are counted."""
    tab = _tab()
    observation = await _started(tab)
    response = _response()
    tab.emit(response)
    await observation.next(1.0)
    assert set(response.request.reads) <= {"method", "resource_type", "post_data"}
    assert not [
        name
        for name in dir(FakeRequest)
        if not name.startswith("_") and callable(getattr(FakeRequest, name))
    ]


async def test_a_kept_response_never_shows_its_body_in_a_repr_or_a_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tab = _tab()
    observation = await _started(tab)
    tab.emit(_response())
    kept = await observation.next(1.0)
    assert kept is not None
    assert SECRET.decode() not in repr(kept) and "startIndex" not in repr(kept)
    assert "sduiid" not in repr(kept)  # the url is left out too: a profile url names a person
    assert SECRET.decode() not in caplog.text


async def test_close_stops_listening_and_drops_what_is_still_being_read() -> None:
    tab = _tab()
    observation = await _started(tab)

    async def never() -> None:
        await asyncio.Event().wait()

    tab.emit(_response(body_delay=never))
    assert tab.listeners["response"]
    await observation.close()
    assert tab.listeners["response"] == []
    tab.emit(_response())
    assert await observation.next(0.0) is None
    await observation.close()  # idempotent
    with pytest.raises(ObservationFailed):
        observation.start()


def test_a_match_refuses_what_it_could_not_compare() -> None:
    with pytest.raises(ValueError):
        ResponseMatch(origin="https://www.linkedin.com/path", rules=(ResponseRule("GET", "/"),))
    with pytest.raises(ValueError):
        ResponseMatch(origin="https://user@www.linkedin.com", rules=(ResponseRule("GET", "/"),))
    with pytest.raises(ValueError):
        ResponseMatch(origin=ORIGIN, rules=())
    with pytest.raises(ValueError):
        ResponseRule("GET", "no-slash")
    assert not MATCH.matches("POST", "not a url at all")
    assert not MATCH.matches("POST", "http://[::1")  # urlsplit refuses it


def test_the_limits_are_pinned() -> None:
    limits = ObservationLimits()
    assert (limits.max_pending, limits.max_body_bytes, limits.body_timeout_s) == (
        16,
        8 * 1024 * 1024,
        30.0,
    )
    assert observe.RESPONSE_EVENT == "response"
    assert observe.REQUEST_FINISHED_EVENT == "requestfinished"
    assert observe.REQUEST_FAILED_EVENT == "requestfailed"
    assert observe.MAX_REMEMBERED_ENDS == 256
    assert observe.STREAMED_WAIT_S == 2.0
    with pytest.raises(ValueError):
        ObservationLimits(max_pending=0)


# --- through BrowserRun -----------------------------------------------------------------


async def test_a_run_observes_its_own_tab_and_closes_the_observation_with_the_tab() -> None:
    site = FlagshipSite()
    provider, _ = fake_provider(site)
    async with provider.run("account-1") as run:
        observation = await run.observe(MATCH)
        (tab,) = site.pages
        assert isinstance(tab, ListeningTab)
        assert observation.page is cast(object, tab) and tab.listeners["response"]
    assert tab.listeners["response"] == [] and tab.is_closed()


def test_a_prefix_rule_keeps_every_path_under_it_and_nothing_beside_it() -> None:
    """#190: a profile a slug redirects to has a path nobody knew before navigating."""
    match = ResponseMatch(origin=ORIGIN, rules=(ResponseRule("GET", "/in/", prefix=True),))
    assert match.matches("GET", f"{ORIGIN}/in/someone-fake/")
    assert match.matches("GET", f"{ORIGIN}/in/renamed-fake/?trk=x")
    assert not match.matches("GET", f"{ORIGIN}/in/")  # the prefix alone is no profile
    assert not match.matches("GET", f"{ORIGIN}/inbox/")
    assert not match.matches("GET", f"{ORIGIN}/feed/in/x/")
    assert not match.matches("POST", f"{ORIGIN}/in/someone-fake/")
    assert not match.matches("GET", "https://evil.example.test/in/someone-fake/")
    with pytest.raises(ValueError, match="ends with '/'"):
        ResponseRule("GET", "/in", prefix=True)
    exact = ResponseRule("GET", "/in/")
    assert not exact.matches("GET", "/in/someone-fake/")
