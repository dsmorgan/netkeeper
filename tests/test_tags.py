"""netkeeper.crm.tags (spec 8.3, 10.3): tags, assignments, suppressions, rules, runs, defaults."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from re import _parser  # type: ignore[attr-defined]
from typing import Any

import factories
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from netkeeper import migrations
from netkeeper.cli import app as cli
from netkeeper.crm import tags as svc
from netkeeper.crm.tags import (
    DEFAULT_FIELDS,
    DEFAULT_PATTERNS,
    DEFAULTS_SEEDED_KEY,
    ContactNotFound,
    DuplicateTag,
    InvalidPattern,
    InvalidTagValue,
    RuleNotFound,
    RuleRun,
    TagNotFound,
    TagSuppressed,
    compile_pattern,
    contact_counts,
    create_rule,
    create_tag,
    delete_rule,
    delete_tag,
    ensure_default_rules,
    find_tag,
    has_nested_unbounded_repeat,
    list_rules,
    list_tags,
    preview_matches,
    preview_rule,
    reorder_rules,
    run_rule,
    run_rules,
    tag_contact,
    untag_contact,
    update_rule,
    update_tag,
)
from netkeeper.db import database_url, make_engine, make_session_factory, session_scope
from netkeeper.models import (
    Contact,
    ContactTag,
    ContactTagSuppression,
    RuleField,
    Tag,
    TagKind,
    TagSource,
    User,
    UserKind,
)
from netkeeper.scoping import scoped, unscoped
from netkeeper.services.settings_kv import get_setting
from netkeeper.services.users import ensure_local_user

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DEFAULT_TAG_NAMES = [name for name, _ in DEFAULT_PATTERNS]

# The "done when" fixture (issue #16): a title and the default tags it gets. Every
# default tag appears at least once, and the last rows are the near misses.
TITLE_MATRIX: list[tuple[str, set[str]]] = [
    ("Chief Executive Officer", {"c-suite"}),
    ("CEO & Co-Founder", {"c-suite", "founder"}),
    ("CTO", {"c-suite"}),
    ("Chief People Officer", {"c-suite"}),
    ("Fractional CFO", {"c-suite", "consultant"}),
    ("VP of Engineering", {"vp", "engineering"}),
    ("SVP Sales", {"vp", "sales"}),
    ("Vice President, Marketing", {"vp", "marketing"}),
    ("VP, Product", {"vp", "product"}),
    ("Director of Product Management", {"director", "product"}),
    ("Managing Director", {"director"}),
    ("Founder", {"founder"}),
    ("Founding Engineer", {"founder", "engineering"}),
    ("Angel Investor", {"investor"}),
    ("General Partner", {"investor"}),
    ("Technical Recruiter", {"recruiter"}),
    ("Talent Acquisition Lead", {"recruiter"}),
    ("Senior Software Engineer", {"engineering"}),
    ("Staff Developer", {"engineering"}),
    ("Product Manager", {"product"}),
    ("Head of Product", {"product"}),
    ("Senior Product Designer", {"design"}),
    ("UX Researcher", {"design"}),
    ("Account Executive", {"sales"}),
    ("Customer Success Manager", {"sales"}),
    ("Growth Marketing Manager", {"marketing"}),
    ("Management Consultant", {"consultant"}),
    ("Associate Professor of Computer Science", {"academic"}),
    ("Research Scientist", {"academic"}),
    ("PhD Candidate", {"academic"}),
    ("Chief of Staff", set()),
    ("Barista", set()),
]


@pytest.fixture
def writer(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A writer session, as every caller of the service must use: it reads, then writes."""
    with session_scope(session_factory, write=True) as session:
        yield session


@pytest.fixture
def user(writer: Session) -> User:
    return factories.make_user(writer)


@pytest.fixture
def other(writer: Session) -> User:
    return factories.make_user(writer, kind=UserKind.HOSTED)


def names_on(session: Session, user: User, contact: Contact) -> set[str]:
    """The tag names a contact carries, whatever their source."""
    statement = (
        scoped(user, Tag)
        .with_only_columns(Tag.name)
        .join(ContactTag, ContactTag.tag_id == Tag.id)
        .where(ContactTag.contact_id == contact.id)
    )
    return set(session.scalars(statement))


def assignment(session: Session, user: User, contact: Contact, tag: Tag) -> ContactTag | None:
    return session.scalars(
        scoped(user, ContactTag).where(
            ContactTag.contact_id == contact.id, ContactTag.tag_id == tag.id
        )
    ).one_or_none()


def suppressed(session: Session, user: User, contact: Contact, tag: Tag) -> bool:
    row = session.scalars(
        scoped(user, ContactTagSuppression).where(
            ContactTagSuppression.contact_id == contact.id,
            ContactTagSuppression.tag_id == tag.id,
        )
    ).one_or_none()
    return row is not None


def contact_with_title(
    session: Session, user: User, title: str | None, **overrides: Any
) -> Contact:
    return factories.make_contact(
        session, user, current_title=title, headline=None, current_company=None, **overrides
    )


# --- tags -------------------------------------------------------------------


def test_create_tag_strips_the_name_and_is_unique_without_regard_to_case(
    writer: Session, user: User, other: User
) -> None:
    tag = create_tag(writer, user, "  VP ", color="#ABCDEF")
    assert (tag.name, tag.name_key, tag.color, tag.kind) == ("VP", "vp", "#abcdef", TagKind.MANUAL)
    with pytest.raises(DuplicateTag):
        create_tag(writer, user, "vp")
    assert create_tag(writer, other, "vp").name == "vp"  # another user's namespace
    assert find_tag(writer, user, "Vp") is tag
    assert find_tag(writer, user, "founder") is None


@pytest.mark.parametrize(
    ("name", "color"), [("", None), ("   ", None), ("x" * 101, None), ("ok", "red"), ("ok", "#abc")]
)
def test_create_tag_rejects_a_bad_name_or_color(
    writer: Session, user: User, name: str, color: str | None
) -> None:
    with pytest.raises(InvalidTagValue):
        create_tag(writer, user, name, color=color)


def test_update_tag_renames_recolors_and_clears(writer: Session, user: User) -> None:
    vp = create_tag(writer, user, "vp", color="#112233")
    create_tag(writer, user, "founder")
    with pytest.raises(DuplicateTag):
        update_tag(writer, user, vp.id, name="Founder")
    assert update_tag(writer, user, vp.id, name="VP").name_key == "vp"  # its own name, recased
    assert update_tag(writer, user, vp.id, name="vice-president").color == "#112233"
    assert update_tag(writer, user, vp.id, color="#445566").color == "#445566"
    assert update_tag(writer, user, vp.id, color=None).color is None
    assert update_tag(writer, user, vp.id, name="vp").name == "vp"
    with pytest.raises(TagNotFound):
        update_tag(writer, user, vp.id + 100, name="x")


def test_delete_tag_takes_its_assignments_suppressions_and_rules_with_it(
    writer: Session, user: User
) -> None:
    vp, founder = create_tag(writer, user, "vp"), create_tag(writer, user, "founder")
    contact = contact_with_title(writer, user, "VP")
    create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    create_rule(writer, user, founder.id, RuleField.TITLE, "founder")
    tag_contact(writer, user, contact.id, vp.id, source=TagSource.RULE)
    tag_contact(writer, user, contact.id, founder.id)
    untag_contact(writer, user, contact.id, vp.id)  # writes a suppression
    delete_tag(writer, user, vp.id)
    writer.expire_all()
    assert [t.tag.name for t in list_tags(writer, user)] == ["founder"]
    assert [r.tag_id for r in list_rules(writer, user)] == [founder.id]
    assert list(writer.scalars(unscoped(select(ContactTagSuppression)))) == []
    assert names_on(writer, user, contact) == {"founder"}
    with pytest.raises(TagNotFound):
        delete_tag(writer, user, vp.id)


def test_list_tags_counts_live_contacts_only_and_sorts_by_name(
    writer: Session, user: User, other: User
) -> None:
    vp = create_tag(writer, user, "VP")
    alpha = create_tag(writer, user, "alpha")
    live = factories.make_contact(writer, user)
    archived = factories.make_contact(writer, user, archived_at=NOW)
    survivor = factories.make_contact(writer, user)
    merged = factories.make_contact(writer, user, merged_into_id=survivor.id)
    for contact in (live, archived, merged):
        tag_contact(writer, user, contact.id, vp.id)
    theirs = create_tag(writer, other, "vp")
    tag_contact(writer, other, factories.make_contact(writer, other).id, theirs.id)
    rows = list_tags(writer, user)
    assert [(row.tag.name, row.contact_count) for row in rows] == [("alpha", 0), ("VP", 1)]
    assert contact_counts(writer, user, [vp.id, alpha.id]) == {vp.id: 1}
    assert contact_counts(writer, user, []) == {}
    assert [(row.tag.name, row.contact_count) for row in list_tags(writer, other)] == [("vp", 1)]


def test_writes_need_a_writer_session(session: Session) -> None:
    user = factories.make_user(session)
    with pytest.raises(RuntimeError, match="writer session"):
        create_tag(session, user, "vp")
    with pytest.raises(RuntimeError, match="writer session"):
        run_rules(session, user)
    with pytest.raises(RuntimeError, match="writer session"):
        ensure_default_rules(session, user)


# --- assignments ------------------------------------------------------------


def test_tag_contact_is_idempotent_and_a_manual_tag_takes_over_an_automatic_one(
    writer: Session, user: User
) -> None:
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    contact = contact_with_title(writer, user, "VP Sales")
    run_rules(writer, user)
    auto = assignment(writer, user, contact, vp)
    assert auto is not None and (auto.source, auto.rule_id) == (TagSource.RULE, rule.id)
    manual = tag_contact(writer, user, contact.id, vp.id)
    assert manual is auto and (manual.source, manual.rule_id) == (TagSource.MANUAL, None)
    assert tag_contact(writer, user, contact.id, vp.id) is manual
    # An automatic source leaves a manual assignment alone.
    assert tag_contact(writer, user, contact.id, vp.id, source=TagSource.RULE) is manual
    assert manual.source is TagSource.MANUAL
    assert len(writer.scalars(scoped(user, ContactTag)).all()) == 1
    writer.expire_all()
    assert [t.name for t in contact.tags] == ["vp"]  # the read-only view through the rows
    assert [a.tag_id for a in contact.tag_assignments] == [vp.id]


def test_untagging_an_automatic_tag_suppresses_it_and_untagging_a_manual_one_does_not(
    writer: Session, user: User
) -> None:
    vp = create_tag(writer, user, "vp")
    llm = create_tag(writer, user, "llm-pick")
    contact = contact_with_title(writer, user, "VP")
    tag_contact(writer, user, contact.id, vp.id, source=TagSource.RULE)
    tag_contact(writer, user, contact.id, llm.id, source=TagSource.LLM)
    assert untag_contact(writer, user, contact.id, vp.id)
    assert untag_contact(writer, user, contact.id, llm.id)
    assert not untag_contact(writer, user, contact.id, vp.id)  # nothing to remove now
    assert suppressed(writer, user, contact, vp) and suppressed(writer, user, contact, llm)
    with pytest.raises(TagSuppressed):
        tag_contact(writer, user, contact.id, vp.id, source=TagSource.RULE)
    with pytest.raises(TagSuppressed):
        tag_contact(writer, user, contact.id, llm.id, source=TagSource.LLM)
    # A manual tag is the user's word: it clears the suppression, and removing it
    # again leaves none behind.
    tag_contact(writer, user, contact.id, vp.id)
    assert not suppressed(writer, user, contact, vp)
    assert untag_contact(writer, user, contact.id, vp.id)
    assert not suppressed(writer, user, contact, vp)
    assert names_on(writer, user, contact) == set()


def test_tag_contact_checks_that_the_contact_and_the_tag_are_the_users(
    writer: Session, user: User, other: User
) -> None:
    mine = create_tag(writer, user, "vp")
    theirs = create_tag(writer, other, "vp")
    my_contact = factories.make_contact(writer, user)
    their_contact = factories.make_contact(writer, other)
    with pytest.raises(ContactNotFound):
        tag_contact(writer, user, their_contact.id, mine.id)
    with pytest.raises(TagNotFound):
        tag_contact(writer, user, my_contact.id, theirs.id)
    assert not untag_contact(writer, user, their_contact.id, theirs.id)


# --- rules ------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["", "   ", "(", "[a-", "x" * 501])
def test_a_rule_pattern_must_compile(writer: Session, user: User, pattern: str) -> None:
    vp = create_tag(writer, user, "vp")
    with pytest.raises(InvalidPattern):
        compile_pattern(pattern)
    with pytest.raises(InvalidPattern):
        create_rule(writer, user, vp.id, RuleField.TITLE, pattern)
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, "ok")
    with pytest.raises(InvalidPattern):
        update_rule(writer, user, rule.id, pattern=pattern)
    assert rule.pattern == "ok"


def test_compile_pattern_ignores_case() -> None:
    assert compile_pattern(r"\bvp\b").search("Senior VP, Sales") is not None


# --- the ReDoS guard --------------------------------------------------------

NESTED_UNBOUNDED = [
    r"(a+)+$",
    r"(a*)*",
    r"(a+)*",
    r"(a*)+",
    r"(a{2,})+",
    r"(?:a+)+",
    r"((a)+)+",
    r"(a+|b)+",
    r"(\w+\s?)+$",
    r"x(?=(y+)+)",
    r"(?>a+)+",
    r"(?P<g>a)(?(g)b+|c)+",
]
NOT_NESTED = [
    r"(a+)",
    r"a+b+",
    r"(a+){1,3}",
    r"(ab)+",
    r"(a?)+",
    r"(a+)?",
    r"(a|b)+",
    r"\b(engineer(s|ing)?|developer)\b",
    r"Chief\s+(\w+[\s-]+){1,2}Officer",
    r"(?:x|y+)",
    r"x(?=y+)",
]


@pytest.mark.parametrize("pattern", NESTED_UNBOUNDED)
def test_a_nested_unbounded_repeat_is_rejected_at_save_and_preview(
    writer: Session, user: User, pattern: str
) -> None:
    assert has_nested_unbounded_repeat(_parser.parse(pattern, re.IGNORECASE))
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, "ok")
    for attempt in (
        lambda: compile_pattern(pattern),
        lambda: create_rule(writer, user, vp.id, RuleField.TITLE, pattern),
        lambda: update_rule(writer, user, rule.id, pattern=pattern),
        lambda: preview_rule(writer, user, RuleField.TITLE, pattern),
    ):
        with pytest.raises(InvalidPattern, match="may run slowly"):
            attempt()
    assert rule.pattern == "ok"


@pytest.mark.parametrize("pattern", NOT_NESTED)
def test_a_pattern_without_nesting_passes_the_static_check(pattern: str) -> None:
    assert not has_nested_unbounded_repeat(_parser.parse(pattern, re.IGNORECASE))
    compile_pattern(pattern)


def test_a_search_that_times_out_is_no_match_and_is_counted(
    writer: Session, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """``(a|aa)+$`` has no nested repeat, so it saves, but it backtracks exponentially."""
    slow = r"(a|aa)+$"
    assert not has_nested_unbounded_repeat(_parser.parse(slow, re.IGNORECASE))
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, slow)
    quick = contact_with_title(writer, user, "aa")
    stuck = contact_with_title(writer, user, "a" * 40 + "b")
    miss = contact_with_title(writer, user, "nothing")
    with caplog.at_level("WARNING", logger="netkeeper.crm.tags"):
        preview = preview_matches(writer, user, RuleField.TITLE, slow)
        result = run_rules(writer, user)
    assert preview == svc.RulePreview(count=1, contact_ids=(quick.id,), timeouts=1)
    assert result == RuleRun(contacts=3, added=1, removed=0, updated=0, timeouts=1)
    assert names_on(writer, user, quick) == {"vp"}
    assert names_on(writer, user, stuck) == set()
    assert names_on(writer, user, miss) == set()
    messages = [record.getMessage() for record in caplog.records]
    assert f"auto-tag rule {rule.id} timed out after 50 ms on contact {stuck.id}" in messages[-1]
    assert f"pattern preview timed out after 50 ms on contact {stuck.id}" in messages[0]
    # Idempotent: the timeout repeats, nothing else changes.
    assert run_rules(writer, user) == RuleRun(contacts=3, added=0, removed=0, updated=0, timeouts=1)


def test_the_timeout_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    compiled = compile_pattern(r"(a|aa)+$")
    assert svc.search(compiled, "aa") is True
    assert svc.search(compiled, "a" * 40 + "b") is None  # 50 ms is not enough
    monkeypatch.setattr(svc, "MATCH_TIMEOUT_S", 30.0)
    assert svc.search(compiled, "a" * 18 + "b") is False  # given time, it finishes: no match


def test_create_update_delete_and_reorder_rules(writer: Session, user: User, other: User) -> None:
    vp, founder = create_tag(writer, user, "vp"), create_tag(writer, user, "founder")
    a = create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    b = create_rule(writer, user, vp.id, RuleField.HEADLINE, "vp", enabled=False)
    c = create_rule(writer, user, founder.id, RuleField.COMPANY, "founder")
    theirs = create_rule(writer, other, create_tag(writer, other, "x").id, RuleField.TITLE, "x")
    assert [(r.id, r.position) for r in list_rules(writer, user)] == [
        (a.id, 0),
        (b.id, 1),
        (c.id, 2),
    ]
    with pytest.raises(TagNotFound):
        create_rule(writer, user, create_tag(writer, other, "y").id, RuleField.TITLE, "y")

    updated = update_rule(
        writer, user, b.id, tag_id=founder.id, field=RuleField.TITLE, enabled=True
    )
    assert (updated.tag_id, updated.field, updated.enabled, updated.pattern) == (
        founder.id,
        RuleField.TITLE,
        True,
        "vp",
    )
    with pytest.raises(RuleNotFound):
        update_rule(writer, user, theirs.id, enabled=False)

    assert [r.id for r in reorder_rules(writer, user, [c.id])] == [c.id, a.id, b.id]
    assert [(r.id, r.position) for r in list_rules(writer, user)] == [
        (c.id, 0),
        (a.id, 1),
        (b.id, 2),
    ]
    with pytest.raises(RuleNotFound):
        reorder_rules(writer, user, [theirs.id])
    with pytest.raises(InvalidTagValue):
        reorder_rules(writer, user, [a.id, a.id])

    delete_rule(writer, user, a.id)
    assert [r.id for r in list_rules(writer, user)] == [c.id, b.id]
    with pytest.raises(RuleNotFound):
        delete_rule(writer, user, theirs.id)
    assert [r.id for r in list_rules(writer, other)] == [theirs.id]


def test_deleting_a_rule_keeps_its_assignments_until_the_next_run(
    writer: Session, user: User
) -> None:
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    contact = contact_with_title(writer, user, "VP")
    run_rules(writer, user)
    delete_rule(writer, user, rule.id)
    writer.expire_all()
    row = assignment(writer, user, contact, vp)
    assert row is not None and (row.source, row.rule_id) == (TagSource.RULE, None)
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=1, updated=0)
    assert names_on(writer, user, contact) == set()


# --- running ----------------------------------------------------------------


def test_default_rules_tag_the_fixture_titles_as_expected(writer: Session, user: User) -> None:
    ensure_default_rules(writer, user)
    contacts = {title: contact_with_title(writer, user, title) for title, _ in TITLE_MATRIX}
    result = run_rules(writer, user)
    assert result.contacts == len(TITLE_MATRIX)
    assert result.added == sum(len(expected) for _, expected in TITLE_MATRIX)
    got = {title: names_on(writer, user, contact) for title, contact in contacts.items()}
    assert got == dict(TITLE_MATRIX)
    assert {name for expected in got.values() for name in expected} == set(DEFAULT_TAG_NAMES)


def test_headline_and_company_rules_read_their_own_fields(writer: Session, user: User) -> None:
    ensure_default_rules(writer, user)
    synced = factories.make_contact(
        writer, user, current_title=None, headline="Founder & CEO at Acme", current_company=None
    )
    acme = create_tag(writer, user, "acme")
    create_rule(writer, user, acme.id, RuleField.COMPANY, r"^acme\b")
    at_acme = factories.make_contact(
        writer, user, current_title="Barista", headline=None, current_company="Acme Inc"
    )
    run_rules(writer, user)
    assert names_on(writer, user, synced) == {"founder", "c-suite"}
    assert names_on(writer, user, at_acme) == {"acme"}


def test_a_run_credits_the_first_matching_rule_and_recredits_when_that_rule_goes(
    writer: Session, user: User
) -> None:
    vp = create_tag(writer, user, "vp")
    first = create_rule(writer, user, vp.id, RuleField.TITLE, r"\bvp\b")
    second = create_rule(writer, user, vp.id, RuleField.TITLE, r"vice president")
    contact = contact_with_title(writer, user, "VP and Vice President")
    assert run_rules(writer, user) == RuleRun(contacts=1, added=1, removed=0, updated=0)
    row = assignment(writer, user, contact, vp)
    assert row is not None and row.rule_id == first.id
    update_rule(writer, user, first.id, enabled=False)
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=1)
    assert row.rule_id == second.id
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0)
    reorder_rules(writer, user, [second.id])
    update_rule(writer, user, first.id, enabled=True)
    assert run_rules(writer, user).updated == 0  # second still matches; no churn by position


def test_a_run_removes_what_no_enabled_rule_matches_and_never_touches_manual_or_llm(
    writer: Session, user: User
) -> None:
    vp, hand, llm = (create_tag(writer, user, n) for n in ("vp", "hand", "llm-pick"))
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, r"\bvp\b")
    contact = contact_with_title(writer, user, "VP Sales")
    tag_contact(writer, user, contact.id, hand.id)
    tag_contact(writer, user, contact.id, llm.id, source=TagSource.LLM)
    run_rules(writer, user)
    assert names_on(writer, user, contact) == {"vp", "hand", "llm-pick"}
    contact.current_title = "Barista"
    writer.flush()
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=1, updated=0)
    assert names_on(writer, user, contact) == {"hand", "llm-pick"}
    # A disabled rule matches nothing, so its assignments go too.
    contact.current_title = "VP Sales"
    writer.flush()
    run_rules(writer, user)
    update_rule(writer, user, rule.id, enabled=False)
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=1, updated=0)
    # A manual assignment of a rule's tag survives the rule not matching.
    tag_contact(writer, user, contact.id, vp.id)
    update_rule(writer, user, rule.id, enabled=True, pattern="nothing-matches")
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"vp", "hand", "llm-pick"}


def test_a_removed_auto_tag_is_suppressed_on_the_next_run(writer: Session, user: User) -> None:
    ensure_default_rules(writer, user)
    contact = contact_with_title(writer, user, "VP of Engineering")
    run_rules(writer, user)
    vp = find_tag(writer, user, "vp")
    assert vp is not None and names_on(writer, user, contact) == {"vp", "engineering"}
    untag_contact(writer, user, contact.id, vp.id)
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"engineering"}
    assert run_rule(writer, user, list_rules(writer, user)[0].id).added == 0
    # Putting it back by hand makes it manual, and the run leaves it alone.
    tag_contact(writer, user, contact.id, vp.id)
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"vp", "engineering"}


def test_a_run_covers_live_contacts_only_and_can_be_limited_to_ids(
    writer: Session, user: User, other: User
) -> None:
    vp = create_tag(writer, user, "vp")
    create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    live = contact_with_title(writer, user, "VP")
    archived = contact_with_title(writer, user, "VP", archived_at=NOW)
    merged = contact_with_title(writer, user, "VP", merged_into_id=live.id)
    later = contact_with_title(writer, user, "VP")
    theirs = contact_with_title(writer, other, "VP")
    create_rule(writer, other, create_tag(writer, other, "vp").id, RuleField.TITLE, "vp")
    assert run_rules(writer, user, [live.id, archived.id, merged.id, theirs.id]) == RuleRun(
        contacts=1, added=1, removed=0, updated=0
    )
    assert names_on(writer, user, live) == {"vp"}
    for untouched in (archived, merged, later):
        assert names_on(writer, user, untouched) == set()
    assert names_on(writer, other, theirs) == set()
    assert run_rules(writer, user, []) == RuleRun(contacts=0, added=0, removed=0, updated=0)
    assert run_rules(writer, user) == RuleRun(contacts=2, added=1, removed=0, updated=0)
    assert names_on(writer, user, later) == {"vp"}


def test_run_rule_reconciles_only_the_tag_the_rule_feeds(writer: Session, user: User) -> None:
    vp, founder = create_tag(writer, user, "vp"), create_tag(writer, user, "founder")
    vp_rule = create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    founder_rule = create_rule(writer, user, founder.id, RuleField.TITLE, "founder")
    contact = contact_with_title(writer, user, "VP & Founder")
    assert run_rule(writer, user, vp_rule.id) == RuleRun(contacts=1, added=1, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"vp"}
    contact.current_title = "Barista"
    writer.flush()
    run_rules(writer, user)  # both tags reconciled from here on
    contact.current_title = "VP & Founder"
    writer.flush()
    run_rules(writer, user)
    assert names_on(writer, user, contact) == {"vp", "founder"}
    contact.current_title = "Nobody"
    writer.flush()
    assert run_rule(writer, user, founder_rule.id) == RuleRun(
        contacts=1, added=0, removed=1, updated=0
    )
    assert names_on(writer, user, contact) == {"vp"}  # vp's turn has not come
    update_rule(writer, user, vp_rule.id, enabled=False)
    assert run_rule(writer, user, vp_rule.id).removed == 1  # a disabled rule justifies nothing
    with pytest.raises(RuleNotFound):
        run_rule(writer, user, vp_rule.id + 100)


def test_a_run_works_in_batches(
    writer: Session, user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc, "BATCH_SIZE", 2)
    vp = create_tag(writer, user, "vp")
    create_rule(writer, user, vp.id, RuleField.TITLE, "vp")
    contacts = [contact_with_title(writer, user, f"VP {n}") for n in range(5)]
    contact_with_title(writer, user, "Nobody")
    assert run_rules(writer, user) == RuleRun(contacts=6, added=5, removed=0, updated=0)
    assert all(names_on(writer, user, c) == {"vp"} for c in contacts)
    assert run_rules(writer, user) == RuleRun(contacts=6, added=0, removed=0, updated=0)


def test_preview_counts_and_samples_without_writing(writer: Session, user: User) -> None:
    contacts = [contact_with_title(writer, user, f"VP {n}") for n in range(12)]
    contact_with_title(writer, user, "VP", archived_at=NOW)
    contact_with_title(writer, user, "Barista")
    preview = preview_matches(writer, user, RuleField.TITLE, r"\bvp\b")
    assert preview.count == 12
    assert preview.contact_ids == tuple(c.id for c in contacts[:10])
    assert preview_rule(writer, user, RuleField.TITLE, r"\bvp\b") == 12
    assert preview_rule(writer, user, RuleField.HEADLINE, r"\bvp\b") == 0
    assert preview_rule(writer, user, RuleField.TITLE, "nothing") == 0
    with pytest.raises(InvalidPattern):
        preview_rule(writer, user, RuleField.TITLE, "(")
    assert writer.scalars(scoped(user, ContactTag)).all() == []
    assert writer.scalars(scoped(user, Tag)).all() == []


# --- defaults ---------------------------------------------------------------


def test_default_patterns_are_the_spec_list_and_compile() -> None:
    assert DEFAULT_TAG_NAMES == [
        "c-suite",
        "vp",
        "director",
        "founder",
        "investor",
        "recruiter",
        "engineering",
        "product",
        "design",
        "sales",
        "marketing",
        "consultant",
        "academic",
    ]
    assert DEFAULT_FIELDS == (RuleField.TITLE, RuleField.HEADLINE)
    for _, pattern in DEFAULT_PATTERNS:
        compile_pattern(pattern)
        assert not has_nested_unbounded_repeat(_parser.parse(pattern, re.IGNORECASE)), pattern


def test_ensure_default_rules_seeds_once_and_never_recreates_a_deleted_default(
    writer: Session, user: User, other: User
) -> None:
    created = ensure_default_rules(writer, user)
    assert len(created) == len(DEFAULT_PATTERNS) * len(DEFAULT_FIELDS)
    tags = list_tags(writer, user)
    assert [row.tag.name for row in tags] == sorted(DEFAULT_TAG_NAMES)
    assert all(row.tag.kind is TagKind.AUTO for row in tags)
    rules = list_rules(writer, user)
    assert [(r.tag.name, r.field) for r in rules[:4]] == [
        ("c-suite", RuleField.TITLE),
        ("c-suite", RuleField.HEADLINE),
        ("vp", RuleField.TITLE),
        ("vp", RuleField.HEADLINE),
    ]
    assert [r.position for r in rules] == list(range(len(rules)))
    assert get_setting(writer, user, DEFAULTS_SEEDED_KEY) is True
    assert ensure_default_rules(writer, user) == []
    vp = find_tag(writer, user, "vp")
    assert vp is not None
    delete_tag(writer, user, vp.id)
    assert ensure_default_rules(writer, user) == []
    assert find_tag(writer, user, "vp") is None
    assert len(list_rules(writer, user)) == len(created) - len(DEFAULT_FIELDS)
    # Per user: the other user is seeded on their own first use.
    assert list_tags(writer, other) == []
    assert len(ensure_default_rules(writer, other)) == len(created)


def test_ensure_default_rules_reuses_a_tag_the_user_already_has(
    writer: Session, user: User
) -> None:
    mine = create_tag(writer, user, "VP", color="#123456")
    ensure_default_rules(writer, user)
    tags = list_tags(writer, user)
    assert len(tags) == len(DEFAULT_PATTERNS)
    reused = find_tag(writer, user, "vp")
    assert reused is mine and (mine.name, mine.kind, mine.color) == (
        "VP",
        TagKind.MANUAL,
        "#123456",
    )
    assert len([r for r in list_rules(writer, user) if r.tag_id == mine.id]) == len(DEFAULT_FIELDS)


# --- CLI --------------------------------------------------------------------


def test_cli_tags_list_and_run_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = database_url(tmp_path)
    monkeypatch.setenv("NETKEEPER_DATABASE_URL", url)
    runner = CliRunner()

    engine = make_engine(url)
    migrations.upgrade(engine)
    result = runner.invoke(cli, ["tags", "list"])
    assert result.exit_code == 1 and "netkeeper db upgrade" in result.stderr

    with session_scope(make_session_factory(engine), write=True) as session:
        user = ensure_local_user(session)
        factories.make_contact(
            session, user, current_title="VP of Engineering", headline=None, current_company=None
        )
    engine.dispose()

    result = runner.invoke(cli, ["tags", "list"])
    assert result.exit_code == 0 and result.stdout == "no tags\n"

    result = runner.invoke(cli, ["tags", "run-rules"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.splitlines() == [
        f"seeded {len(DEFAULT_PATTERNS) * len(DEFAULT_FIELDS)} default rules",
        "1 contacts: 2 tags added, 0 removed, 0 re-credited",
    ]

    result = runner.invoke(cli, ["tags", "run-rules"])
    assert result.exit_code == 0
    assert result.stdout == "1 contacts: 0 tags added, 0 removed, 0 re-credited\n"

    result = runner.invoke(cli, ["tags", "list"])
    assert result.exit_code == 0, result.stdout
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["NAME", "KIND", "COLOR", "CONTACTS"]
    rows = {line.split()[0]: line.split()[1:] for line in lines[1:]}
    assert set(rows) == set(DEFAULT_TAG_NAMES)
    assert rows["vp"] == ["auto", "-", "1"]
    assert rows["engineering"] == ["auto", "-", "1"]
    assert rows["sales"] == ["auto", "-", "0"]
