"""The Gmail installed-app OAuth flow against a loopback fake of Google (#244, P3-01)."""

import base64
import hashlib
import json
import threading
import urllib.request
from urllib.parse import parse_qs, urlsplit

import pytest
from gmail_fakes import CLIENT_ID, CLIENT_SECRET, FAKE_EMAIL, FakeGoogle

from netkeeper.campaigns import gmail_oauth
from netkeeper.campaigns.gmail_oauth import (
    ClientConfigError,
    GmailApiRefused,
    InvalidGrant,
    LoopbackReceiver,
    OAuthClient,
    OAuthRefused,
    OAuthUnavailable,
    ScopeNotGranted,
)

CLIENT = OAuthClient(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
REDIRECT = "http://127.0.0.1:8000/api/v1/mailboxes/oauth/callback"


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def test_the_scope_and_googles_endpoints_are_pinned() -> None:
    """gmail.modify only (spec 11.5: no delete), and Google's documented endpoints."""
    assert gmail_oauth.GMAIL_SCOPE == "https://www.googleapis.com/auth/gmail.modify"
    assert (
        gmail_oauth.GoogleEndpoints(
            auth_uri="https://accounts.google.com/o/oauth2/v2/auth",
            token_uri="https://oauth2.googleapis.com/token",
            profile_uri="https://gmail.googleapis.com/gmail/v1/users/me/profile",
            timeout_s=20.0,
            use_system_proxy=True,
        )
        == gmail_oauth.GOOGLE_ENDPOINTS
    )


def test_tests_cannot_reach_google() -> None:
    """The autouse fixture points GOOGLE at a scheme urllib refuses."""
    with pytest.raises(OAuthUnavailable):
        gmail_oauth.refresh_access_token(CLIENT, "rt")


# --- the client --------------------------------------------------------------------


def test_a_desktop_client_file_is_read() -> None:
    text = json.dumps(
        {
            "installed": {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uris": ["http://localhost"],
            }
        }
    )
    assert gmail_oauth.parse_client_file(text) == CLIENT


def test_a_web_client_file_is_refused_with_a_pointer_to_the_guide() -> None:
    text = json.dumps({"web": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}})
    with pytest.raises(ClientConfigError, match="Desktop app") as caught:
        gmail_oauth.parse_client_file(text)
    assert caught.value.code == "web_client"


@pytest.mark.parametrize("text", ["not json", "[]", '{"other": {}}'])
def test_a_file_that_is_not_a_client_is_refused(text: str) -> None:
    with pytest.raises(ClientConfigError):
        gmail_oauth.parse_client_file(text)


@pytest.mark.parametrize(
    ("client_id", "secret", "code"),
    [
        ("my-project-123", CLIENT_SECRET, "bad_client_id"),
        (".apps.googleusercontent.com", CLIENT_SECRET, "bad_client_id"),
        (CLIENT_ID, "", "bad_secret"),
        (CLIENT_ID, "two words", "bad_secret"),
    ],
)
def test_pasted_values_are_checked(client_id: str, secret: str, code: str) -> None:
    with pytest.raises(ClientConfigError) as caught:
        gmail_oauth.validate_client(client_id, secret)
    assert caught.value.code == code


def test_pasted_values_are_trimmed() -> None:
    assert gmail_oauth.validate_client(f"  {CLIENT_ID}\n", f" {CLIENT_SECRET} ") == CLIENT


def test_the_secret_stays_out_of_repr() -> None:
    assert CLIENT_SECRET not in repr(CLIENT)
    assert OAuthClient.from_json(CLIENT.to_json()) == CLIENT


# --- authorize ---------------------------------------------------------------------


def test_the_authorization_url_asks_for_offline_gmail_modify_with_pkce() -> None:
    authorization = gmail_oauth.begin(CLIENT, REDIRECT, login_hint="me@example.com")
    assert authorization.url.startswith("blocked-in-tests://google/auth?")
    query = _query(authorization.url)
    assert query == {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": gmail_oauth.GMAIL_SCOPE,
        "state": authorization.state,
        "code_challenge": query["code_challenge"],
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": "me@example.com",
    }
    digest = hashlib.sha256(authorization.verifier.encode()).digest()
    assert query["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert 43 <= len(authorization.verifier) <= 128  # RFC 7636
    assert authorization.verifier not in repr(authorization)


def test_every_authorization_has_its_own_state_and_verifier() -> None:
    first = gmail_oauth.begin(CLIENT, REDIRECT)
    second = gmail_oauth.begin(CLIENT, REDIRECT)
    assert first.state != second.state
    assert first.verifier != second.verifier
    assert "login_hint" not in _query(first.url)


@pytest.mark.parametrize(
    "redirect", ["https://127.0.0.1/cb", "http://example.com/cb", "http://192.168.1.2:8000/"]
)
def test_the_redirect_must_be_loopback_http(redirect: str) -> None:
    with pytest.raises(ClientConfigError):
        gmail_oauth.begin(CLIENT, redirect)


# --- exchange, refresh, profile ----------------------------------------------------


def test_a_consented_code_gives_a_refresh_token_and_the_accounts_address(
    fake_google: FakeGoogle,
) -> None:
    authorization = gmail_oauth.begin(CLIENT, REDIRECT)
    code = _query(fake_google.consent(authorization.url, email="Sender@Example.com"))["code"]
    grant = gmail_oauth.exchange_code(CLIENT, authorization, code)
    assert fake_google.refresh_tokens[grant.refresh_token] == "Sender@Example.com"
    assert gmail_oauth.fetch_email(grant.access_token) == "sender@example.com"
    assert grant.refresh_token not in repr(grant)


def test_a_wrong_verifier_is_refused_by_google(fake_google: FakeGoogle) -> None:
    authorization = gmail_oauth.begin(CLIENT, REDIRECT)
    code = _query(fake_google.consent(authorization.url))["code"]
    other = gmail_oauth.begin(CLIENT, REDIRECT)
    forged = gmail_oauth.Authorization(
        url=authorization.url,
        state=authorization.state,
        verifier=other.verifier,
        redirect_uri=REDIRECT,
    )
    with pytest.raises(InvalidGrant):
        gmail_oauth.exchange_code(CLIENT, forged, code)


def test_unticking_gmail_on_the_consent_screen_is_refused(fake_google: FakeGoogle) -> None:
    authorization = gmail_oauth.begin(CLIENT, REDIRECT)
    code = _query(fake_google.consent(authorization.url, scope="openid email"))["code"]
    with pytest.raises(ScopeNotGranted):
        gmail_oauth.exchange_code(CLIENT, authorization, code)


def test_a_refresh_gives_an_access_token(fake_google: FakeGoogle) -> None:
    token = fake_google.issue_refresh_token()
    access = gmail_oauth.refresh_access_token(CLIENT, token)
    assert fake_google.access_tokens[access] == FAKE_EMAIL
    assert fake_google.requests[-1] == ("/token", "refresh_token")


def test_a_revoked_token_is_invalid_grant(fake_google: FakeGoogle) -> None:
    token = fake_google.issue_refresh_token()
    fake_google.revoke_all()
    with pytest.raises(InvalidGrant) as caught:
        gmail_oauth.refresh_access_token(CLIENT, token)
    assert caught.value.code == "invalid_grant"
    assert token not in str(caught.value)


def test_a_wrong_client_secret_is_refused_with_googles_code(fake_google: FakeGoogle) -> None:
    token = fake_google.issue_refresh_token()
    wrong = OAuthClient(client_id=CLIENT_ID, client_secret="not-it")
    with pytest.raises(OAuthRefused) as caught:
        gmail_oauth.refresh_access_token(wrong, token)
    assert caught.value.code == "invalid_client"
    assert "not-it" not in str(caught.value)


def test_a_server_error_is_unavailable_not_a_dead_grant(fake_google: FakeGoogle) -> None:
    token = fake_google.issue_refresh_token()
    fake_google.token_status = 503
    with pytest.raises(OAuthUnavailable):
        gmail_oauth.refresh_access_token(CLIENT, token)


def test_an_unreachable_endpoint_is_unavailable(fake_google: FakeGoogle) -> None:
    endpoints = fake_google.endpoints
    fake_google.stop()
    with pytest.raises(OAuthUnavailable):
        gmail_oauth.refresh_access_token(CLIENT, "rt", endpoints=endpoints)


def test_a_disabled_gmail_api_says_so(fake_google: FakeGoogle) -> None:
    access = gmail_oauth.refresh_access_token(CLIENT, fake_google.issue_refresh_token())
    fake_google.profile_status = 403
    with pytest.raises(GmailApiRefused, match="enable the Gmail API"):
        gmail_oauth.fetch_email(access)


# --- the CLI's receiver ------------------------------------------------------------


def _get(url: str) -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=5) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def test_the_receiver_hands_back_the_redirects_query_and_ignores_other_paths() -> None:
    with LoopbackReceiver() as receiver:
        assert receiver.redirect_uri.startswith("http://127.0.0.1:")
        statuses: list[int] = []

        def browser() -> None:
            statuses.append(_get(f"{receiver.redirect_uri}favicon.ico"))
            statuses.append(_get(f"{receiver.redirect_uri}?state=s1&code=c1"))

        thread = threading.Thread(target=browser)
        thread.start()
        assert receiver.wait(5) == {"state": "s1", "code": "c1"}
        thread.join(5)
    assert statuses == [404, 200]


def test_the_receiver_gives_up_after_its_timeout() -> None:
    with LoopbackReceiver() as receiver, pytest.raises(TimeoutError):
        receiver.wait(0.05)
