import httpx
import pytest

from netkeeper.web.security import (
    RULE_CLIENT_HEADER,
    RULE_ORIGIN,
    RULE_SEC_FETCH_SITE,
    csrf_violation,
)

OK = {"x-netkeeper-client": "1"}
CSRF = {"X-Netkeeper-Client": "1"}
PATH = "/api/v1/tasks/ping"


def _rule(
    method: str,
    path: str = PATH,
    headers: dict[str, str] | None = None,
    *,
    scheme: str = "http",
    host: str = "testserver",
) -> str | None:
    return csrf_violation(method, path, {"host": host, **(headers or {})}, scheme=scheme)


# --- the pure rule ----------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_reads_are_never_blocked(method: str) -> None:
    assert _rule(method) is None
    hostile = {"origin": "http://evil.example", "sec-fetch-site": "cross-site"}
    assert _rule(method, headers=hostile) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_state_changes_need_the_client_header(method: str) -> None:
    assert _rule(method) == RULE_CLIENT_HEADER
    assert _rule(method, headers={"x-netkeeper-client": "yes"}) == RULE_CLIENT_HEADER
    assert _rule(method, headers=OK) is None


def test_paths_outside_the_api_are_not_guarded() -> None:
    assert _rule("POST", "/") is None
    assert _rule("POST", "/apiary") is None
    assert _rule("POST", "/api") is None
    assert _rule("POST", "/api/") == RULE_CLIENT_HEADER


def test_origin_must_match_the_request_origin() -> None:
    assert _rule("POST", headers={**OK, "origin": "http://testserver"}) is None
    assert _rule("POST", headers={**OK, "origin": "http://evil.example"}) == RULE_ORIGIN
    assert _rule("POST", headers={**OK, "origin": "null"}) == RULE_ORIGIN
    assert _rule("POST", headers={**OK, "origin": "garbage"}) == RULE_ORIGIN
    assert _rule("POST", headers={**OK, "origin": "https://testserver"}) == RULE_ORIGIN
    assert (
        _rule("POST", headers={**OK, "origin": "http://testserver:8001"}, host="testserver:8000")
        == RULE_ORIGIN
    )


def test_origin_comparison_normalizes_ports_and_case() -> None:
    assert _rule("POST", headers={**OK, "origin": "http://TestServer:80"}) is None
    assert (
        _rule("POST", headers={**OK, "origin": "http://testserver"}, host="testserver:80") is None
    )
    assert (
        _rule(
            "POST",
            headers={**OK, "origin": "https://testserver"},
            scheme="https",
            host="testserver:443",
        )
        is None
    )
    assert (
        _rule("POST", headers={**OK, "origin": "http://127.0.0.1:8000"}, host="127.0.0.1:8000")
        is None
    )
    assert _rule("POST", headers={**OK, "origin": "http://[::1]:8000"}, host="[::1]:8000") is None


def test_sec_fetch_site_vouches_for_a_mismatched_origin() -> None:
    proxied = {**OK, "origin": "http://localhost:5173"}
    assert _rule("POST", headers={**proxied, "sec-fetch-site": "same-origin"}) is None
    assert _rule("POST", headers={**proxied, "sec-fetch-site": "none"}) is None
    assert _rule("POST", headers={**proxied, "sec-fetch-site": "cross-site"}) == RULE_ORIGIN
    assert _rule("POST", headers={**proxied, "sec-fetch-site": "same-site"}) == RULE_ORIGIN


def test_sec_fetch_site_alone_blocks_cross_site() -> None:
    assert _rule("POST", headers={**OK, "sec-fetch-site": "cross-site"}) == RULE_SEC_FETCH_SITE
    assert _rule("POST", headers={**OK, "sec-fetch-site": "same-site"}) == RULE_SEC_FETCH_SITE
    assert _rule("POST", headers={**OK, "sec-fetch-site": "same-origin"}) is None
    assert _rule("POST", headers={**OK, "sec-fetch-site": "none"}) is None


# --- through the app --------------------------------------------------------


async def test_post_without_the_header_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH)
    assert response.status_code == 403
    body = response.json()
    assert body["rule"] == RULE_CLIENT_HEADER
    assert "X-Netkeeper-Client" in body["detail"]


async def test_post_with_the_header_and_same_origin_passes(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH, headers={**CSRF, "Origin": "http://testserver"})
    assert response.status_code == 202


async def test_post_from_another_origin_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH, headers={**CSRF, "Origin": "http://evil.example"})
    assert response.status_code == 403
    assert response.json()["rule"] == RULE_ORIGIN


async def test_post_flagged_cross_site_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(PATH, headers={**CSRF, "Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert response.json()["rule"] == RULE_SEC_FETCH_SITE


async def test_get_is_never_blocked(client: httpx.AsyncClient) -> None:
    hostile = {"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"}
    response = await client.get("/api/v1/health", headers=hostile)
    assert response.status_code == 200
