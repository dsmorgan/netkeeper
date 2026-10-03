"""`netkeeper posture` and `netkeeper rehearse`: two thirds of CP3's demo (P2-11).

CP3 is the checkpoint where the extractor's safety is reviewed before the first
live run, and these are the commands it is reviewed through. So what is tested
here is the command as somebody types it: what it prints, what it exits with,
and -- for ``rehearse`` -- that pointing it at LinkedIn is refused at the
command line and not only inside the function.

``rehearse`` is driven with the real loopback replica the command starts for
itself and a fake browser in place of Chrome, so the wiring under test is the
whole command bar the CDP connection: the replica really serves, the pacing
plan is really built, and the log is really rendered. The Chrome half is
``tests/smoke/test_rehearse_smoke.py``, opt-in behind ``NETKEEPER_BROWSER_TESTS=1``.
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from browser_fakes import FakeBrowser, FakeConnector
from sqlalchemy.orm import Session, sessionmaker
from test_rehearse import ReplayContext
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import _session_probe
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin.browser import AttachBrowserProvider, BrowserUnavailable
from netkeeper.linkedin.preflight import Fingerprint, LoginState, PreflightReport
from netkeeper.models import User, UserKind
from netkeeper.scoping import install_scope_guard
from netkeeper.services import heat as heat_rows
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.posture import SINGLE_ACCOUNT_ID
from netkeeper.services.scheduler import sync_account_schedule
from netkeeper.services.users import ensure_local_user


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A migrated database with the local user, pointed to by ``NETKEEPER_DATABASE_URL``."""
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        user = ensure_local_user(session, settings=Settings())
        # The state `netkeeper serve` leaves behind at start: a schedule for
        # every job kind. Without it the scheduler-side protections have
        # nothing to act on and posture says so, which is its own test in
        # tests/test_posture.py rather than a permanent warning here.
        sync_account_schedule(
            session,
            user,
            SINGLE_ACCOUNT_ID,
            now=datetime.now(UTC),
            rng=random.Random(4),
            tz=Settings().linkedin.timezone,
        )
    yield factory
    engine.dispose()


@pytest.fixture
def fake_chrome(monkeypatch: pytest.MonkeyPatch) -> ReplayContext:
    """Stand a fake browser in for the Chrome the command would attach to.

    ``fetch=True``: the fake tab really fetches each url over loopback and
    reports the status it got. That is what makes these tests evidence that the
    command starts a replica which actually serves -- a tab that fabricated a
    200 would pass just as happily against a port nothing is listening on.
    """
    context = ReplayContext(fetch=True)
    connector = FakeConnector([FakeBrowser([context])])

    def provider(_settings: Settings) -> AttachBrowserProvider:
        return AttachBrowserProvider("http://127.0.0.1:9222", connector=connector)

    monkeypatch.setattr("netkeeper.cli._provider", provider)
    return context


# --- netkeeper posture ----------------------------------------------------------


def test_posture_prints_the_table_of_protections(cli_db: sessionmaker[Session]) -> None:
    result = CliRunner().invoke(cli, ["posture", "--no-probe", "--details"])

    assert "PROTECTION" in result.output and "DETAIL" in result.output
    assert "attach-only browser" in result.output
    assert "budget profile_visits" in result.output
    assert "heat" in result.output
    assert "today's profile-visit budget:" in result.output
    assert "heat skip gate" in result.output
    assert "JOB" in result.output and "NEXT DUE" in result.output
    assert "not covered by this report:" in result.output
    assert "hard max" in result.output


def test_posture_leads_with_a_summary_and_keeps_the_details_for_the_flag(
    cli_db: sessionmaker[Session],
) -> None:
    """#340: one line per protection by default; --details adds the rest."""
    summary = CliRunner().invoke(cli, ["posture", "--no-probe"])
    details = CliRunner().invoke(cli, ["posture", "--no-probe", "--details"])

    assert "PROTECTION" in summary.output and "SUMMARY" in summary.output
    assert "DETAIL" not in summary.output
    for name in ("attach-only browser", "budget profile_visits", "scheduled jobs", "reply poll"):
        assert name in summary.output and name in details.output
    for detail in ("today's profile-visit budget:", "NEXT DUE", "not covered by this report:"):
        assert detail not in summary.output
        assert detail in details.output
    assert "hard max" not in summary.output
    assert "run `netkeeper posture --details`" in summary.output
    assert len(summary.output.splitlines()) < len(details.output.splitlines())


def test_posture_shows_a_warning_in_the_summary_and_exits_the_same_either_way(
    cli_db: sessionmaker[Session],
) -> None:
    """#340: a warning that makes posture not ok is never behind --details, and the exit
    code does not depend on the flag."""
    summary = CliRunner().invoke(cli, ["posture", "--no-probe"])
    details = CliRunner().invoke(cli, ["posture", "--no-probe", "--details"])

    assert summary.exit_code == details.exit_code == 1
    assert "warning: linkedin session: nothing has checked the LinkedIn session yet" in (
        summary.output
    )
    assert summary.output.rstrip().splitlines()[-1] == details.output.rstrip().splitlines()[-1]
    assert "NOT clear" in summary.output


def test_posture_exits_non_zero_when_anything_warned(cli_db: sessionmaker[Session]) -> None:
    """Without a probe the LinkedIn session is unknown, which is a warning, not an assumption."""
    result = CliRunner().invoke(cli, ["posture", "--no-probe"])

    assert result.exit_code == 1
    assert "NOT clear" in result.output
    assert "nothing has checked the LinkedIn session yet" in result.output


def test_posture_reports_a_browser_it_could_not_attach_to(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that failed is reported, not swallowed and not fatal to the rest of the table."""

    def unreachable(_settings: Settings) -> AttachBrowserProvider:
        return AttachBrowserProvider(
            "http://127.0.0.1:9222",
            connector=FakeConnector(error=BrowserUnavailable("Chrome is not running")),
        )

    monkeypatch.setattr("netkeeper.cli._provider", unreachable)

    result = CliRunner().invoke(cli, ["posture"])

    assert result.exit_code == 1
    assert "not attached" in result.output
    assert "Chrome is not running" in result.output
    assert "budget profile_visits" in result.output  # the rest of the report still came


def test_posture_is_all_clear_when_the_probe_finds_a_session(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CP3 demo: "the posture page with every protection on"."""

    def logged_in(_settings: Settings) -> AttachBrowserProvider:
        jar = [{"name": "li_at", "value": "fake", "domain": ".linkedin.com", "expires": -1}]
        context = ReplayContext()
        context.cookie_jar = list(jar)
        context.evaluate_result = {
            "userAgent": "Mozilla/5.0 Chrome/140.0.7339.80",
            "languages": ["en-US"],
            "timezone": "America/New_York",
        }
        return AttachBrowserProvider(
            "http://127.0.0.1:9222", connector=FakeConnector([FakeBrowser([context])])
        )

    monkeypatch.setattr("netkeeper.cli._provider", logged_in)

    result = CliRunner().invoke(cli, ["posture"])

    assert result.exit_code == 0, result.output
    assert "nothing is misconfigured" in result.output
    assert "logged in (li_at)" in result.output
    assert "NOT clear" not in result.output


def test_posture_stays_clear_and_exits_zero_above_100_a_day_with_the_note_listed(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#319 review S1: the profile-visit risk is a note, not a warning. A script
    gated on `netkeeper posture` keeps passing; the reader still sees why."""

    def logged_in(_settings: Settings) -> AttachBrowserProvider:
        jar = [{"name": "li_at", "value": "fake", "domain": ".linkedin.com", "expires": -1}]
        context = ReplayContext()
        context.cookie_jar = list(jar)
        context.evaluate_result = {
            "userAgent": "Mozilla/5.0 Chrome/140.0.7339.80",
            "languages": ["en-US"],
            "timezone": "America/New_York",
        }
        return AttachBrowserProvider(
            "http://127.0.0.1:9222", connector=FakeConnector([FakeBrowser([context])])
        )

    monkeypatch.setattr("netkeeper.cli._provider", logged_in)
    config = tmp_path / "318-risky.toml"
    config.write_text("[linkedin.budget]\nprofile_visits_per_day = 101\n", encoding="utf-8")

    result = CliRunner().invoke(cli, ["--config", str(config), "posture", "--details"])

    assert result.exit_code == 0, result.output
    assert "nothing is misconfigured" in result.output
    assert "NOT clear" not in result.output
    assert "note: budget profile_visits: Profile visits are set to 101 a day" in result.output
    assert "warning:" not in result.output

    # The summary (#340) leaves the note itself out, says the row has one, and
    # exits the same way.
    summary = CliRunner().invoke(cli, ["--config", str(config), "posture"])
    assert summary.exit_code == 0, summary.output
    assert "Profile visits are set to 101 a day" not in summary.output
    assert "(1 note)" in summary.output
    assert "1 note, " in summary.output
    assert "nothing is misconfigured" in summary.output


@pytest.mark.parametrize(
    ("state", "expected"),
    [(LoginState.LOGGED_IN, True), (LoginState.NO_SESSION, False), (LoginState.UNKNOWN, None)],
)
def test_a_preflight_report_becomes_a_probe_with_no_place_for_a_secret(
    state: LoginState, expected: bool | None
) -> None:
    report = PreflightReport(
        cdp_url="http://127.0.0.1:9222",
        attached=True,
        browser_version="Chrome/140",
        login=state,
        session_cookies=("li_at",),
        fingerprint=Fingerprint(user_agent="Chrome/140"),
    )

    probe = _session_probe(report)

    assert probe.logged_in is expected
    assert probe.cookie_names == ("li_at",)
    # The probe's whole field list, so a field that could hold a value cannot be
    # added without this test being edited on purpose.
    assert set(probe.__dataclass_fields__) == {
        "attached",
        "logged_in",
        "browser_version",
        "cookie_names",
        "problems",
    }


# --- netkeeper rehearse ----------------------------------------------------------


def test_rehearse_starts_its_own_replica_and_prints_the_request_log(
    fake_chrome: ReplayContext,
) -> None:
    result = CliRunner().invoke(cli, ["rehearse", "--visits", "2", "--scale", "5000"])

    assert result.exit_code == 0, result.output
    assert "VISIT 1" in result.output and "VISIT 2" in result.output
    assert "loopback only; linkedin.com is refused" in result.output
    assert "/in/rehearsal-alex-doe/" in result.output
    assert "/static/replica.css" in result.output
    assert "Nothing reached linkedin.com" in result.output
    assert "1 host(s): 127.0.0.1" in result.output
    assert fake_chrome.new_page_calls == 1
    # Every status came from the replica the command started, not from the fake.
    assert result.output.count("200") >= 6


def test_rehearse_shuts_its_replica_down_when_it_is_done(fake_chrome: ReplayContext) -> None:
    """A loopback server left listening after every run is a leak nobody would notice."""
    result = CliRunner().invoke(cli, ["rehearse", "--visits", "1", "--scale", "5000"])
    match = re.search(r"site        (http://127\.0\.0\.1:\d+)", result.output)

    assert match is not None, result.output
    with pytest.raises(httpx.HTTPError):
        httpx.get(match.group(1), timeout=2)


def test_rehearse_is_repeatable_from_the_seed_it_prints(fake_chrome: ReplayContext) -> None:
    first = CliRunner().invoke(
        cli, ["rehearse", "--visits", "3", "--seed", "77", "--scale", "5000"]
    )
    second = CliRunner().invoke(
        cli, ["rehearse", "--visits", "3", "--seed", "77", "--scale", "5000"]
    )

    assert first.exit_code == 0 and second.exit_code == 0
    assert "seed        77" in first.output
    assert _scroll_lines(first.output) == _scroll_lines(second.output)


def _scroll_lines(output: str) -> list[str]:
    """The pacing decisions, which are what a seed is supposed to fix."""
    return [line.strip() for line in output.splitlines() if line.strip().startswith("scrolled")]


def test_rehearse_writes_the_log_where_asked(fake_chrome: ReplayContext, tmp_path: Path) -> None:
    target = tmp_path / "cp3-request-log.txt"

    result = CliRunner().invoke(
        cli, ["rehearse", "--visits", "1", "--scale", "5000", "--log", str(target)]
    )

    assert result.exit_code == 0, result.output
    written = target.read_text(encoding="utf-8")
    assert "VISIT 1" in written
    assert written in result.output  # what was written is what was shown
    assert f"wrote the request log to {target}" in result.output


@pytest.mark.parametrize(
    "site",
    ["https://www.linkedin.com", "https://www.linkedin.com/in/someone", "http://example.test"],
)
def test_rehearse_refuses_a_site_that_is_not_the_loopback_replica(
    fake_chrome: ReplayContext, site: str
) -> None:
    result = CliRunner().invoke(cli, ["rehearse", "--site", site, "--scale", "5000"])

    assert result.exit_code == 1
    assert "error:" in result.output
    assert fake_chrome.new_page_calls == 0  # refused before a tab was ever opened


def test_rehearse_says_so_when_it_is_not_waiting_what_a_run_would(
    fake_chrome: ReplayContext,
) -> None:
    scaled = CliRunner().invoke(cli, ["rehearse", "--visits", "1", "--scale", "5000"])

    assert "SCALED" in scaled.output
    assert "This is not what a run waits" in scaled.output


def test_rehearse_reports_a_browser_it_cannot_attach_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unreachable(_settings: Settings) -> AttachBrowserProvider:
        return AttachBrowserProvider(
            "http://127.0.0.1:9222",
            connector=FakeConnector(error=BrowserUnavailable("Chrome is not running")),
        )

    monkeypatch.setattr("netkeeper.cli._provider", unreachable)

    result = CliRunner().invoke(cli, ["rehearse", "--visits", "1", "--scale", "5000"])

    assert result.exit_code == 1
    assert "Chrome is not running" in result.output


def test_rehearse_uses_the_pacing_in_the_config_file(
    fake_chrome: ReplayContext, tmp_path: Path
) -> None:
    """The fidelity claim, at the command line.

    A config asking for a 1-second median must produce 1-second waits in the
    log. Before the settings were threaded through, the rehearsal used the
    pacing module's 25-second default and the log said 25 while the live run
    would have used 1 -- the exact inversion of what rehearsing is for.
    """
    config = tmp_path / "config.toml"
    config.write_text(
        "[linkedin.pacing]\n"
        "profile_delay_median_s = 1\n"
        "profile_delay_sigma = 0.01\n"
        "distraction_p = 0.0\n",
        encoding="utf-8",
    )

    brisk = CliRunner().invoke(
        cli,
        ["--config", str(config), "rehearse", "--visits", "3", "--seed", "5", "--scale", "5000"],
    )
    default = CliRunner().invoke(
        cli, ["rehearse", "--visits", "3", "--seed", "5", "--scale", "5000"]
    )

    assert brisk.exit_code == 0, brisk.output
    assert _planned_waits(brisk.output), brisk.output
    assert max(_planned_waits(brisk.output)) < 3.0
    assert max(_planned_waits(default.output)) > 5.0


def _planned_waits(output: str) -> list[float]:
    """The "Ns planned" figures the log prints between profile visits."""
    return [float(value) for value in re.findall(r"([\d.]+)s planned", output)]


def test_posture_reads_the_mode_off_the_provider_it_would_run_with(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not off the class, which would make the attach-only row tautological.

    ``AttachBrowserProvider.mode`` is a class attribute that is always
    ``"attach"``, so reading it compares a constant with itself. The instance
    below shadows it, which is the shape a future second provider would have.
    """

    def odd(_settings: Settings) -> AttachBrowserProvider:
        provider = AttachBrowserProvider("http://127.0.0.1:9222", connector=FakeConnector())
        provider.mode = "launch"
        return provider

    monkeypatch.setattr("netkeeper.cli._provider", odd)

    result = CliRunner().invoke(cli, ["posture", "--no-probe"])

    assert result.exit_code == 1
    assert "second device" in result.output
    assert AttachBrowserProvider.mode == "attach", "the class attribute is untouched"


def test_the_default_rehearsal_is_short_enough_to_watch() -> None:
    """Three visits at the default pacing is about a minute; the demo has to finish."""
    from netkeeper.cli import DEFAULT_REHEARSAL_VISITS

    assert DEFAULT_REHEARSAL_VISITS == 3


def test_a_rehearsal_started_now_records_when(fake_chrome: ReplayContext) -> None:
    before = datetime.now(UTC)

    result = CliRunner().invoke(cli, ["rehearse", "--visits", "1", "--scale", "5000"])

    assert f"started     {before:%Y-%m-%d %H:%M}" in result.output


def test_posture_reads_the_local_users_own_account_by_default(
    cli_db: sessionmaker[Session],
) -> None:
    """Budgets and heat are keyed by the account row (P2-06), not by a hardcoded 1.

    A hosted user's account takes id 1 here, so the local user's is 2; heat
    raised on account 2 must show up in a plain ``netkeeper posture``, and
    ``--account 1`` must still read the other one.
    """
    with session_scope(cli_db, write=True) as session:
        hosted = User(kind=UserKind.HOSTED, timezone="UTC")
        session.add(hosted)
        session.flush()
        assert ensure_account(session, hosted).id == 1
        local = ensure_local_user(session, settings=Settings())
        account = ensure_account(session, local).id
        assert account == 2
        heat_rows.raise_heat(
            session, local, account, now=datetime.now(UTC), settings=Settings().linkedin.heat
        )

    by_default = CliRunner().invoke(cli, ["posture", "--no-probe"])
    explicit_other = CliRunner().invoke(cli, ["posture", "--no-probe", "--account", "1"])

    assert "last raised" in by_default.output
    assert "last raised" not in explicit_other.output
