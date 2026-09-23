"""``netkeeper.linkedin.fetch``: the in-page Voyager fetch and its gate (P2-01, #150).

Offline throughout: everything here drives ``PageVoyagerFetch`` against
``tests/browser_fakes.py``'s fake tab, never a real page. The opt-in smoke suite
(``tests/smoke/test_fetch_smoke.py``) is what proves the same class works against a
real Chrome and a served fixture.

The fixtures below are invented: no real LinkedIn cookie value, URN, or profile ever
appears here, and the assertions in the "cookie handling" section are what keep any
cookie value that *is* invented out of an exception message.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any
from urllib.parse import urlencode

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserRun
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.fetch import (
    LINKEDIN_ORIGIN,
    NotLinkedInOrigin,
    PageVoyagerFetch,
    VoyagerFetchError,
    VoyagerNotOk,
    parse_ok,
)
from netkeeper.linkedin.voyager import (
    CONNECTIONS_PATH,
    VoyagerFetch,
    VoyagerRequest,
    VoyagerResponse,
    build_headers,
    connections_query,
    parse_connections_page,
)

CDP_URL = "http://127.0.0.1:9222"
LOOPBACK_ORIGIN = "http://127.0.0.1:52341"

# Invented, and never a value any assertion below expects to see leave this module.
RAW_JSESSIONID = '"ajax:MARKER-CSRF-VALUE-0000"'
STRIPPED_CSRF = "ajax:MARKER-CSRF-VALUE-0000"


def jsessionid_cookie(
    *, domain: str = ".www.linkedin.com", value: str = RAW_JSESSIONID
) -> dict[str, str]:
    return {"name": "JSESSIONID", "value": value, "domain": domain, "path": "/"}


def make_context(
    cookies: list[dict[str, Any]] | None = None, evaluate_result: object = None
) -> FakeContext:
    return FakeContext(
        cookies=[jsessionid_cookie()] if cookies is None else cookies,
        evaluate_result=evaluate_result,
    )


def run_with(context: FakeContext) -> AbstractAsyncContextManager[BrowserRun]:
    connector = FakeConnector([FakeBrowser([context])])
    provider = AttachBrowserProvider(CDP_URL, connector=connector)
    return provider.run()


CONNECTIONS_RESPONSE = {
    "data": {
        "elements": [{"*connectedMemberResolutionResult": "urn:li:fsd_profile:MARKERURN"}],
        "paging": {"start": 0, "count": 1, "total": 1},
    },
    "included": [
        {
            "entityUrn": "urn:li:fsd_profile:MARKERURN",
            "publicIdentifier": "rehearsal-marker-person",
            "firstName": "Marker",
            "lastName": "Person",
            "headline": "Invented for a test",
        }
    ],
}


def fetch_result(*, status: int = 200, body: object = "", url: str = "") -> dict[str, object]:
    return {"status": status, "body": body, "url": url}


# --- origin restriction --------------------------------------------------------


async def test_the_default_origin_is_linkedin() -> None:
    async with run_with(make_context()) as run:
        fetch = PageVoyagerFetch(run)
        assert fetch.origin == LINKEDIN_ORIGIN


async def test_an_arbitrary_host_is_refused_at_construction() -> None:
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin, match="linkedin"):
            PageVoyagerFetch(run, origin="https://evil.example.invalid")


async def test_a_malformed_origin_is_refused_not_crashed_on() -> None:
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin):
            PageVoyagerFetch(run, origin="http://[::1")


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:9999", "http://localhost:9999", "http://[::1]:9999"]
)
async def test_a_loopback_origin_is_allowed_for_tests(origin: str) -> None:
    async with run_with(make_context()) as run:
        fetch = PageVoyagerFetch(run, origin=origin)
        assert fetch.origin == origin


async def test_a_loopback_looking_host_over_an_unlisted_scheme_is_still_refused() -> None:
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin):
            PageVoyagerFetch(run, origin="ftp://127.0.0.1:9999")


# --- path restriction ------------------------------------------------------------


async def test_a_non_voyager_path_is_refused() -> None:
    async with run_with(make_context()) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(ValueError, match="Voyager path"):
            await fetch(VoyagerRequest(path="/feed/update/urn:li:activity:1"))


# --- reading the live csrf cookie -------------------------------------------------


async def test_no_jsessionid_cookie_is_a_fetch_error() -> None:
    context = make_context(cookies=[], evaluate_result=fetch_result())
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError, match="JSESSIONID"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))


async def test_a_jsessionid_for_an_unrelated_domain_does_not_count() -> None:
    context = make_context(
        cookies=[jsessionid_cookie(domain=".example.invalid")], evaluate_result=fetch_result()
    )
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError, match="JSESSIONID"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))


async def test_an_unreadable_cookie_jar_is_a_fetch_error_not_a_crash() -> None:
    context = make_context(evaluate_result=fetch_result())
    context.cookie_error = RuntimeError("Protocol error")
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError, match="cookie jar"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))


async def test_a_loopback_fetch_matches_the_cookie_by_the_loopback_host_itself() -> None:
    """The domain suffix a csrf cookie must match tracks ``origin``, not a hardcoded host."""
    context = make_context(
        cookies=[jsessionid_cookie(domain="127.0.0.1")],
        evaluate_result=fetch_result(status=200, body="{}", url=f"{LOOPBACK_ORIGIN}/x"),
    )
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run, origin=LOOPBACK_ORIGIN)
        response = await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
    assert response.status == 200


async def test_the_cookie_value_never_reaches_an_exception_message() -> None:
    """A fetch failure must never echo what the in-page script actually returned."""
    context = make_context(evaluate_result=f"broken shape carrying {STRIPPED_CSRF}")
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError) as excinfo:
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
    assert STRIPPED_CSRF not in str(excinfo.value)


async def test_a_real_evaluate_failure_never_leaks_the_cookie_value_either() -> None:
    """Playwright's own error, not this module's -- it must not carry the value either.

    The csrf-token value is embedded in the very script ``page.evaluate`` runs (see
    ``_fetch_expression``), so a driver error naming the failing call is exactly the
    place a value could leak if this module ever wrapped or re-raised it carelessly.
    It does not: the failure propagates unchanged, unwrapped, carrying nothing this
    module added -- which is what this asserts for the one value it must never leak.
    """
    context = make_context(evaluate_result=fetch_result())
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        # Open the tab first, so the failure can be armed on it before the fetch
        # reaches the same (idempotent) `ensure_page()` call internally.
        await run.ensure_page()
        context.pages[0].fail_next_evaluate(RuntimeError("Execution context was destroyed"))

        with pytest.raises(RuntimeError, match="Execution context") as excinfo:
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert STRIPPED_CSRF not in str(excinfo.value)


# --- the request the in-page script actually carries ------------------------------


async def test_the_fetch_carries_exactly_what_build_headers_produces() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        await fetch(VoyagerRequest(path=CONNECTIONS_PATH, query=connections_query(start=0)))
        page = context.pages[0]

    expected_headers = build_headers(RAW_JSESSIONID)
    expected_url = f"{LINKEDIN_ORIGIN}{CONNECTIONS_PATH}?{urlencode(connections_query(start=0))}"
    sent = page.evaluate_calls[-1]
    assert json.dumps(expected_headers) in sent
    assert json.dumps(expected_url) in sent
    # The still-quoted raw cookie value, JSON-encoded as a string in its own right
    # (its embedded quotes escaped), is what the header would carry if the quotes
    # were never stripped; it must not appear anywhere near the csrf-token value.
    unstripped = json.dumps(RAW_JSESSIONID)
    assert unstripped not in sent, "the quotes must be stripped from the JSESSIONID cookie"


async def test_a_callers_extra_header_overrides_build_headers_own_value() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        await fetch(VoyagerRequest(path=CONNECTIONS_PATH, headers={"accept": "text/plain"}))
        page = context.pages[0]

    assert json.dumps("text/plain") in page.evaluate_calls[-1]


async def test_satisfies_the_voyager_fetch_protocol() -> None:
    async with run_with(make_context()) as run:
        fetch = PageVoyagerFetch(run)
        assert isinstance(fetch, VoyagerFetch)


async def test_the_fetch_result_round_trips_into_a_parser() -> None:
    body = json.dumps(CONNECTIONS_RESPONSE)
    context = make_context(
        evaluate_result=fetch_result(
            status=200, body=body, url=f"{LINKEDIN_ORIGIN}{CONNECTIONS_PATH}"
        )
    )
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        response = await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert response.status == 200
    assert response.body == body
    page = parse_ok(response, parse_connections_page)
    assert page.connections[0].public_id == "rehearsal-marker-person"


# --- shape validation on what the script returned -----------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not a mapping",
        {"status": "200", "body": "x", "url": "x"},
        {"status": 200, "body": 5, "url": "x"},
        {"status": 200, "body": "x", "url": 5},
        {"status": True, "body": "x", "url": "x"},  # bool must not pass as an int status
        {},
    ],
)
async def test_a_malformed_evaluate_result_is_a_fetch_error(raw: object) -> None:
    context = make_context(evaluate_result=raw)
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))


# --- the gate: parse_ok -------------------------------------------------------------


def test_parse_ok_parses_an_ok_response() -> None:
    response = VoyagerResponse(status=200, body='{"a": 1}', final_url="https://www.linkedin.com/x")

    result = parse_ok(response, json.loads)

    assert result == {"a": 1}


@pytest.mark.parametrize(
    ("status", "url", "body", "expected"),
    [
        (200, "https://www.linkedin.com/checkpoint/challenge", "{}", Outcome.CHECKPOINT),
        (401, "https://www.linkedin.com/voyager/api/x", "{}", Outcome.LOGGED_OUT),
        (429, "https://www.linkedin.com/voyager/api/x", "{}", Outcome.THROTTLED),
        (404, "https://www.linkedin.com/voyager/api/x", "{}", Outcome.NOT_FOUND),
        (
            200,
            "https://www.linkedin.com/voyager/api/x",
            "<html>not json</html>",
            Outcome.ROUTE_CHANGED,
        ),
    ],
)
def test_parse_ok_never_calls_the_parser_on_anything_but_ok(
    status: int, url: str, body: str, expected: Outcome
) -> None:
    response = VoyagerResponse(status=status, body=body, final_url=url)
    calls: list[str] = []

    def spy(text: str) -> str:
        calls.append(text)
        return text

    with pytest.raises(VoyagerNotOk) as excinfo:
        parse_ok(response, spy)

    assert excinfo.value.outcome is expected
    assert excinfo.value.response is response
    assert calls == [], "the gate let a non-Ok response reach the parser"
