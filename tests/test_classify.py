"""netkeeper.linkedin.classify (spec 9.7): a response's status, url, and body
in, one of six outcomes out.

Every fixture below is invented, as every fixture in this repository is
(CLAUDE.md): no captured LinkedIn markup, just the one detail classify() cares
about in each -- a reference to a challenge path, a reference to a login path,
a recognizable JSON shape -- wrapped in enough HTML or JSON to look real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from netkeeper.linkedin.classify import Outcome, classify, is_retryable

# What ADR 0005 keeps out of the extractor, same list test_linkedin_archive.py
# uses: importing the ORM is how the models (and a session) would arrive.
FORBIDDEN_IMPORTS = ("netkeeper.models", "netkeeper.crm", "netkeeper.db", "sqlalchemy")

CONNECTIONS_URL = "https://www.linkedin.com/voyager/api/relationships/connections"
PROFILE_URL = "https://www.linkedin.com/voyager/api/identity/profiles/nettie-keeperton/profileView"
GHOST_PROFILE_URL = (
    "https://www.linkedin.com/voyager/api/identity/profiles/no-such-person/profileView"
)
CHECKPOINT_URL = "https://www.linkedin.com/checkpoint/challenge/?ctx=abc123"
AUTHWALL_URL = "https://www.linkedin.com/authwall?trk=login-reg&sessionRedirect=abc"

# A stand-in for LinkedIn's own security-checkpoint interstitial. Invented
# markup: the one thing classify() looks for is the reference to the
# challenge path, here in a meta-refresh the way a real one would use to send
# the browser on.
CHECKPOINT_BODY = (
    "<html><head><title>Let's do a quick security check</title>"
    '<meta http-equiv="refresh" '
    'content="0;URL=https://www.linkedin.com/checkpoint/challenge/?ctx=abc123"></head>'
    "<body>We want to make sure it's really you.</body></html>"
)

# Same idea for the login wall an unauthenticated request gets back: invented
# markup with a reference to the login-submit path, the shape that matters
# when the request never actually redirected (the in-page fetch case, 9.3)
# and the original API url comes back unchanged.
LOGIN_WALL_BODY = (
    "<html><head><title>Sign in to LinkedIn</title>"
    '<link rel="canonical" href="https://www.linkedin.com/uas/login-submit"></head>'
    "<body>Please sign in to continue.</body></html>"
)

# An HTML body that is neither of the above -- the negative control proving
# classify() reacts to the specific paths, not to "the body happens to be
# HTML".
GENERIC_ERROR_BODY = "<html><body>Something went wrong. Please try again.</body></html>"


def _connections_json(count: int = 3) -> str:
    return json.dumps({"elements": [], "paging": {"count": count, "start": 0, "total": count}})


def _throttle_json() -> str:
    return json.dumps({"status": 429, "message": "Too many requests. Please wait and try again."})


def _request_denied_json() -> str:
    return json.dumps({"status": 999, "message": "Request denied"})


def _unauthorized_json() -> str:
    return json.dumps({"status": 401, "message": "Unauthorized"})


def _not_found_json() -> str:
    return json.dumps({"status": 404, "message": "Profile not found"})


def _bad_request_json() -> str:
    return json.dumps({"status": 400, "message": "Unrecognized field: legacyFoo"})


def _server_error_json() -> str:
    return json.dumps({"status": 503, "message": "upstream unavailable"})


def _forbidden_json() -> str:
    return json.dumps({"status": 403, "message": "forbidden"})


@pytest.mark.parametrize(
    ("response", "url", "body", "expected"),
    [
        # --- the six rows of spec 9.7's table, plain, each isolated to the
        # one signal that row is about -------------------------------------
        pytest.param(200, CONNECTIONS_URL, _connections_json(), Outcome.OK, id="200_json_is_ok"),
        pytest.param(
            429, CONNECTIONS_URL, _throttle_json(), Outcome.THROTTLED, id="429_is_throttled"
        ),
        pytest.param(
            999, CONNECTIONS_URL, _request_denied_json(), Outcome.THROTTLED, id="999_is_throttled"
        ),
        pytest.param(
            200,
            CHECKPOINT_URL,
            GENERIC_ERROR_BODY,
            Outcome.CHECKPOINT,
            id="url_pointing_at_checkpoint_is_checkpoint",
        ),
        pytest.param(
            401, PROFILE_URL, _unauthorized_json(), Outcome.LOGGED_OUT, id="401_is_logged_out"
        ),
        pytest.param(
            200,
            AUTHWALL_URL,
            GENERIC_ERROR_BODY,
            Outcome.LOGGED_OUT,
            id="url_pointing_at_authwall_is_logged_out",
        ),
        pytest.param(
            404,
            GHOST_PROFILE_URL,
            _not_found_json(),
            Outcome.NOT_FOUND,
            id="404_on_a_profile_is_not_found",
        ),
        pytest.param(
            400,
            CONNECTIONS_URL,
            _bad_request_json(),
            Outcome.ROUTE_CHANGED,
            id="400_on_a_known_endpoint_is_route_changed",
        ),
        pytest.param(
            200,
            CONNECTIONS_URL,
            GENERIC_ERROR_BODY,
            Outcome.ROUTE_CHANGED,
            id="200_with_unrecognized_shape_is_route_changed",
        ),
        # --- statuses spec 9.7's table does not name: the conservative
        # default, and proof the ``response == 200`` guard on the Ok branch
        # actually matters (a valid JSON body at a non-200 status must not
        # become Ok) -----------------------------------------------------
        pytest.param(
            503,
            CONNECTIONS_URL,
            _server_error_json(),
            Outcome.ROUTE_CHANGED,
            id="503_with_json_body_is_route_changed_not_ok",
        ),
        pytest.param(
            403,
            CONNECTIONS_URL,
            _forbidden_json(),
            Outcome.ROUTE_CHANGED,
            id="403_is_route_changed",
        ),
        # --- the two rows where the HTTP status lies (the issue's "done
        # when"), each with a negative control right beside it so the outcome
        # is provably tied to the body's content and not merely to the status
        # or to the body being non-JSON -------------------------------------
        pytest.param(
            429,
            CONNECTIONS_URL,
            CHECKPOINT_BODY,
            Outcome.CHECKPOINT,
            id="429_with_html_body_is_checkpoint_not_throttled",
        ),
        pytest.param(
            429,
            CONNECTIONS_URL,
            GENERIC_ERROR_BODY,
            Outcome.THROTTLED,
            id="429_with_plain_html_body_is_still_throttled",
        ),
        pytest.param(
            200,
            PROFILE_URL,
            LOGIN_WALL_BODY,
            Outcome.LOGGED_OUT,
            id="200_with_login_page_is_logged_out_not_ok",
        ),
        pytest.param(
            200,
            PROFILE_URL,
            CHECKPOINT_BODY,
            Outcome.CHECKPOINT,
            id="200_with_checkpoint_body_and_unchanged_url_is_checkpoint",
        ),
        # --- the edge of the JSON-shape check for Ok / RouteChanged --------
        pytest.param(
            200,
            CONNECTIONS_URL,
            json.dumps("maintenance"),
            Outcome.ROUTE_CHANGED,
            id="200_with_bare_json_string_is_route_changed",
        ),
        pytest.param(
            200,
            CONNECTIONS_URL,
            "",
            Outcome.ROUTE_CHANGED,
            id="200_with_empty_body_is_route_changed",
        ),
    ],
)
def test_classify_table(response: int, url: str, body: str, expected: Outcome) -> None:
    assert classify(response, url, body) is expected


# --- a contact's own data must never be misread as a session signal --------
#
# Review on #144 found that scanning the whole body for a bare path
# substring, unconditionally, misclassified a perfectly healthy response
# whenever *someone else's data* happened to contain one of the marker
# strings: a contact's personal website, a tracking url LinkedIn attaches to
# a result, a headline that happens to name a path. Each case below is a
# recognizable JSON body, so `classify()` must not scan it at all -- only an
# unrecognized (HTML) body is scanned. Every one of these used to come back
# `checkpoint` or `logged_out` and halt a run over a stranger's headline.


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            json.dumps({"websites": [{"url": "https://acme.example/login"}]}),
            id="contacts_own_website_ends_in_login",
        ),
        pytest.param(
            json.dumps({"elements": [{"navigationUrl": "https://www.example.test/uas/login?x=1"}]}),
            id="a_navigation_url_field_contains_uas_login",
        ),
        pytest.param(
            json.dumps({"elements": [{"trackingUrl": "/checkpoint/lite/ping"}]}),
            id="a_tracking_url_field_contains_checkpoint",
        ),
        pytest.param(
            json.dumps({"elements": [{"headline": "I run the /challenge/ series"}]}),
            id="a_headline_field_contains_challenge",
        ),
    ],
)
def test_a_path_substring_inside_recognizable_json_is_not_a_session_signal(body: str) -> None:
    assert classify(200, CONNECTIONS_URL, body) is Outcome.OK


# --- every path constant is individually load-bearing ----------------------
#
# Review on #144 found that four of the five path constants could be deleted
# from classify.py with the suite still green, because the shared fixtures
# above (CHECKPOINT_URL, CHECKPOINT_BODY, LOGIN_WALL_BODY) each happen to
# carry more than one marker at once. Each case below carries exactly one.


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "https://www.linkedin.com/checkpoint/lite/ping", Outcome.CHECKPOINT, id="checkpoint"
        ),
        pytest.param(
            "https://www.linkedin.com/challenge/verify", Outcome.CHECKPOINT, id="challenge"
        ),
        pytest.param("https://www.linkedin.com/login/identity", Outcome.LOGGED_OUT, id="login"),
        pytest.param("https://www.linkedin.com/authwall", Outcome.LOGGED_OUT, id="authwall"),
        pytest.param(
            "https://www.linkedin.com/uas/request-password-reset",
            Outcome.LOGGED_OUT,
            id="uas",
        ),
    ],
)
def test_each_path_constant_is_checked_alone_in_the_url(url: str, expected: Outcome) -> None:
    # A recognizable JSON body carrying none of the markers, so only the url
    # branch of _mentions() can be responsible for the outcome.
    assert classify(200, url, _connections_json()) is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            "<html><body>redirecting to /checkpoint/lite/ping</body></html>",
            Outcome.CHECKPOINT,
            id="checkpoint",
        ),
        pytest.param(
            "<html><body>redirecting to /challenge/verify</body></html>",
            Outcome.CHECKPOINT,
            id="challenge",
        ),
        pytest.param(
            "<html><body>please use /login to continue</body></html>",
            Outcome.LOGGED_OUT,
            id="login",
        ),
        pytest.param(
            "<html><body>blocked by /authwall</body></html>", Outcome.LOGGED_OUT, id="authwall"
        ),
        pytest.param(
            "<html><body>see /uas/request-password-reset</body></html>",
            Outcome.LOGGED_OUT,
            id="uas",
        ),
    ],
)
def test_each_path_constant_is_checked_alone_in_the_body(body: str, expected: Outcome) -> None:
    # PROFILE_URL carries none of the markers, and the body is not JSON, so
    # only the body branch of _mentions() can be responsible for the outcome.
    assert classify(200, PROFILE_URL, body) is expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(Outcome.OK, False, id="ok"),
        pytest.param(Outcome.THROTTLED, True, id="throttled"),
        pytest.param(Outcome.CHECKPOINT, False, id="checkpoint"),
        pytest.param(Outcome.LOGGED_OUT, False, id="logged_out"),
        pytest.param(Outcome.NOT_FOUND, False, id="not_found"),
        pytest.param(Outcome.ROUTE_CHANGED, False, id="route_changed"),
    ],
)
def test_is_retryable(outcome: Outcome, expected: bool) -> None:
    assert is_retryable(outcome) is expected


def test_checkpoint_is_never_retryable() -> None:
    """The literal "no retry on Checkpoint" requirement (P2-03), named on its own
    rather than folded into the table above so it cannot be lost in a refactor
    of that table."""
    assert is_retryable(Outcome.CHECKPOINT) is False


def test_outcome_is_exactly_the_six_names_spec_9_7_lists() -> None:
    """A new outcome, a rename, or a dropped one all change this list -- and none
    of them are this function's call to make (see the module docstring)."""
    assert [outcome.value for outcome in Outcome] == [
        "ok",
        "throttled",
        "checkpoint",
        "logged_out",
        "not_found",
        "route_changed",
    ]


# --- the boundary -------------------------------------------------------


def test_classify_loads_no_models_and_no_session() -> None:
    """ADR 0005: nothing under ``linkedin/`` imports the ORM or opens a session.

    Asserted in a subprocess for the same reason test_linkedin_archive.py's
    equivalent test is: this test module already has the whole package
    imported, so it would see every one of these in ``sys.modules`` regardless
    of what classify.py itself did. A plain grep would miss a transitive
    import, which is the way this invariant actually breaks.
    """
    script = (
        "import sys, json\n"
        "import netkeeper.linkedin.classify\n"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=Path(__file__).parent.parent,
    )
    loaded = json.loads(result.stdout)
    leaked = [
        name
        for name in loaded
        for forbidden in FORBIDDEN_IMPORTS
        if name == forbidden or name.startswith(f"{forbidden}.")
    ]
    assert leaked == [], f"netkeeper.linkedin.classify pulled in {leaked}"
