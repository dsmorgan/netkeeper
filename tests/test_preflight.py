"""``netkeeper preflight`` and ``netkeeper browser launch`` (spec 9.1, ADR 0002).

Preflight has to answer "is the LinkedIn session still there" without asking
linkedin.com, because a check that loads the site is a request the user did not make
and a test that loads the site is not a test. It reads the browser's own cookie jar
instead, so every case below is an ordinary offline test with a fake jar, and the
test that matters most asserts the thing preflight must never do: navigate.

The fixtures here are invented. ``li_at`` never holds a real session value, and the
assertions below are what keep any cookie value out of the report, the logs, and the
terminal.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from browser_fakes import FakeBrowser, FakeConnector, FakeContext
from browser_guard import UNREACHABLE_CDP_URL
from typer.testing import CliRunner

from netkeeper.cli import app as cli
from netkeeper.linkedin.browser import ActivityLocks, AttachBrowserProvider
from netkeeper.linkedin.preflight import Fingerprint, LoginState, preflight

CDP_URL = UNREACHABLE_CDP_URL  # never a real Chrome, even without the guard (#294)
BUSY_TIMEOUT_S = 1.0

# Invented, and never a value any code path reads: the tests below assert that this
# string never leaves the fake jar.
FAKE_SESSION_VALUE = "fake-li-at-value-0000-not-a-session"
FAKE_CSRF_VALUE = "ajax:0000000000000000000"

NORMAL_CHROME = {
    "userAgent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "platform": "MacIntel",
    "languages": ["en-US", "en"],
    "timezone": "America/New_York",
    "hardwareConcurrency": 10,
    "webdriver": False,
    "pluginCount": 5,
}


def cookie(
    name: str, value: str, *, domain: str = ".www.linkedin.com", expires: float = -1
) -> dict[str, Any]:
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


def logged_in_jar(expires: float = -1) -> list[dict[str, Any]]:
    return [
        cookie("li_at", FAKE_SESSION_VALUE, expires=expires),
        cookie("JSESSIONID", FAKE_CSRF_VALUE),
        cookie("session-id", "unrelated-site-cookie", domain=".example.invalid"),
    ]


def make_context(
    cookies: list[dict[str, Any]] | None = None, fingerprint: dict[str, Any] | None = None
) -> FakeContext:
    return FakeContext(
        cookies=logged_in_jar() if cookies is None else cookies,
        evaluate_result=NORMAL_CHROME if fingerprint is None else fingerprint,
    )


def provider_for(
    context: FakeContext, connector: FakeConnector | None = None
) -> AttachBrowserProvider:
    return AttachBrowserProvider(
        CDP_URL,
        connector=connector or FakeConnector([FakeBrowser([context])]),
        locks=ActivityLocks(),
    )


def make_provider(
    *,
    cookies: list[dict[str, Any]] | None = None,
    fingerprint: dict[str, Any] | None = None,
    connector: FakeConnector | None = None,
) -> AttachBrowserProvider:
    return provider_for(make_context(cookies, fingerprint), connector)


# --- what preflight reads ----------------------------------------------------


async def test_preflight_never_navigates_anywhere() -> None:
    """The point of the cookie-jar check: no request to linkedin.com, ever.

    If a later item makes preflight load a page to prove the session, this test is
    the one that has to be argued with first.
    """
    context = make_context()
    report = await preflight(provider_for(context))

    assert len(context.pages) == 1, "preflight opens exactly one blank tab"
    assert context.pages[0].goto_calls == [], (
        f"preflight navigated to {context.pages[0].goto_calls}"
    )
    assert context.pages[0].is_closed(), "preflight closed the tab it opened"
    assert report.attached


async def test_a_live_session_reads_as_logged_in() -> None:
    report = await preflight(make_provider())

    assert report.login is LoginState.LOGGED_IN
    assert report.session_cookies == ("li_at", "JSESSIONID")
    assert report.ok
    assert report.problems == ()


async def test_a_profile_without_the_session_cookie_has_no_session() -> None:
    jar = [cookie("JSESSIONID", FAKE_CSRF_VALUE), cookie("bcookie", "x")]
    report = await preflight(make_provider(cookies=jar))

    assert report.login is LoginState.NO_SESSION
    assert not report.ok
    assert "log in to LinkedIn" in " ".join(report.problems)


async def test_an_expired_session_cookie_has_no_session() -> None:
    yesterday = (datetime.now(UTC) - timedelta(days=1)).timestamp()
    report = await preflight(make_provider(cookies=logged_in_jar(expires=yesterday)))

    assert report.login is LoginState.NO_SESSION
    assert report.session_expires_at is not None
    assert not report.ok


async def test_a_session_cookie_for_another_site_does_not_count() -> None:
    """``li_at`` on someone else's domain is not a LinkedIn session."""
    jar = [cookie("li_at", FAKE_SESSION_VALUE, domain=".linkedin.com.example.invalid")]
    report = await preflight(make_provider(cookies=jar))

    assert report.login is LoginState.NO_SESSION
    assert report.session_cookies == ()


async def test_an_unreadable_cookie_jar_is_unknown_rather_than_logged_out() -> None:
    context = make_context()
    context.cookie_error = RuntimeError("Protocol error")

    report = await preflight(provider_for(context))

    assert report.login is LoginState.UNKNOWN
    assert any("login state is unknown" in warning for warning in report.warnings)
    assert report.ok, "an unreadable jar is worth saying, not worth stopping for"


async def test_the_expiry_is_reported_for_a_live_session() -> None:
    later = (datetime.now(UTC) + timedelta(days=30)).timestamp()
    report = await preflight(make_provider(cookies=logged_in_jar(expires=later)))

    assert report.login is LoginState.LOGGED_IN
    assert report.session_expires_at is not None
    assert report.session_expires_at > datetime.now(UTC)


# --- the fingerprint ---------------------------------------------------------


async def test_a_normal_chrome_reports_no_warnings() -> None:
    report = await preflight(make_provider())

    assert report.warnings == ()
    assert report.fingerprint == Fingerprint(
        user_agent=str(NORMAL_CHROME["userAgent"]),
        platform="MacIntel",
        languages=("en-US", "en"),
        timezone="America/New_York",
        hardware_concurrency=10,
        webdriver=False,
        plugin_count=5,
    )
    assert report.browser_version == "Chrome/140.0.7339.80"


async def test_an_automated_chrome_is_called_out() -> None:
    """The two tells LinkedIn looks for, and the run the user would not want."""
    automated = dict(NORMAL_CHROME)
    automated["userAgent"] = "Mozilla/5.0 HeadlessChrome/140.0.0.0 Safari/537.36"
    automated["webdriver"] = True
    report = await preflight(make_provider(fingerprint=automated))

    warnings = " ".join(report.warnings)
    assert "headless" in warnings
    assert "navigator.webdriver" in warnings
    assert report.fingerprint is not None
    assert report.fingerprint.headless


async def test_a_profile_missing_its_locale_is_called_out() -> None:
    odd = dict(NORMAL_CHROME)
    odd["languages"] = []
    odd["timezone"] = ""
    report = await preflight(make_provider(fingerprint=odd))

    warnings = " ".join(report.warnings)
    assert "navigator.languages" in warnings
    assert "timezone" in warnings


async def test_a_fingerprint_that_will_not_read_is_a_warning_not_a_crash() -> None:
    report = await preflight(make_provider(fingerprint={"unexpected": "shape"}))

    assert report.fingerprint is not None
    assert report.fingerprint.user_agent == ""
    assert "does not report itself as Chrome" in " ".join(report.warnings)


# --- when the browser is not there -------------------------------------------


async def test_a_browser_that_is_not_running_is_a_problem_not_an_exception() -> None:
    connector = FakeConnector(error=OSError("connection refused"))
    report = await preflight(make_provider(connector=connector))

    assert not report.attached
    assert not report.ok
    assert "cannot attach to Chrome" in " ".join(report.problems)
    assert report.fingerprint is None


async def test_preflight_waits_for_nobody_and_reports_busy() -> None:
    """Spec 9.9: the "check session" path takes the same lock as a run.

    The timeout is the same guard its twin in ``tests/test_browser.py`` carries: with
    the busy check gone this would wait forever, and a hung CI job is worse than a red
    one.
    """
    provider = make_provider()

    async with provider.run():
        async with asyncio.timeout(BUSY_TIMEOUT_S):
            report = await preflight(provider)

    assert not report.ok
    assert "already holds the browser" in " ".join(report.problems)


# --- secrets -----------------------------------------------------------------


async def test_no_cookie_value_reaches_the_report_or_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cookies never appear in logs, not even at DEBUG (spec 15)."""
    caplog.set_level(logging.DEBUG)
    report = await preflight(make_provider())

    assert FAKE_SESSION_VALUE not in caplog.text
    assert FAKE_CSRF_VALUE not in caplog.text
    assert FAKE_SESSION_VALUE not in repr(report)
    assert FAKE_CSRF_VALUE not in repr(report)


async def test_the_session_state_is_not_a_classification_outcome() -> None:
    """``LoginState`` and P2-03's ``classify.Outcome`` must never compare equal.

    Both are ``StrEnum``, so two members that share a value are equal, hash alike, and
    pass each other's membership tests. A preflight answer that reads as a classified
    response could be written as a session flag, which is a flag nobody's response
    caused. Keeping the values disjoint is what stops it; ``NO_SESSION`` is this
    module's word for "no cookie in the jar".
    """
    assert "logged_out" not in {state.value for state in LoginState}

    if importlib.util.find_spec("netkeeper.linkedin.classify") is None:
        pytest.skip("classify lands with P2-03 (#143/#144); the rule above holds either way")
    outcome = importlib.import_module("netkeeper.linkedin.classify").Outcome
    overlap = {state.value for state in LoginState} & {member.value for member in outcome}
    assert not overlap, f"LoginState and Outcome share {sorted(overlap)}"


# --- the commands ------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_preflight_command_prints_the_session_and_the_fingerprint(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider())

    result = runner.invoke(cli, ["preflight"])

    assert result.exit_code == 0, result.output
    assert "logged in (li_at, JSESSIONID)" in result.output
    assert "America/New_York" in result.output
    assert "Chrome/140.0.7339.80" in result.output
    assert "ready" in result.output
    assert FAKE_SESSION_VALUE not in result.output


def test_preflight_command_exits_non_zero_when_a_job_could_not_run(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = FakeConnector(error=OSError("connection refused"))
    monkeypatch.setattr(
        "netkeeper.cli.AttachBrowserProvider", lambda cdp_url: make_provider(connector=connector)
    )

    result = runner.invoke(cli, ["preflight"])

    assert result.exit_code == 1
    assert "not ready" in result.output
    assert "netkeeper browser launch" in result.output


def test_browser_launch_prints_the_command_and_runs_nothing(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`browser launch` is instructions. A browser netkeeper starts is ADR 0002's incident."""
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path))

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("netkeeper started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.setattr(os, "posix_spawn", refuse)
    monkeypatch.setattr(os, "execvp", refuse)

    result = runner.invoke(cli, ["browser", "launch"])

    assert result.exit_code == 0, result.output
    assert "--remote-debugging-port=9222" in result.output
    assert str(tmp_path / "chrome-profile") in result.output
    assert "never starts one" in result.output
    assert not (tmp_path / "chrome-profile").exists(), "nothing creates the profile but Chrome"


def test_browser_launch_uses_the_port_from_the_config(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[linkedin]\ncdp_url = "http://127.0.0.1:9333"\n', encoding="utf-8")
    monkeypatch.setenv("NETKEEPER_DATA", str(tmp_path))

    result = runner.invoke(cli, ["--config", str(config), "browser", "launch"])

    assert result.exit_code == 0, result.output
    assert "--remote-debugging-port=9333" in result.output
    assert "http://127.0.0.1:9333" in result.output
