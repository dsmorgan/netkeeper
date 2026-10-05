"""The body tap (#200): the streamed copy of an answer whose own body Chrome could not keep.

:class:`netkeeper.linkedin.body_tap.BodyTap` is fed CDP event parameters by hand here,
the way the one read-only session ``BrowserRun._open_body_tap`` opens feeds it; the
stream call is a fake that records what it was asked. Nothing opens a socket.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
from typing import Any

import pytest

from netkeeper.linkedin import body_tap
from netkeeper.linkedin.body_tap import BodyTap
from netkeeper.linkedin.observe import ResponseMatch, ResponseRule

ORIGIN = "https://www.linkedin.com"
PAGINATION = f"{ORIGIN}/flagship-web/rsc-action/actions/pagination?sduiid=fake"
MATCH = ResponseMatch(
    origin=ORIGIN, rules=(ResponseRule("POST", "/flagship-web/rsc-action/actions/pagination"),)
)
ASK = '{"startIndex":20}'


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class Stream:
    """The fake ``Network.streamResourceContent``: what was buffered, or a refusal."""

    def __init__(self, buffered: bytes = b"", refuse: bool = False) -> None:
        self.buffered = buffered
        self.refuse = refuse
        self.asked: list[str] = []

    async def __call__(self, request_id: str) -> Mapping[str, Any]:
        self.asked.append(request_id)
        if self.refuse:
            raise RuntimeError("Request with the provided ID has already finished loading")
        return {"bufferedData": _b64(self.buffered)}


def _tap(stream: Stream, *, limit: int = 1024) -> BodyTap:
    return BodyTap(MATCH, stream=stream, max_body_bytes=limit)


def _answer(
    tap: BodyTap,
    rid: str,
    *,
    url: str = PAGINATION,
    method: str = "POST",
    post: str | None = ASK,
    chunks: tuple[bytes, ...] = (),
    end: str | None = "failed",
) -> None:
    request: dict[str, Any] = {"url": url, "method": method}
    if post is not None:
        request["postData"] = post
    tap.on_request({"requestId": rid, "request": request})
    tap.on_response({"requestId": rid, "response": {"url": url, "status": 200}})
    for chunk in chunks:
        tap.on_data({"requestId": rid, "data": _b64(chunk), "dataLength": len(chunk)})
    if end == "failed":
        tap.on_failed({"requestId": rid, "errorText": "net::ERR_ABORTED", "canceled": True})
    elif end == "finished":
        tap.on_finished({"requestId": rid})


async def test_an_aborted_answer_is_handed_over_whole_once() -> None:
    stream = Stream(buffered=b"0:first-")
    tap = _tap(stream)
    _answer(tap, "7.1", chunks=(b"second-", b"third"))
    got = await tap.take("post", PAGINATION, ASK, wait_s=1.0)
    assert got == b"0:first-second-third" and stream.asked == ["7.1"] and tap.handed == 1
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.01) is None


async def test_only_matching_answers_are_streamed() -> None:
    stream = Stream()
    tap = _tap(stream)
    _answer(tap, "1", url=f"{ORIGIN}/voyager/api/graphql")
    _answer(tap, "2", method="GET")
    _answer(tap, "3", url="https://www.linkedin.com.evil.example.test/flagship-web/x")
    await asyncio.sleep(0)
    assert stream.asked == []


async def test_a_refused_stream_is_no_copy() -> None:
    """Chrome refuses an answer that already finished: its own copy is whole."""
    tap = _tap(Stream(refuse=True))
    _answer(tap, "1", chunks=(b"ignored",), end="finished")
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) is None


async def test_an_answer_that_has_not_ended_is_no_copy() -> None:
    tap = _tap(Stream(buffered=b"part"))
    _answer(tap, "1", end=None)
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.05) is None


async def test_an_answer_past_the_body_limit_is_no_copy() -> None:
    tap = _tap(Stream(buffered=b"x" * 10), limit=16)
    _answer(tap, "1", chunks=(b"y" * 10,))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) is None


async def test_the_copy_is_matched_by_request_and_the_oldest_goes_first() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"first ask",))
    _answer(tap, "2", post='{"startIndex":30}', chunks=(b"another start",))
    _answer(tap, "3", chunks=(b"second ask",))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"first ask"
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"second ask"
    assert await tap.take("POST", PAGINATION, '{"startIndex":30}', wait_s=1.0) == b"another start"


async def test_a_read_that_succeeded_discards_its_copy() -> None:
    tap = _tap(Stream())
    _answer(tap, "1", chunks=(b"read fine",), end="finished")
    _answer(tap, "2", chunks=(b"lost",))
    tap.discard("POST", PAGINATION, ASK)
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"lost"


async def test_held_answers_are_bounded() -> None:
    tap = _tap(Stream())
    for index in range(body_tap.MAX_STREAMS + 5):
        _answer(tap, str(index), post=f'{{"startIndex":{index}}}', end=None)
    assert len(tap._streams) == body_tap.MAX_STREAMS
    assert "0" not in tap._streams and str(body_tap.MAX_STREAMS + 4) in tap._streams
    await tap.close()


async def test_malformed_events_are_ignored() -> None:
    tap = _tap(Stream())
    tap.on_request({})
    tap.on_request({"requestId": "1", "request": None})
    tap.on_response({"no": "id"})
    tap.on_data({"requestId": "unknown", "data": "!!"})
    tap.on_failed({})
    _answer(tap, "2", chunks=())
    tap.on_data({"requestId": "2", "data": 5})
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b""


async def test_close_detaches_once_and_stops_listening() -> None:
    detached: list[bool] = []

    async def detach() -> None:
        detached.append(True)

    tap = BodyTap(MATCH, stream=Stream(), detach=detach, max_body_bytes=1024)
    _answer(tap, "1", end=None)
    await tap.close()
    await tap.close()
    _answer(tap, "2", chunks=(b"after close",))
    assert detached == [True]
    assert await tap.take("POST", PAGINATION, ASK, wait_s=0.01) is None


async def test_a_detach_that_fails_is_quiet() -> None:
    async def detach() -> None:
        raise RuntimeError("Target closed")

    await BodyTap(MATCH, stream=Stream(), detach=detach, max_body_bytes=1024).close()


def test_the_tap_listens_to_the_network_events_only() -> None:
    tap = _tap(Stream())
    assert [event for event, _ in tap.handlers()] == [
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.dataReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    ]
    assert body_tap.MAX_STREAMS == 32


@pytest.mark.parametrize("bad", ["!!not base64!!", None, 12])
async def test_undecodable_buffered_data_is_empty(bad: object) -> None:
    class Odd(Stream):
        async def __call__(self, request_id: str) -> Mapping[str, Any]:
            return {"bufferedData": bad}

    tap = _tap(Odd())
    _answer(tap, "1", chunks=(b"rest",))
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"rest"


def test_the_tap_session_buffers_are_pinned() -> None:
    from netkeeper.linkedin import browser

    assert browser.TAP_RESOURCE_BUFFER_BYTES == 8 * 1024 * 1024
    assert browser.TAP_TOTAL_BUFFER_BYTES == 32 * 1024 * 1024


@pytest.mark.parametrize(
    ("failed", "whole"),
    [
        ({"canceled": True, "errorText": "net::ERR_ABORTED"}, True),
        ({"canceled": False, "errorText": "net::ERR_ABORTED"}, False),
        ({"canceled": True, "errorText": "net::ERR_CONNECTION_RESET"}, False),
        ({"errorText": "net::ERR_CONTENT_LENGTH_MISMATCH"}, False),
        ({}, False),
    ],
    ids=["page-cancel", "not-canceled", "reset", "length-mismatch", "no-reason"],
)
async def test_a_failed_stream_is_a_copy_only_when_the_page_cancelled_it(
    failed: dict[str, Any], whole: bool
) -> None:
    """#202 review: only a cancel by the page's own client (``canceled`` and
    ``net::ERR_ABORTED``) leaves a failed stream whole; any other failure may have
    cut it short."""
    tap = _tap(Stream(buffered=b"all of it"))
    _answer(tap, "1", end=None)
    tap.on_failed({"requestId": "1", **failed})
    got = await tap.take("POST", PAGINATION, ASK, wait_s=1.0)
    assert (got == b"all of it") is whole and (got is None) is not whole


async def test_a_finished_stream_is_a_copy() -> None:
    tap = _tap(Stream(buffered=b"whole"))
    _answer(tap, "1", end="finished")
    assert await tap.take("POST", PAGINATION, ASK, wait_s=1.0) == b"whole"


def test_the_one_whole_failure_is_pinned() -> None:
    assert body_tap.ABORTED_BY_PAGE == "net::ERR_ABORTED"


# --- #405: why a take had no copy, in fixed words -------------------------------------------


class _Stalled(Stream):
    """A ``Network.streamResourceContent`` that never answers within the wait."""

    async def __call__(self, request_id: str) -> Mapping[str, Any]:
        self.asked.append(request_id)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("case", "miss"),
    [
        ("unmatched", body_tap.MISS_NOT_MATCHED),
        ("refused", body_tap.MISS_NOT_STREAMED),
        ("stalled", body_tap.MISS_NOT_STARTED),
        ("open", body_tap.MISS_NOT_ENDED),
        ("reset", body_tap.MISS_FAILED),
        ("large", body_tap.MISS_TOO_LARGE),
        ("whole", None),
    ],
)
async def test_each_take_without_a_copy_says_why(case: str, miss: str | None) -> None:
    stream: Stream = _Stalled() if case == "stalled" else Stream(buffered=b"x" * 10)
    if case == "refused":
        stream = Stream(refuse=True)
    tap = _tap(stream, limit=16 if case == "large" else 1024)
    if case == "large":
        _answer(tap, "1", chunks=(b"y" * 10,))
    elif case == "refused":
        _answer(tap, "1", end="finished")
    elif case in ("stalled", "open", "reset"):
        _answer(tap, "1", end=None)
        if case == "reset":
            tap.on_failed({"requestId": "1", "errorText": "net::ERR_CONNECTION_RESET"})
    elif case == "whole":
        _answer(tap, "1")
    got = await tap.take("POST", PAGINATION, ASK, wait_s=0.05)
    assert tap.last_miss == miss and (got is None) is (miss is not None)
    await tap.close()
