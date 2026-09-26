"""netkeeper.crm.tags (spec 8.3, 10.3): tags, assignments, suppressions, rules, runs, defaults."""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from re import _parser  # type: ignore[attr-defined]
from typing import Any, cast

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
    DEFAULTS,
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
    has_ambiguous_nested_repeat,
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
    TagMetSignal,
    TagSource,
    User,
    UserKind,
)
from netkeeper.scoping import scoped, unscoped
from netkeeper.services.settings_kv import delete_setting, get_setting, set_setting
from netkeeper.services.users import ensure_local_user

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DEFAULT_TAG_NAMES = [default.name for default in DEFAULTS]
RULES_PER_SEED = sum(len(default.fields) for default in DEFAULTS)


@contextmanager
def match_budget(seconds: float) -> Iterator[None]:
    """Run the block with a match budget long enough that no honest search times out."""
    original = svc.MATCH_TIMEOUT_S
    svc.MATCH_TIMEOUT_S = seconds  # type: ignore[misc]
    try:
        yield
    finally:
        svc.MATCH_TIMEOUT_S = original  # type: ignore[misc]


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
    # The shapes a real address book actually holds, including the one that
    # reads retired and is not: somebody who works in retirement planning.
    ("Retired", {"retired"}),
    # "Product Developer" is not one of the product pattern's shapes; the
    # developer half is what the engineering rule reads.
    ("Retired Technologist and Product Developer", {"retired", "engineering"}),
    ("Telecom Solution Architect - Retired", {"retired"}),
    ("Retirement Plan Consultant", {"consultant"}),
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


def test_a_tag_carries_the_meaning_the_user_gives_it(writer: Session, user: User) -> None:
    """``met_signal`` is what puts a tag's triage batch on offer (spec 10.2)."""
    plain = create_tag(writer, user, "vp")
    assert plain.met_signal is None
    recruiters = create_tag(writer, user, "recruiter", met_signal=TagMetSignal.NOT_MET)
    assert recruiters.met_signal is TagMetSignal.NOT_MET
    assert update_tag(writer, user, plain.id, met_signal=TagMetSignal.MET).met_signal is (
        TagMetSignal.MET
    )
    assert update_tag(writer, user, plain.id, name="VP").met_signal is TagMetSignal.MET, (
        "a field left out is left alone"
    )
    assert update_tag(writer, user, plain.id, met_signal=None).met_signal is None


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


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "   ",
        "(",
        "[a-",
        "x" * 501,
        # The parser raises OverflowError, not re.error, for a count at or above MAXREPEAT.
        "a{1,4294967296}",
        "a{4294967295}",
    ],
)
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
    # From #63's list of "harmless" patterns, and it is not: the optional \s*
    # lets the next [\w&-] either extend the inner repeat or start another pass,
    # so "VP of " and a long word with no end anchor splits 2^n ways. The regex
    # package happens to cut that search short; the standard library's re takes
    # a second at thirty-one characters, and the guard does not bet on a heuristic.
    r"\b(VP|Head)\s+of\s+([\w&-]+\s*)+$",
    # Case folding: [a-z] and [A-Z] are the same set once case is ignored.
    r"([a-z]+[A-Z])+",
    # What a backreference or a lookahead's contents match is not analyzed,
    # so it counts as any character, and doubt rejects.
    r"(?:(\w)+\1)+",
    r"(?:(?=(\w+\s)+)x)+",
    # #224: a count that can vary is a choice too, one copy at a time. Each of
    # these takes from a tenth of a second to minutes under the standard
    # library's re on 30-40 characters of a near miss.
    r"(a{1,2})+$",
    r"(a{1,3})+$",
    r"(x{2,4})+y",
    r"(a{0,2})+$",
    r"(a{1,2}b?)+$",
    r"(?:a{1,2}|b)+$",
    # An optional one leaves the pass empty only when nothing else in it can
    # match a character; otherwise skipping it is a second way to split.
    r"(aa?)+$",
    r"(a?a)+$",
    r"(a?b?)+$",
    r"(a?b?a?)+$",
    r"(a(a?))+$",
    r"((a?){1,3})+$",
    # #233: alternation branches that overlap are a choice per pass, like a
    # variable count. These hang the standard library's re on a near miss of
    # about thirty characters. The parser factors out a shared prefix first, so
    # (a|aa) arrives as a(?:|a) and (a|a) as a(?:|): an empty branch next to
    # one whose first character could also come after the alternation.
    r"(a?|a)+$",
    r"(a?|aa)+$",
    r"(a|a)+$",
    r"(a|aa)+$",
    r"((a|)a)+$",
    r"((a?|b?)c)+$",
    r"(\w+\s+|and\s+)+$",
    # One branch a prefix of another: "ab" is one pass or two. Refused
    # without proof that the rest of the pattern can split a string that way.
    r"(ab|cd|a)+$",
    r"(ab|a|b)+$",
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
UNAMBIGUOUS_NESTED = [
    # #63: nested, but nothing after the inner repeat can extend it, so the
    # inner repeat always runs to the end of its run and adds no choice.
    r"\b(senior|lead)\s+(\w+\s+)*engineer\b",
    r"(?:[A-Z]\w+\s+)+Engineer",
    r"^(\w+\W+){2,}Officer$",
    r"(a+b)+",
    r"(\d+,)*\d+$",
    # #224: a variable count nothing after it can continue, a fixed count, and
    # an optional one alone in its pass (an empty pass ends the repeat).
    r"(x(a{1,2})y)+$",
    r"(ba?)+$",
    r"(a{2})+$",
    r"(\ba?)+$",
    r"(a?)+$",
    # #233: branches that start differently, and an empty branch alone in its
    # pass (an empty pass ends the repeat, as with (a?)+).
    r"(ab|ac)+$",
    r"(a|b)+$",
    r"(a|)+$",
    r"(a|b|)+$",
    r"(a?|b?)+$",
    r"((x|)z)+$",
    # Two branches start alike but differ by their second character.
    r"(?:(senior|staff|lead)\s+)+engineer",
    r"(?:(vp|vice\s+president|svp)\s+of\s+)+sales",
]


@pytest.mark.parametrize("pattern", NESTED_UNBOUNDED)
def test_a_nested_unbounded_repeat_is_rejected_at_save_and_preview(
    writer: Session, user: User, pattern: str
) -> None:
    assert has_ambiguous_nested_repeat(_parser.parse(pattern, re.IGNORECASE))
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


@pytest.mark.parametrize("pattern", NOT_NESTED + UNAMBIGUOUS_NESTED)
def test_a_pattern_without_nesting_passes_the_static_check(pattern: str) -> None:
    assert not has_ambiguous_nested_repeat(_parser.parse(pattern, re.IGNORECASE))
    compile_pattern(pattern)


NEAR_MISSES = ["senior " + "ab" * 12 + "!", "Ab" * 12 + "!", "a" * 24 + "!", "1" * 24 + "x"]
"""Titles that make an ambiguous nested repeat try every split before failing."""


@pytest.mark.parametrize("pattern", UNAMBIGUOUS_NESTED)
def test_an_accepted_nested_pattern_does_not_backtrack_exponentially(pattern: str) -> None:
    r"""Accepting a nested repeat is a claim that it cannot backtrack exponentially.

    The ``regex`` package cuts many exponential searches short on its own, so a
    search through it would pass for patterns the guard should refuse. The
    standard library's ``re`` has no such shortcut: on these near misses the
    ambiguous twin of each pattern here (``\s*`` for ``\s+``, ``,?`` for ``,``,
    and so on) takes from 50 ms to 40 s, and the patterns themselves about
    10 us, so a 50 ms bound is thousands of times either side of the line.
    """
    compiled = re.compile(pattern, re.IGNORECASE)
    for title in NEAR_MISSES:
        started = time.perf_counter()
        compiled.search(title)
        assert time.perf_counter() - started < 0.05, title


def test_the_patterns_from_63_match_what_they_were_written_for() -> None:
    assert svc.search(compile_pattern(UNAMBIGUOUS_NESTED[0]), "Senior Staff Software Engineer")
    assert svc.search(compile_pattern(UNAMBIGUOUS_NESTED[1]), "Principal Data Engineer")
    assert svc.search(compile_pattern(UNAMBIGUOUS_NESTED[2]), "Chief Revenue Officer")


def test_the_slow_pattern_message_says_how_to_rewrite_it() -> None:
    with pytest.raises(InvalidPattern) as caught:
        compile_pattern(r"(\w+\s*)+$")
    assert r"(\w+\s+)+" in str(caught.value)
    with pytest.raises(InvalidPattern) as caught:
        compile_pattern(r"(a?|aa)+$")
    assert "alternation" in str(caught.value)
    assert "start differently" in str(caught.value)


COMPILE_BOMBS = [
    r"(?:(?:a{100}){100}){100}",
    r"(?:a{40}){40}",
    r"((a{2}){3}){200}",
    r"(a{60}|b){60}",
    r"(?:(?:a{50}){50}){50}",
]
CHEAP_COUNTED = [
    r"(?:a{10}){10}",
    r"Chief\s+(\w+[\s-]+){1,2}Officer",
    r"(?:a{500})",
    r"(\w{2,4}\s){1,3}Manager",
]


@pytest.mark.parametrize("pattern", COMPILE_BOMBS)
def test_nested_counted_repeats_are_refused_before_they_are_compiled(
    writer: Session, user: User, pattern: str
) -> None:
    """``regex`` expands ``a{n}`` at compile time, which no search timeout bounds.

    ``(?:(?:a{300}){300}){300}`` is twenty-four characters and needs about six
    gigabytes, so the cost has to be refused from the parse tree.
    """
    assert svc.expansion(_parser.parse(pattern, re.IGNORECASE)) > svc.MAX_EXPANSION
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, "ok")
    for attempt in (
        lambda: compile_pattern(pattern),
        lambda: create_rule(writer, user, vp.id, RuleField.TITLE, pattern),
        lambda: update_rule(writer, user, rule.id, pattern=pattern),
        lambda: preview_rule(writer, user, RuleField.TITLE, pattern),
    ):
        with pytest.raises(InvalidPattern, match="too much memory"):
            attempt()
    assert rule.pattern == "ok"


@pytest.mark.parametrize("pattern", CHEAP_COUNTED + [p for _, p in DEFAULT_PATTERNS])
def test_a_pattern_that_expands_cheaply_is_accepted(pattern: str) -> None:
    assert svc.expansion(_parser.parse(pattern, re.IGNORECASE)) <= svc.MAX_EXPANSION
    compile_pattern(pattern)


SLOW_PATTERN = r"(a|aa){1,40}$"
"""Saves, because the static check looks inside unbounded repeats only, and forty copies
expand cheaply, but its alternation backtracks exponentially: the run-time timeout is
the guard here. ``(a|aa)+$``, the unbounded form, is refused at save since #233."""

MATCHES_SLOWLY = "a" * 29 + "b" + "aa"
"""``SLOW_PATTERN`` matches this, but only after about two thirds of a second of
backtracking: thirteen times the shipped 50 ms budget, and the cost grows 2.6x per two
characters, so neither half of the comparison is close enough to the boundary to turn
on machine speed."""

NEVER_MATCHES = "a" * 40 + "b"
"""About a minute of backtracking, then no match. Always a timeout, on any machine."""


def test_a_timed_out_search_leaves_an_existing_tag_alone(writer: Session, user: User) -> None:
    """A timeout means "not known". Treating it as "does not match" on the removal side
    would make a tag depend on how loaded the machine is: the same rule and the same
    contact would tag on a fast run and untag on a slow one, and because no suppression
    is written the tag would come back on the next fast run and flap forever."""
    vp = create_tag(writer, user, "vp")
    create_rule(writer, user, vp.id, RuleField.TITLE, SLOW_PATTERN)
    contact = contact_with_title(writer, user, MATCHES_SLOWLY)

    with match_budget(30.0):
        assert run_rules(writer, user) == RuleRun(contacts=1, added=1, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"vp"}

    # The shipped budget cannot finish this search. The tag must survive anyway.
    assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0, timeouts=1)
    assert names_on(writer, user, contact) == {"vp"}

    with match_budget(30.0):
        assert run_rules(writer, user) == RuleRun(contacts=1, added=0, removed=0, updated=0)
    assert names_on(writer, user, contact) == {"vp"}


def test_a_rule_that_keeps_timing_out_is_skipped_for_the_rest_of_the_run(
    writer: Session, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """One evil pattern must not cost the run its corpus size. Without a give-up, 10,000
    contacts at 50 ms a search is hours inside the writer transaction, holding the write
    lock that the guard exists to protect."""
    give_up = svc.MATCH_TIMEOUT_GIVE_UP
    contacts = [contact_with_title(writer, user, NEVER_MATCHES) for _ in range(give_up + 5)]
    vp = create_tag(writer, user, "vp")
    rule = create_rule(writer, user, vp.id, RuleField.TITLE, SLOW_PATTERN)

    with caplog.at_level("WARNING", logger="netkeeper.crm.tags"):
        result = run_rules(writer, user)

    # Only `give_up` searches ran; the last five contacts cost nothing.
    assert result == RuleRun(
        contacts=len(contacts), added=0, removed=0, updated=0, timeouts=give_up
    )
    assert f"auto-tag rule {rule.id} has timed out on {give_up} contacts" in caplog.text
    assert all(names_on(writer, user, contact) == set() for contact in contacts)


def test_a_rule_skipped_after_its_give_up_still_removes_nothing(
    writer: Session, user: User
) -> None:
    """The give-up is a timeout too, so it has to be as non-destructive as one."""
    give_up = svc.MATCH_TIMEOUT_GIVE_UP
    stuck = [contact_with_title(writer, user, NEVER_MATCHES) for _ in range(give_up)]
    tagged = contact_with_title(writer, user, MATCHES_SLOWLY)  # searched last, after the budget
    vp = create_tag(writer, user, "vp")
    create_rule(writer, user, vp.id, RuleField.TITLE, SLOW_PATTERN)

    with match_budget(30.0):
        run_rules(writer, user, [tagged.id])
    assert names_on(writer, user, tagged) == {"vp"}

    result = run_rules(writer, user)
    assert (result.removed, result.timeouts) == (0, give_up)
    assert names_on(writer, user, tagged) == {"vp"}
    assert all(names_on(writer, user, contact) == set() for contact in stuck)


def test_a_search_that_times_out_is_no_match_and_is_counted(
    writer: Session, user: User, caplog: pytest.LogCaptureFixture
) -> None:
    """``SLOW_PATTERN`` passes the static check, but it backtracks exponentially."""
    slow = SLOW_PATTERN
    assert not has_ambiguous_nested_repeat(_parser.parse(slow, re.IGNORECASE))
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
    compiled = compile_pattern(SLOW_PATTERN)
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


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Retired", {"retired"}),
        ("Retired Inc.", {"retired"}),
        ("Retired!", {"retired"}),
        # Standing on its own, wherever in the value it stands.
        ("Google (Retired)", {"retired"}),
        ("Self-employed (Retired)", {"retired"}),
        ("N/A - Retired", {"retired"}),
        ("Currently Retired", {"retired"}),
        ("Formerly Acme Corp, now Retired", {"retired"}),
        ("Semi retired", {"retired"}),
        # Mid-name, the same word says who an organization serves. Everybody on
        # these payrolls is working.
        ("American Association of Retired Persons", set()),
        ("National Association of Retired Federal Employees", set()),
        ("The Retired Enlisted Association", set()),
        # And the industry, which the title pattern already refuses.
        ("Sunrise Retirement Communities", set()),
    ],
    ids=str,
)
def test_the_retired_rule_reads_a_company_as_the_answer_not_the_industry(
    writer: Session, user: User, company: str, expected: set[str]
) -> None:
    """ "Where do you work?" — "Retired." That is the only thing this reads.

    The company pattern is anchored where the title pattern is not, because a
    company name is somebody else's words: in the middle of one, "retired"
    describes the people an organization exists for rather than the person on
    the payroll.
    """
    ensure_default_rules(writer, user)
    contact = factories.make_contact(
        writer, user, current_title="Office Manager", headline=None, current_company=company
    )
    run_rules(writer, user)
    assert names_on(writer, user, contact) == expected


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
        "retired",
    ]
    assert DEFAULT_FIELDS == (RuleField.TITLE, RuleField.HEADLINE)
    for default in DEFAULTS:
        compile_pattern(default.pattern)
        assert not has_ambiguous_nested_repeat(_parser.parse(default.pattern, re.IGNORECASE)), (
            default.pattern
        )
    # Only "retired" reads the company, where the name is the statement rather
    # than the industry somebody happens to work in.
    assert [default.name for default in DEFAULTS if RuleField.COMPANY in default.fields] == [
        "retired"
    ]


def test_ensure_default_rules_seeds_once_and_never_recreates_a_deleted_default(
    writer: Session, user: User, other: User
) -> None:
    created = ensure_default_rules(writer, user)
    assert len(created) == RULES_PER_SEED
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
    assert get_setting(writer, user, DEFAULTS_SEEDED_KEY) == sorted(DEFAULT_TAG_NAMES)
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


def test_a_default_added_later_reaches_a_database_seeded_before_it_existed(
    writer: Session, user: User
) -> None:
    """The record is which defaults were offered, not whether any were.

    It used to be one boolean, so a default added after somebody started using
    netkeeper never reached them: the first import wrote "seeded" and every
    later run read it and stopped. A deleted default still stays deleted --
    its name is in the record either way.
    """
    ensure_default_rules(writer, user)
    retired = find_tag(writer, user, "retired")
    assert retired is not None
    delete_tag(writer, user, retired.id)
    # Wind the record back to what a database seeded before "retired" existed
    # holds: every name but that one.
    set_setting(
        writer,
        user,
        DEFAULTS_SEEDED_KEY,
        sorted(name for name, _ in DEFAULT_PATTERNS),
    )

    created = ensure_default_rules(writer, user)

    assert [rule.field for rule in created] == [
        RuleField.TITLE,
        RuleField.HEADLINE,
        RuleField.COMPANY,
    ]
    assert find_tag(writer, user, "retired") is not None
    # And nothing else was seeded twice.
    assert len(list_tags(writer, user)) == len(DEFAULTS)
    assert ensure_default_rules(writer, user) == []


def test_the_old_seeded_flag_still_means_the_defaults_it_was_written_for(
    writer: Session, user: User
) -> None:
    """`True` is what every database seeded before this shape carries."""
    set_setting(writer, user, DEFAULTS_SEEDED_KEY, True)
    created = ensure_default_rules(writer, user)
    assert [rule.tag.name for rule in created] == ["retired", "retired", "retired"]
    assert ensure_default_rules(writer, user) == []


@pytest.mark.parametrize(
    "corrupt",
    ["yes", 1, ["vp", 7], {"seeded": True}, []],
    ids=["a string", "a number", "a list with a number in it", "an object", "an empty list"],
)
def test_an_unreadable_seeded_record_leaves_the_defaults_alone(
    writer: Session, user: User, corrupt: object
) -> None:
    """The failure direction matters: reseeding duplicates every rule.

    A tag is reused by name, so nothing makes a second "vp". A rule is not, so
    a record read as "seeded nothing" adds another copy of all of them on every
    run, and the rules list grows by twenty-nine each time. Reading an
    unreadable record as "seeded everything" costs a user one missing default
    at worst, which the log names.

    An empty list is in here on purpose: it is the shape an unreadable record
    is most likely to be *repaired* into by hand, and it means the same thing.
    """
    ensure_default_rules(writer, user)
    vp = find_tag(writer, user, "vp")
    assert vp is not None
    delete_tag(writer, user, vp.id)
    before = {(rule.tag_id, rule.field, rule.pattern) for rule in list_rules(writer, user)}
    set_setting(writer, user, DEFAULTS_SEEDED_KEY, cast(Any, corrupt))

    assert ensure_default_rules(writer, user) == []
    assert {(rule.tag_id, rule.field, rule.pattern) for rule in list_rules(writer, user)} == before
    assert len(list_rules(writer, user)) == len(before), "no rule was added a second time"
    assert find_tag(writer, user, "vp") is None, "a default the user deleted came back"


def test_seeding_a_second_time_never_adds_the_same_rule_twice(writer: Session, user: User) -> None:
    """The record is a record, not the only thing standing between here and duplicates.

    Lose it entirely -- a hand-edited database, a restore from before it was
    written -- and every default is "never offered" again. A tag is reused by
    name, so nothing makes a second "vp"; a rule has no such key, so without a
    guard of its own every run would add another copy of all of them.
    """
    ensure_default_rules(writer, user)
    before = {(rule.tag_id, rule.field, rule.pattern) for rule in list_rules(writer, user)}
    delete_setting(writer, user, DEFAULTS_SEEDED_KEY)

    assert ensure_default_rules(writer, user) == []

    rules = list_rules(writer, user)
    assert len(rules) == len(before), "a rule was seeded on top of the one already there"
    assert {(rule.tag_id, rule.field, rule.pattern) for rule in rules} == before
    # And the record is written again, so the next run reads a shape it knows.
    assert get_setting(writer, user, DEFAULTS_SEEDED_KEY) == sorted(DEFAULT_TAG_NAMES)


def test_ensure_default_rules_reuses_a_tag_the_user_already_has(
    writer: Session, user: User
) -> None:
    mine = create_tag(writer, user, "VP", color="#123456")
    ensure_default_rules(writer, user)
    tags = list_tags(writer, user)
    assert len(tags) == len(DEFAULTS)
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
        f"seeded {RULES_PER_SEED} default rules",
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
