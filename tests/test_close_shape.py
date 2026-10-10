"""The sent bubble's close-control shape, in counts and fixed words (#495).

CP8b's first scheduled auto-send (#479) clicked Send, and the message landed. Then
``close_sent_bubble`` refused: the bubble "does not have one close control for this
person". The diagnostic says which way the lookup missed, without a name, URL, href,
or page text, in the run's note and through ``netkeeper linkedin message-check
--bubble``. These tests drive it on invented bubbles (``messaging_dom``'s fake DOM),
one per relation the report must tell apart.

Every page and name here is invented; nothing is a capture of LinkedIn's page.
"""

from __future__ import annotations

import functools
import logging
import unicodedata
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from factories import make_contact
from messaging_dom import MessagingSite, MessagingTab
from messaging_pages import Member, existing_bubble_html
from run_fakes import fake_provider
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import cli as cli_module
from netkeeper import migrations
from netkeeper.cli import _bubble_check_lines
from netkeeper.cli import app as cli
from netkeeper.config import Settings
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.linkedin import browser
from netkeeper.linkedin.activity_lock import account_key
from netkeeper.linkedin.browser import (
    NO_ONE_CLOSE_CONTROL,
    BrowserRun,
    BubbleCheck,
    CloseButtonShape,
    CloseShape,
    NameRelation,
    NameSource,
    name_relation,
    read_close_shape,
)
from netkeeper.linkedin.classify import Outcome
from netkeeper.linkedin.enrich import LINKEDIN_ORIGIN
from netkeeper.linkedin.page_messaging import PageBubbleCheck
from netkeeper.models import SyncRunKind, SyncRunStatus, SyncRunTrigger, User
from netkeeper.scoping import install_scope_guard
from netkeeper.services import budgets, message_check, runs
from netkeeper.services.budgets import ActionClass
from netkeeper.services.linkedin_accounts import ensure_account
from netkeeper.services.linkedin_session import flag_session
from netkeeper.services.linkedin_steps import BUBBLE_LEFT_OPEN_NOTE
from netkeeper.services.users import ensure_local_user
from netkeeper.worker import BubbleCheckResult, run_bubble_check

#: An invented member, used only here: the no-PII checks look for every part of it.
PERSON = Member(495, "Quorbelle", "Fictionary", "Invented headline for #495")
OTHER = Member(496, "Wendolyn", "Madeupton", "Another invented headline")
FEED = f"{LINKEDIN_ORIGIN}/feed/"
CLOSE = "Close your conversation with "


def assert_no_pii(text: str) -> None:
    """No name, part of a name, slug, profile id, URL or path in ``text``."""
    for member in (PERSON, OTHER):
        for secret in (
            member.first,
            member.last,
            member.slug,
            member.profile_id,
            member.first.casefold(),
            member.last.casefold(),
        ):
            assert secret not in text, secret
    for secret in ("/in/", "http", "linkedin.com", "href=", "Invented"):
        assert secret not in text, secret


def _tab(site: MessagingSite, html: str, *, url: str = FEED) -> MessagingTab:
    tab = MessagingTab(site)
    tab._url = url
    tab.load(html)
    site.pages.append(tab)
    return tab


def _bubble(change: Callable[[str], str] = lambda html: html, member: Member = PERSON) -> str:
    return change(existing_bubble_html(member))


def _close_button(name: str) -> str:
    return f"<button><span>{CLOSE}{name}</span></button>"


def _rename_close(suffix: str) -> Callable[[str], str]:
    """The close button's name past its prefix becomes ``suffix``; the header stays."""

    def change(html: str) -> str:
        old = _close_button(PERSON.name)
        assert old in html
        return html.replace(old, _close_button(suffix))

    return change


def _relink(inner: str, *, attrs: str = "") -> Callable[[str], str]:
    """The header link's content becomes ``inner``; the buttons keep the plain name."""

    def change(html: str) -> str:
        old = f'<a href="/in/{PERSON.profile_id}/">{PERSON.name}</a>'
        assert old in html
        return html.replace(old, f'<a href="/in/{PERSON.profile_id}/"{attrs}>{inner}</a>')

    return change


async def shape_of(html: str) -> CloseShape:
    site = MessagingSite(PERSON)
    tab = _tab(site, html)
    dialog = tab.get_by_role("dialog", name="Messaging", exact=True, include_hidden=True)
    shape = await read_close_shape(cast(Any, tab), cast(Any, dialog))
    assert tab.clicks == [] and tab.keys == [] and tab.focus_calls == [] and tab.fronted == 0
    assert_no_pii(shape.describe())
    assert_no_pii("\n".join(shape.lines()))
    return shape


# --- the relation, word by word -------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "suffix", "relation", "difference"),
    [
        ("Quorbelle Fictionary", "Quorbelle Fictionary", NameRelation.EXACT, 0),
        ("Quorbelle  Fictionary ", "Quorbelle Fictionary", NameRelation.WHITESPACE, 0),
        ("Quorbelle\u00a0Fictionary", "Quorbelle Fictionary", NameRelation.WHITESPACE, 0),
        ("Quor\u200bbelle Fictionary", "Quorbelle Fictionary", NameRelation.WHITESPACE, 0),
        (
            unicodedata.normalize("NFD", "Quorbéllé Fictionary"),
            "Quorbéllé Fictionary",
            NameRelation.UNICODE,
            0,
        ),
        ("Quorbelle Fictionary", "quorbelle fictionary", NameRelation.CASE, 0),
        ("Quorbelle Fictionary Premium", "Quorbelle Fictionary", NameRelation.HEADER_STARTS, 8),
        ("Quorbelle", "Quorbelle Fictionary", NameRelation.SUFFIX_STARTS, -11),
        ("Dr. Quorbelle Fictionary", "Quorbelle Fictionary", NameRelation.HEADER_CONTAINS, 4),
        ("Fictionary", "Dr. Fictionary, PhD", NameRelation.SUFFIX_CONTAINS, -9),
        ("Quorbelle Fictionary", "Wendolyn Madeupton", NameRelation.UNRELATED, 2),
    ],
)
def test_each_relation_and_its_signed_difference(
    header: str, suffix: str, relation: NameRelation, difference: int
) -> None:
    assert name_relation(header, suffix) == (relation, difference)


def test_the_relation_words_are_fixed() -> None:
    assert {r.value for r in NameRelation} == {
        "exact",
        "equal after whitespace normalization",
        "equal after NFC normalization",
        "differ only in case",
        "header name starts with suffix",
        "suffix starts with header name",
        "header name contains suffix",
        "suffix contains header name",
        "unrelated",
    }


# --- the shape, on invented bubbles ---------------------------------------------------------


async def test_the_capture_s_bubble_has_one_exact_close_control() -> None:
    shape = await shape_of(_bubble())
    assert (shape.by_prefix, shape.by_prefix_visible, shape.exact) == (1, 1, 1)
    assert (shape.options_by_prefix, shape.minimize_by_prefix) == (1, 1)
    assert (shape.draft_close, shape.draft_minimize, shape.any_close) == (0, 0, 1)
    [button] = shape.buttons
    assert (button.visible, button.relation, button.difference) == (True, NameRelation.EXACT, 0)
    assert button.source is NameSource.TEXT and button.hidden_text is False
    assert shape.header_source is NameSource.TEXT and shape.header_labelled is False
    assert (shape.header_elements, shape.header_hidden_parts, shape.header_text_extra) == (
        0,
        0,
        0,
    )
    assert shape.header_nfc_changes is False


async def test_a_badge_in_the_header_link_makes_the_header_name_longer() -> None:
    """The run-134 hypothesis: the link's text holds more than the name (a badge)."""
    shape = await shape_of(_bubble(_relink(f"{PERSON.name}<span> Premium</span>")))
    assert shape.exact == 0 and shape.by_prefix == 1
    [button] = shape.buttons
    assert (button.relation, button.difference) == (NameRelation.HEADER_STARTS, 8)
    assert shape.header_elements == 1 and shape.header_hidden_parts == 0
    assert shape.describe() == (
        "header from text, 1 elements; close buttons: 1 by prefix, 1 visible, 0 exact;"
        " #1 visible, header +8, header name starts with suffix"
    )
    assert "suffix vs header name: header name longer by 8" in "\n".join(shape.lines())


async def test_an_aria_hidden_badge_is_counted_and_left_out_of_the_name() -> None:
    html = _bubble(_relink(f'{PERSON.name}<span aria-hidden="true"> 3</span>'))
    shape = await shape_of(html)
    assert shape.exact == 1
    assert shape.header_source is NameSource.SHOWN and shape.header_hidden_parts == 1
    assert shape.header_text_extra == 2
    assert shape.buttons[0].relation is NameRelation.EXACT
    assert shape.describe().startswith(
        "header from text minus aria-hidden, 1 elements, 1 aria-hidden, text +2;"
    )
    assert "its text longer than its name by 2" in "\n".join(shape.lines())


async def test_an_aria_label_on_the_header_link_is_named_as_its_source() -> None:
    html = _bubble(_relink(f"{PERSON.first}", attrs=f' aria-label="{PERSON.name}"'))
    shape = await shape_of(html)
    assert shape.header_source is NameSource.LABEL and shape.header_labelled is True
    assert shape.header_text_extra == -(len(PERSON.last) + 1)


async def test_a_decomposed_header_name_that_nfc_changes_is_named() -> None:
    """The lookup asks with the NFC name; a page whose text is NFD doesn't match it."""
    decomposed = unicodedata.normalize("NFD", "Quorbéllé Fictionary")
    html = existing_bubble_html(PERSON).replace(PERSON.name, decomposed)
    shape = await shape_of(html)
    assert shape.exact == 0 and shape.by_prefix == 1 and shape.header_nfc_changes is True
    [button] = shape.buttons
    assert button.relation is NameRelation.EXACT
    assert "NFC changes it" in shape.describe()


async def test_whitespace_in_the_header_link_is_named() -> None:
    shape = await shape_of(_bubble(_relink(f" {PERSON.first}  {PERSON.last} ")))
    [button] = shape.buttons
    assert button.relation is NameRelation.WHITESPACE and shape.exact == 1


@pytest.mark.parametrize(
    ("suffix", "relation", "difference"),
    [
        (PERSON.name.lower(), NameRelation.CASE, 0),
        (PERSON.first, NameRelation.HEADER_STARTS, len(PERSON.last) + 1),
        (f"{PERSON.name} and others", NameRelation.SUFFIX_STARTS, -11),
        (PERSON.last, NameRelation.HEADER_CONTAINS, len(PERSON.first) + 1),
        (f"Dr. {PERSON.name}", NameRelation.SUFFIX_CONTAINS, -4),
        (OTHER.name, NameRelation.UNRELATED, len(PERSON.name) - len(OTHER.name)),
    ],
)
async def test_each_relation_of_a_renamed_close_control(
    suffix: str, relation: NameRelation, difference: int
) -> None:
    shape = await shape_of(_bubble(_rename_close(suffix)))
    assert shape.exact == 0 and shape.by_prefix == 1
    [button] = shape.buttons
    assert (button.relation, button.difference) == (relation, difference)
    assert relation.value in shape.describe()


async def test_hidden_and_visible_close_controls_are_counted_apart() -> None:
    def two(html: str) -> str:
        hidden = f'<button style="display: none"><span>{CLOSE}{PERSON.name}</span></button>'
        return html.replace("</header>", f"{hidden}</header>", 1)

    shape = await shape_of(_bubble(two))
    assert (shape.by_prefix, shape.by_prefix_visible, shape.exact) == (2, 1, 2)
    assert [b.visible for b in shape.buttons] == [True, False]
    assert [b.hidden_text for b in shape.buttons] == [False, None]
    assert shape.describe().endswith(
        "close buttons: 2 by prefix, 1 visible, 2 exact; #1 visible, same length, exact;"
        " #2 hidden, same length, exact"
    )
    assert "close button 2: hidden" in "\n".join(shape.lines())
    assert "hidden text unknown" in "\n".join(shape.lines())


async def test_hidden_text_in_the_close_control_is_named() -> None:
    """The lookup counts hidden text in a button's name (``include_hidden=True``); the
    header link's name, a visible link's, leaves it out."""

    def icon(html: str) -> str:
        old = _close_button(PERSON.name)
        return html.replace(
            old, f'<button><span>{CLOSE}{PERSON.name}</span><span aria-hidden="true"> x</span>'
        )

    shape = await shape_of(_bubble(icon))
    assert (shape.by_prefix, shape.exact) == (1, 0)
    [button] = shape.buttons
    assert button.relation is NameRelation.EXACT and button.hidden_text is True
    assert button.source is NameSource.SHOWN
    assert shape.describe().endswith("#1 visible, same length, exact, hidden text")
    assert "hidden text changes its name" in "\n".join(shape.lines())


async def test_hidden_text_before_the_prefix_is_counted_without_it() -> None:
    def icon(html: str) -> str:
        old = _close_button(PERSON.name)
        return html.replace(
            old, f'<button><span aria-hidden="true">x </span><span>{CLOSE}{PERSON.name}</span>'
        )

    shape = await shape_of(_bubble(icon))
    assert (shape.by_prefix, shape.by_prefix_shown_names, shape.exact) == (0, 1, 0)
    assert "0 by prefix, 0 visible, 0 exact, 1 without hidden text" in shape.describe()


async def test_the_never_messaged_header_controls_are_counted() -> None:
    def draft(html: str) -> str:
        html = html.replace(
            _close_button(PERSON.name), "<button>Close your draft conversation</button>"
        )
        return html.replace(
            f"<span>Minimize your conversation with {PERSON.name}</span>",
            "<span>Minimize your conversation</span>",
        )

    shape = await shape_of(_bubble(draft))
    assert (shape.by_prefix, shape.exact, shape.draft_close, shape.any_close) == (0, 0, 1, 1)
    assert (shape.minimize_by_prefix, shape.draft_minimize, shape.options_by_prefix) == (0, 1, 1)
    assert shape.buttons == ()
    assert shape.describe().endswith("1 any Close, 1 draft Close")
    assert "named 'Close your draft conversation': 1" in "\n".join(shape.lines())


async def test_a_close_control_outside_the_dialog_is_counted_on_the_page() -> None:
    def move(html: str) -> str:
        return html.replace(_close_button(PERSON.name), "") + _close_button(PERSON.name)

    shape = await shape_of(_bubble(move))
    assert (shape.by_prefix, shape.on_page_by_prefix) == (0, 1)


async def test_an_unreadable_header_name_says_so() -> None:
    html = _bubble(_relink(PERSON.name, attrs=' aria-labelledby="nk-495-label"')).replace(
        "</header>", '<span id="nk-495-label">Someone</span></header>', 1
    )
    shape = await shape_of(html)
    assert shape.header_source is None and shape.exact is None
    [button] = shape.buttons
    assert button.relation is None and button.why == "no header name to compare"
    assert "header name unreadable" in shape.describe()


async def test_two_header_links_are_counted_and_not_read() -> None:
    html = _bubble(_relink(f"{PERSON.name}</a><a href='/in/{OTHER.profile_id}/'>{OTHER.name}"))
    shape = await shape_of(html)
    assert shape.header_links == 2 and shape.header_source is None
    assert "2 header links" in shape.describe()


async def test_at_most_four_close_controls_are_described() -> None:
    def many(html: str) -> str:
        return html.replace("</header>", _close_button(PERSON.name) * 5 + "</header>", 1)

    shape = await shape_of(_bubble(many))
    assert shape.by_prefix == 6 and len(shape.buttons) == 4
    assert "(only the first 4 close buttons are described)" in shape.lines()


# --- the refusal ------------------------------------------------------------------------------


async def test_the_refusal_keeps_its_words_when_the_shape_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def boom(*_: Any) -> CloseShape:
        raise RuntimeError(f"locator for {PERSON.name} timed out")

    monkeypatch.setattr(browser, "read_close_shape", boom)
    site = MessagingSite(PERSON)
    tab = _tab(site, _bubble(_rename_close(OTHER.name)))
    dialog = tab.get_by_role("dialog", name="Messaging", exact=True, include_hidden=True)
    with caplog.at_level(logging.DEBUG):
        refusal = await browser._no_one_close_control(cast(Any, tab), cast(Any, dialog))
    assert refusal == f"{NO_ONE_CLOSE_CONTROL} (its shape could not be read)"
    assert "the close control's shape could not be read (RuntimeError)" in caplog.text
    assert "timed out" not in caplog.text and "locator" not in caplog.text
    assert_no_pii(caplog.text)


def test_the_refusal_s_words_are_pinned() -> None:
    assert NO_ONE_CLOSE_CONTROL == "the sent bubble does not have one close control for this person"


# --- the bubble check: finding the tab, reading only ----------------------------------------


async def bubble_check(site: MessagingSite) -> tuple[BubbleCheck, BrowserRun]:
    provider, _ = fake_provider(site)
    async with provider.run() as run:
        found = await PageBubbleCheck(run).run(PERSON.profile_id)
    return found, run


def assert_untouched(site: MessagingSite, tabs: list[MessagingTab]) -> None:
    assert site.new_page_calls == 0 and site.navigations == []
    for tab in tabs:
        assert tab.clicks == [] and tab.keys == [] and tab.attempts == []
        assert tab.focus_calls == [] and tab.fronted == 0 and tab.goto_calls == []
        assert not tab.is_closed()


async def test_the_bubble_check_reads_the_tab_that_holds_the_contact_s_bubble() -> None:
    site = MessagingSite(PERSON)
    blank = _tab(site, "<main></main>", url="about:blank")
    elsewhere = _tab(site, _bubble(), url="https://example.invalid/")
    other = _tab(site, _bubble(member=OTHER))
    mine = _tab(site, _bubble(_relink(f"{PERSON.name}<span> Premium</span>")))
    found, _ = await bubble_check(site)
    assert (found.tabs, found.on_origin, found.with_bubble, found.for_contact) == (4, 2, 2, 1)
    assert (found.dialogs, found.dialogs_for_contact, found.composers) == (1, 1, 1)
    assert found.composer_in_dialog is True
    assert found.shape is not None and found.shape.exact == 0
    assert_untouched(site, [blank, elsewhere, other, mine])
    assert elsewhere.reads == 0 and blank.reads == 0
    report = "\n".join(_bubble_check_lines(9, BubbleCheckResult(found)))
    assert_no_pii(report)
    assert "with this contact's bubble: 1" in report
    assert "header name longer by 8, header name starts with suffix" in report


async def test_no_tab_with_the_contact_s_bubble_reports_counts_only() -> None:
    site = MessagingSite(PERSON)
    tabs = [_tab(site, _bubble(member=OTHER)), _tab(site, "<main></main>")]
    found, _ = await bubble_check(site)
    assert (found.on_origin, found.with_bubble, found.for_contact) == (2, 1, 0)
    assert found.shape is None and found.dialogs is None
    assert_untouched(site, tabs)
    report = "\n".join(_bubble_check_lines(9, BubbleCheckResult(found)))
    assert "If the tab auto-send left open is still open, run this against it" in report
    assert "open the bubble by hand in the netkeeper Chrome window" in report
    assert_no_pii(report)


async def test_two_tabs_with_the_contact_s_bubble_are_counted_and_the_first_read() -> None:
    site = MessagingSite(PERSON)
    first = _tab(site, _bubble(_rename_close(PERSON.first)))
    second = _tab(site, _bubble())
    found, _ = await bubble_check(site)
    assert found.for_contact == 2 and found.shape is not None
    [button] = found.shape.buttons
    assert button.relation is NameRelation.HEADER_STARTS  # the first tab's bubble
    assert_untouched(site, [first, second])
    report = "\n".join(_bubble_check_lines(9, BubbleCheckResult(found)))
    assert "2 tabs hold this contact's bubble; this reads the first" in report
    assert_no_pii(report)


async def test_a_second_dialog_in_the_tab_is_counted() -> None:
    site = MessagingSite(PERSON)
    tab = _tab(site, _bubble(member=OTHER) + _bubble())
    found, _ = await bubble_check(site)
    assert (found.for_contact, found.dialogs, found.dialogs_for_contact) == (1, 2, 1)
    assert found.composers == 2 and found.shape is not None and found.shape.exact == 1
    assert_untouched(site, [tab])


def test_the_bubble_check_opens_only_linkedin_or_loopback() -> None:
    with pytest.raises(ValueError, match="never"):
        PageBubbleCheck(object(), origin="https://example.invalid")  # type: ignore[arg-type]


# --- the command and its run ----------------------------------------------------------------


@pytest.fixture
def cli_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    engine = make_engine(url)
    migrations.upgrade(engine)
    factory = make_session_factory(engine)
    install_scope_guard(factory)
    with session_scope(factory, write=True) as session:
        ensure_local_user(session, settings=Settings())
    yield factory
    engine.dispose()


def _user(session: Session) -> User:
    return ensure_local_user(session, settings=Settings())


def _contact(factory: sessionmaker[Session]) -> int:
    with session_scope(factory, write=True) as session:
        return make_contact(
            session,
            _user(session),
            li_urn=PERSON.urn,
            li_public_id=PERSON.slug,
            first_name=PERSON.first,
            last_name=PERSON.last,
        ).id


def _visits(factory: sessionmaker[Session]) -> int:
    with session_scope(factory) as session:
        user = _user(session)
        return budgets.status(
            session,
            user,
            ensure_account(session, user).id,
            ActionClass.PROFILE_VISITS,
            now=datetime.now(UTC),
            settings=Settings().linkedin.budget,
        ).day.count


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> MessagingSite:
    site = MessagingSite(PERSON)
    provider, _ = fake_provider(site)
    monkeypatch.setattr(cli_module, "_provider", lambda settings: provider)
    monkeypatch.setattr(cli_module, "run_bubble_check", functools.partial(run_bubble_check))
    return site


@pytest.mark.usefixtures("inside_active_hours")
def test_the_command_reports_the_shape_and_records_a_run_without_a_visit(
    cli_db: sessionmaker[Session], chrome: MessagingSite
) -> None:
    tab = _tab(chrome, _bubble(_rename_close(PERSON.first)))
    contact_id = _contact(cli_db)
    result = CliRunner().invoke(cli, ["linkedin", "message-check", str(contact_id), "--bubble"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "--- bubble check report (no names, URLs, hrefs, or page text) ---" in out
    assert "close lookup by the exact name: 0" in out
    assert "header name starts with suffix" in out
    assert "run 1: completed (read the open message bubble's close control" in out
    assert_no_pii(out)
    assert_untouched(chrome, [tab])
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), 1)
        assert (run.kind, run.trigger, run.status, run.stop_reason) == (
            SyncRunKind.MESSAGE_SEND,
            SyncRunTrigger.MANUAL,
            SyncRunStatus.COMPLETED,
            message_check.BUBBLE_CHECK_STOP,
        )
        assert run.notes == message_check.BUBBLE_CHECK_NOTE and run.error is None
    assert _visits(cli_db) == 0


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_session_flagged_after_the_start_refuses_before_the_read(
    cli_db: sessionmaker[Session],
) -> None:
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        target = message_check.start(
            session,
            _user(session),
            contact_id,
            now=datetime.now(UTC),
            settings=Settings(),
            bubble=True,
        )
        flag_session(session, _user(session), Outcome.CHECKPOINT, url="/checkpoint/x")
    site = MessagingSite(PERSON)
    tab = _tab(site, _bubble())
    provider, _ = fake_provider(site)
    result = await run_bubble_check(provider, cli_db, 1, target, settings=Settings())
    assert result.bubble is None and tab.reads == 0
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.FAILED, "session_flagged")


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_busy_browser_fails_the_bubble_check(cli_db: sessionmaker[Session]) -> None:
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        target = message_check.start(
            session,
            _user(session),
            contact_id,
            now=datetime.now(UTC),
            settings=Settings(),
            bubble=True,
        )
    provider, _ = fake_provider(MessagingSite(PERSON))
    async with provider.run(account_key(target.account_id)):  # another run holds it
        result = await run_bubble_check(provider, cli_db, 1, target, settings=Settings())
    assert result.stopped == "the browser was busy with another run"


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_bubble_check_failure_records_only_its_type(
    cli_db: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    contact_id = _contact(cli_db)
    with session_scope(cli_db, write=True) as session:
        target = message_check.start(
            session,
            _user(session),
            contact_id,
            now=datetime.now(UTC),
            settings=Settings(),
            bubble=True,
        )

    async def boom(self: BrowserRun, *args: object) -> BubbleCheck:
        raise ValueError(f"name={PERSON.name} timed out")

    monkeypatch.setattr(BrowserRun, "bubble_check", boom)
    provider, _ = fake_provider(MessagingSite(PERSON))
    with caplog.at_level(logging.DEBUG):
        result = await run_bubble_check(provider, cli_db, 1, target, settings=Settings())
    assert result.stopped == "the check failed (ValueError)"
    assert f"bubble check run {target.run_id} failed (ValueError)" in caplog.text
    assert "timed out" not in caplog.text and "name=" not in caplog.text
    for secret in (PERSON.first, PERSON.last, PERSON.slug, PERSON.profile_id):
        assert secret not in caplog.text
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert run.status is SyncRunStatus.FAILED and run.error == "the check failed (ValueError)"
        assert_no_pii(run.error or "")


def test_the_bubble_check_s_stop_reason_has_plain_words() -> None:
    assert message_check.BUBBLE_CHECK_STOP == "bubble_check"
    assert runs.STOP_REASON_TEXT["bubble_check"] == (
        "read the open message bubble's close control; nothing was clicked"
    )


def test_the_diagnostic_s_names_are_pinned() -> None:
    assert browser.CLOSE_CONTROL_PATTERN.pattern == "^Close your conversation with "
    assert browser.OPTIONS_CONTROL_PATTERN.pattern == (
        "^Open the options list in your conversation with "
    )
    assert browser.MINIMIZE_CONTROL_PATTERN.pattern == "^Minimize your conversation with "
    assert browser.ANY_CLOSE_PATTERN.pattern == r"^close\b"
    assert (browser.DRAFT_CLOSE_NAME, browser.DRAFT_MINIMIZE_NAME) == (
        "Close your draft conversation",
        "Minimize your conversation",
    )
    assert (browser.CLOSE_SHAPE_MAX, browser.BUBBLE_CHECK_MAX_DIALOGS) == (4, 8)


# --- review follow-ups ------------------------------------------------------------------------


async def test_a_close_control_whose_name_cannot_be_read_says_so_in_fixed_words() -> None:
    """A name given only through ``aria-labelledby`` can't be read without script."""

    def labelled(html: str) -> str:
        return html.replace(
            _close_button(PERSON.name),
            '<button aria-labelledby="nk-495-close"><span>x</span></button>'
            f'<span id="nk-495-close" hidden>{CLOSE}{PERSON.name}</span>',
        )

    shape = await shape_of(_bubble(labelled))
    assert shape.by_prefix == 1
    [button] = shape.buttons
    assert button.relation is None and button.why == "its name could not be read"
    assert shape.describe().endswith("#1 visible, its name could not be read")


def _worst_shape(buttons: int) -> CloseShape:
    return CloseShape(
        by_prefix=99,
        by_prefix_visible=98,
        by_prefix_shown_names=97,
        on_page_by_prefix=96,
        any_close=95,
        draft_close=94,
        options_by_prefix=93,
        minimize_by_prefix=92,
        draft_minimize=91,
        exact=90,
        header_links=1,
        header_source=NameSource.SHOWN,
        header_labelled=True,
        header_elements=999,
        header_hidden_parts=999,
        header_text_extra=-999,
        header_nfc_changes=True,
        buttons=tuple(
            CloseButtonShape(False, NameRelation.WHITESPACE, -999, NameSource.SHOWN, True)
            for _ in range(buttons)
        ),
    )


def _note(shape: CloseShape) -> str:
    """The run's note line as auto-send writes it, with the in-front note after it."""
    left_open = f"{BUBBLE_LEFT_OPEN_NOTE}{NO_ONE_CLOSE_CONTROL} ({shape.describe()})"
    return runs._line(f"{left_open} {runs.OPENED_IN_FRONT_NOTE}")


async def test_a_two_button_note_and_the_in_front_note_both_stay_whole() -> None:
    def two(html: str) -> str:
        hidden = f'<button style="display: none"><span>{CLOSE}{PERSON.first}</span></button>'
        return html.replace("</header>", f"{hidden}</header>", 1)

    shape = await shape_of(_bubble(two))
    assert len(shape.buttons) == 2
    note = _note(shape)
    assert note.endswith(f"({shape.describe()}) {runs.OPENED_IN_FRONT_NOTE}")
    assert "#1 visible" in note and "#2 hidden" in note and "more" not in note


def test_the_worst_case_note_leaves_room_for_the_in_front_note() -> None:
    shape = _worst_shape(4)
    words = shape.describe()
    assert len(words) <= browser.CLOSE_SHAPE_NOTE_MAX == 280
    assert words.startswith("header from text minus aria-hidden, has aria-label,")
    assert words.endswith("more")
    assert _note(shape).endswith(runs.OPENED_IN_FRONT_NOTE)
    assert len(_note(shape)) <= runs.MAX_MESSAGE_LENGTH


def test_the_note_counts_the_buttons_it_does_not_describe() -> None:
    shape = CloseShape(
        by_prefix=4,
        by_prefix_visible=4,
        by_prefix_shown_names=4,
        on_page_by_prefix=4,
        any_close=4,
        draft_close=0,
        options_by_prefix=1,
        minimize_by_prefix=1,
        draft_minimize=0,
        exact=0,
        header_links=1,
        header_source=NameSource.TEXT,
        header_labelled=False,
        header_elements=1,
        header_hidden_parts=0,
        header_text_extra=8,
        header_nfc_changes=False,
        buttons=tuple(
            CloseButtonShape(True, NameRelation.HEADER_STARTS, 8, NameSource.TEXT, False)
            for _ in range(4)
        ),
    )
    assert shape.describe() == (
        "header from text, 1 elements, text +8; close buttons: 4 by prefix, 4 visible,"
        " 0 exact; #1 visible, header +8, header name starts with suffix; #2 visible,"
        " header +8, header name starts with suffix; +2 more"
    )


def test_the_worst_case_note_counts_every_button_it_has_no_room_for() -> None:
    assert _worst_shape(4).describe().endswith("94 draft Close; +99 more")


async def _cancelled_target(factory: sessionmaker[Session]) -> message_check.CheckTarget:
    contact_id = _contact(factory)
    with session_scope(factory, write=True) as session:
        return message_check.start(
            session,
            _user(session),
            contact_id,
            now=datetime.now(UTC),
            settings=Settings(),
            bubble=True,
        )


def _cancel(factory: sessionmaker[Session], run_id: int) -> None:
    with session_scope(factory, write=True) as session:
        runs.request_cancel(
            session, _user(session), run_id, now=datetime.now(UTC), browser_held=lambda _: True
        )


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_cancel_before_the_read_ends_the_run_aborted(
    cli_db: sessionmaker[Session],
) -> None:
    target = await _cancelled_target(cli_db)
    _cancel(cli_db, target.run_id)
    site = MessagingSite(PERSON)
    tab = _tab(site, _bubble())
    provider, _ = fake_provider(site)
    result = await run_bubble_check(provider, cli_db, 1, target, settings=Settings())
    assert result.bubble is None and tab.reads == 0
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, runs.CANCELLED)


@pytest.mark.usefixtures("inside_active_hours")
async def test_a_cancel_during_the_read_ends_the_run_aborted(
    cli_db: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    target = await _cancelled_target(cli_db)
    real = BrowserRun.bubble_check

    async def then_cancel(self: BrowserRun, profile_id: str, origin: str) -> BubbleCheck:
        found = await real(self, profile_id, origin)
        _cancel(cli_db, target.run_id)
        return found

    monkeypatch.setattr(BrowserRun, "bubble_check", then_cancel)
    site = MessagingSite(PERSON)
    _tab(site, _bubble())
    provider, _ = fake_provider(site)
    result = await run_bubble_check(provider, cli_db, 1, target, settings=Settings())
    assert result.stopped == "cancelled" and result.bubble is not None
    with session_scope(cli_db) as session:
        run = runs.get_run(session, _user(session), target.run_id)
        assert (run.status, run.stop_reason) == (SyncRunStatus.ABORTED, runs.CANCELLED)
