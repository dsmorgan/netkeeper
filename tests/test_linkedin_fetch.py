"""``netkeeper.linkedin.fetch``: the in-page Voyager fetch and its gate (P2-01, #150).

Offline throughout: everything here drives ``PageVoyagerFetch`` against
``tests/browser_fakes.py``'s fake tab, never a real page. The fakes cannot execute
the JavaScript this module hands to ``page.evaluate`` -- they return whatever
``context.evaluate_result`` is set to, or raise whatever ``fail_next_evaluate`` was
armed with, regardless of the script's actual text -- so the tests below split into
two kinds: ones that drive the *Python* side (origin checks, header composition, the
in-page failure translated into a friendly error, response-shape validation, the
``parse_ok`` gate) end to end, and structural checks on the generated script's text
for the logic that only a real browser can execute (reading ``document.cookie``,
refusing an empty token). The opt-in smoke suite (``tests/smoke/test_fetch_smoke.py``)
is what proves the script itself, run by a real Chrome, does the right thing.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from urllib.parse import urlencode

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext

from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserRun
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.fetch import (
    _NO_CSRF_MARKER,
    _ORIGIN_MISMATCH_MARKER,
    LINKEDIN_ORIGIN,
    NotLinkedInOrigin,
    PageVoyagerFetch,
    VoyagerFetchError,
    VoyagerNotOk,
    _base_headers,
    _fetch_expression,
    parse_ok,
)
from netkeeper.linkedin.voyager import (
    CONNECTIONS_PATH,
    CSRF_HEADER_NAME,
    VoyagerFetch,
    VoyagerRequest,
    VoyagerResponse,
    build_headers,
    connections_query,
    parse_connections_page,
)

CDP_URL = "http://127.0.0.1:9222"
LOOPBACK_ORIGIN = "http://127.0.0.1:52341"
OTHER_LOOPBACK_ORIGIN = "http://127.0.0.1:9"


def make_context(evaluate_result: object = None) -> FakeContext:
    return FakeContext(evaluate_result=evaluate_result)


def run_with(context: FakeContext) -> AbstractAsyncContextManager[BrowserRun]:
    connector = FakeConnector([FakeBrowser([context])])
    provider = AttachBrowserProvider(CDP_URL, connector=connector)
    return provider.run()


async def on_origin(run: BrowserRun, origin: str = LINKEDIN_ORIGIN) -> None:
    """Put the run's tab on ``origin``, which every real fetch call now requires (F2a)."""
    await run.goto(origin)


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


# --- origin restriction (construction time) -------------------------------------


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


async def test_an_http_downgrade_of_the_linkedin_origin_is_refused() -> None:
    """F3/N4: the production origin is fixed to https. A bare scheme swap must not pass."""
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin):
            PageVoyagerFetch(run, origin="http://www.linkedin.com")


async def test_the_whatwg_backslash_userinfo_trick_is_refused() -> None:
    """F3: the exact url a reviewer showed reads as loopback to urlsplit, LinkedIn to Chrome."""
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin):
            PageVoyagerFetch(run, origin="http://www.linkedin.com\\@127.0.0.1:8080")


async def test_a_substring_loopback_lookalike_host_is_refused() -> None:
    """F3/N3: 'localhost.evil.example' must not pass because it contains 'localhost'."""
    async with run_with(make_context()) as run:
        with pytest.raises(NotLinkedInOrigin):
            PageVoyagerFetch(run, origin="http://localhost.evil.example:9999")


# --- path restriction (never touches the page) ------------------------------------


async def test_a_non_voyager_path_is_refused() -> None:
    async with run_with(make_context()) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(ValueError, match="Voyager path"):
            await fetch(VoyagerRequest(path="/feed/update/urn:li:activity:1"))


# --- the page must actually be on this instance's origin (F2a) --------------------


async def test_a_fetch_before_the_tab_is_ever_navigated_is_refused() -> None:
    """A fresh tab is on about:blank -- nothing in this module may run a script there."""
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError, match="not on"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
        # The whole point: nothing was ever handed to the page.
        assert context.pages[0].evaluate_calls == []


async def test_a_fetch_while_the_tab_is_on_a_different_loopback_port_is_refused() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        await on_origin(run, OTHER_LOOPBACK_ORIGIN)
        fetch = PageVoyagerFetch(run, origin=LOOPBACK_ORIGIN)
        with pytest.raises(VoyagerFetchError, match="not on"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
        assert context.pages[0].evaluate_calls == []


async def test_a_fetch_after_navigating_to_the_matching_origin_proceeds() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        await on_origin(run, LOOPBACK_ORIGIN)
        fetch = PageVoyagerFetch(run, origin=LOOPBACK_ORIGIN)
        await fetch(VoyagerRequest(path=CONNECTIONS_PATH))
        assert len(context.pages[0].evaluate_calls) == 1


# --- the in-page failure modes, translated without leaking anything (F2b, F6) -------


async def test_no_readable_csrf_cookie_in_the_page_is_a_friendly_fetch_error() -> None:
    """What a real page throws when document.cookie has no JSESSIONID, simulated."""
    context = make_context()
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        context.pages[0].fail_next_evaluate(RuntimeError(_NO_CSRF_MARKER))

        with pytest.raises(VoyagerFetchError, match="JSESSIONID") as excinfo:
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert "log in to LinkedIn" in str(excinfo.value)


async def test_a_generic_in_page_failure_is_wrapped_into_a_fixed_message() -> None:
    """F6: a cross-origin redirect's 'Failed to fetch', or any other in-page failure.

    This replaces a tautological version of this test (#168 review, F6): asserting
    only that some marker the test itself chose is absent proves nothing about
    what the module actually does with the failure. This asserts the *positive*
    claim instead -- the raised message is one exact, fixed string, built from no
    part of the underlying failure -- so a future change that starts interpolating
    the driver's own exception text into the message fails this test even if the
    interpolated text happens not to contain whatever marker a test chose.
    """
    context = make_context()
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        context.pages[0].fail_next_evaluate(
            TypeError("Failed to fetch: net::ERR_FAILED (a cross-origin redirect)")
        )

        with pytest.raises(VoyagerFetchError) as excinfo:
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert str(excinfo.value) == (
        "the in-page fetch failed (a network error, a cross-origin redirect, or the"
        " page navigating away mid-request)"
    )
    assert "ERR_FAILED" not in str(excinfo.value)
    assert "Failed to fetch" not in str(excinfo.value)


async def test_no_error_from_this_module_ever_carries_cookie_or_driver_data() -> None:
    """N10, generalized: nothing this module raises may embed data from the failure it wraps.

    Moot in one sense after F2b (there is no cookie value in Python to leak any
    more), but the invariant this pins is broader than that one value: an
    exception's ``str()`` here is always one of this module's own fixed sentences.
    """
    context = make_context()
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        secret_shaped = 'RuntimeError: cookie="ajax:should-never-appear-anywhere"'
        context.pages[0].fail_next_evaluate(RuntimeError(secret_shaped))

        with pytest.raises(VoyagerFetchError) as excinfo:
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert "should-never-appear-anywhere" not in str(excinfo.value)
    assert "ajax:" not in str(excinfo.value)


# --- headers: composed in Python, minus the one value that never is (F2b) ----------


def test_base_headers_never_include_a_csrf_token_key() -> None:
    assert CSRF_HEADER_NAME not in _base_headers({})


def test_base_headers_match_build_headers_minus_the_csrf_token() -> None:
    """No drift: this module reads the same constants build_headers does."""
    placeholder_full = build_headers("placeholder-not-a-real-cookie", extra={"x-extra": "1"})
    del placeholder_full[CSRF_HEADER_NAME]

    assert _base_headers({"x-extra": "1"}) == placeholder_full


def test_extra_headers_override_the_base_set() -> None:
    assert _base_headers({"accept": "text/plain"})["accept"] == "text/plain"


@pytest.mark.parametrize("spelling", ["csrf-token", "CSRF-TOKEN", "Csrf-Token", "cSrF-tOkEn"])
def test_a_caller_cannot_override_or_merge_with_csrf_token_whatever_the_case(
    spelling: str,
) -> None:
    """#170 item 5 (mutation R7): a caller header spelled differently than
    ``csrf-token`` must not survive into the headers this module hands the script --
    it would otherwise land under its own casing and get *combined* with the
    script's live value by fetch()'s own Headers merging, rather than overridden.
    """
    headers = _base_headers({spelling: "attacker-supplied-value"})
    assert "attacker-supplied-value" not in headers.values()
    assert all(name.lower() != CSRF_HEADER_NAME.lower() for name in headers)


async def test_a_callers_extra_header_reaches_the_script() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        await fetch(VoyagerRequest(path=CONNECTIONS_PATH, headers={"accept": "text/plain"}))
        page = context.pages[0]

    assert json.dumps("text/plain") in page.evaluate_calls[-1]


async def test_a_callers_csrf_token_header_never_reaches_the_script_whatever_the_case() -> None:
    """The end-to-end version of the ``_base_headers`` unit tests above: even a
    caller reaching for ``PageVoyagerFetch`` directly cannot get a value of its own
    choosing sent as (or merged into) ``csrf-token``."""
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        await fetch(
            VoyagerRequest(path=CONNECTIONS_PATH, headers={"CSRF-Token": "attacker-supplied"})
        )
        page = context.pages[0]

    assert "attacker-supplied" not in page.evaluate_calls[-1]


async def test_the_url_and_base_headers_reach_the_script() -> None:
    context = make_context(evaluate_result=fetch_result(status=200, body="{}", url="x"))
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        await fetch(VoyagerRequest(path=CONNECTIONS_PATH, query=connections_query(start=0)))
        sent = context.pages[0].evaluate_calls[-1]

    expected_url = f"{LINKEDIN_ORIGIN}{CONNECTIONS_PATH}?{urlencode(connections_query(start=0))}"
    assert json.dumps(expected_url) in sent
    assert json.dumps(_base_headers({})) in sent


# --- structural checks on the generated script (what a browser executes) -----------


def test_the_script_reads_document_cookie_for_jsessionid() -> None:
    text = _fetch_expression("https://example.invalid/x", {"accept": "a"}, origin=LINKEDIN_ORIGIN)
    assert "document.cookie" in text
    assert "JSESSIONID" in text


def test_the_script_strips_the_cookies_surrounding_quotes() -> None:
    text = _fetch_expression("https://example.invalid/x", {}, origin=LINKEDIN_ORIGIN)
    assert "slice(1, -1)" in text, "the quote-stripping this module documents must be in the script"


def test_the_script_refuses_an_empty_or_missing_csrf_token() -> None:
    """N6: an empty (or absent) JSESSIONID must not be sent as an empty header value."""
    text = _fetch_expression("https://example.invalid/x", {}, origin=LINKEDIN_ORIGIN)
    assert "if (!t)" in text
    assert json.dumps(_NO_CSRF_MARKER) in text


def test_the_script_never_embeds_a_python_known_csrf_value() -> None:
    """There is nothing to embed any more -- pinned so a regression is a red test, not a re-read."""
    text = _fetch_expression("https://example.invalid/x", _base_headers({}), origin=LINKEDIN_ORIGIN)
    assert CSRF_HEADER_NAME not in json.dumps(_base_headers({}))
    # The header *name* legitimately appears once, added by the script itself.
    assert text.count(json.dumps(CSRF_HEADER_NAME)) == 1


# --- #170 item 3: the origin check is repeated atomically inside the script -------


def test_the_script_checks_its_own_origin_before_anything_else() -> None:
    text = _fetch_expression("https://example.invalid/x", {}, origin=LINKEDIN_ORIGIN)
    assert f"location.origin !== {json.dumps(LINKEDIN_ORIGIN)}" in text
    assert json.dumps(_ORIGIN_MISMATCH_MARKER) in text
    # Checked before the cookie is ever read: the whole point is to fail before
    # touching anything else, not merely to fail eventually.
    assert text.index("location.origin") < text.index("document.cookie")


async def test_an_origin_mismatch_inside_the_script_is_a_clear_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Python-side page.url check can't catch a navigation that happens in the
    gap between that read and page.evaluate actually running; this simulates the
    in-page script itself catching it, the way a real browser would."""
    context = make_context()
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        context.pages[0].fail_next_evaluate(RuntimeError(_ORIGIN_MISMATCH_MARKER))
        with pytest.raises(VoyagerFetchError, match="navigated away"):
            await fetch(VoyagerRequest(path=CONNECTIONS_PATH))


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
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        response = await fetch(VoyagerRequest(path=CONNECTIONS_PATH))

    assert response.status == 200
    assert response.body == body
    page = parse_ok(response, parse_connections_page)
    assert page.connections[0].public_id == "rehearsal-marker-person"


# --- shape validation on what the script returned -----------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_message"),
    [
        ("not a mapping", "the in-page fetch returned str, not an object"),
        ({"status": "200", "body": "x", "url": "x"}, "no numeric 'status'"),
        ({"status": 200, "body": 5, "url": "x"}, "no string 'body'"),
        ({"status": 200, "body": "x", "url": 5}, "no string 'url'"),
        ({"status": True, "body": "x", "url": "x"}, "no numeric 'status'"),
        ({}, "no numeric 'status'"),
    ],
)
async def test_a_malformed_evaluate_result_raises_a_fixed_structural_message(
    raw: object, expected_message: str
) -> None:
    """The message names the shape problem, never the value that caused it (F6, generalized)."""
    context = make_context(evaluate_result=raw)
    async with run_with(context) as run:
        await on_origin(run)
        fetch = PageVoyagerFetch(run)
        with pytest.raises(VoyagerFetchError, match=expected_message):
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


def test_voyager_not_oks_message_never_includes_the_body_or_the_query_string() -> None:
    """N5: a marker placed in the body or a query string must never surface in str()."""
    response = VoyagerResponse(
        status=429,
        body="a-body-marker-that-must-never-appear-in-the-message",
        final_url="https://www.linkedin.com/voyager/api/x?token=a-query-marker-too",
    )

    with pytest.raises(VoyagerNotOk) as excinfo:
        parse_ok(response, lambda text: text)

    message = str(excinfo.value)
    assert "a-body-marker-that-must-never-appear-in-the-message" not in message
    assert "a-query-marker-too" not in message
    assert message == "voyager response classified throttled, not ok"
