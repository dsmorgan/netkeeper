"""Tags and auto-tag rules (spec 8.3 and 10.3): the service behind the API and the CLI.

What this module decides
------------------------
- A tag name is unique per user without regard to case (``Tag.name_key``).
- :func:`tag_contact` with the default ``manual`` source is the user's own act:
  it clears any suppression of that tag on that contact, and it takes over an
  assignment a rule or the LLM made (the row becomes ``manual`` with no rule),
  so no later run can remove it.
- :func:`untag_contact` on a ``rule`` or ``llm`` assignment writes a
  suppression, so nothing automatic re-adds that tag to that contact; on a
  ``manual`` one it does not, so a matching rule may tag the contact again at
  the next run.
- A run (:func:`run_rules`, :func:`run_rule`) reconciles ``rule`` assignments
  with the enabled rules. For each live contact (not merged away, not archived)
  every enabled rule is searched, case-insensitively, in the field it names. A
  matched tag the contact lacks is added as a ``rule`` assignment credited to
  the first matching rule by position, unless the tag is suppressed for that
  contact. A ``rule`` assignment whose tag no enabled rule matches any more is
  removed. ``manual`` and ``llm`` assignments are never touched. A disabled
  rule matches nothing; a deleted rule leaves its assignments (the database
  clears ``rule_id``) for the next run to remove or credit to another rule.
- Patterns are Python regular expressions searched without regard to case,
  validated when a rule is saved (:func:`compile_pattern`), and evaluated in
  Python over the candidate rows in batches, because SQL regular expressions
  differ between SQLite and PostgreSQL.
- A pattern runs inside a writer transaction, which on SQLite holds the write
  lock for everyone, so a pattern that backtracks catastrophically (``(a+)+$``
  takes about a minute on a thirty-character title) is guarded twice. At save
  and preview time the pattern's parse tree is refused when an unbounded
  repeat (``+``, ``*``, ``{n,}``) contains another one, the shape behind
  exponential backtracking, and when its counted repeats multiply out past
  :data:`MAX_EXPANSION`, because ``regex`` expands those at compile time and
  a twenty-four character pattern can ask for gigabytes. At run time every
  search goes through the ``regex`` package with a :data:`MATCH_TIMEOUT_S`
  timeout. A timed-out search means "not known", not "does not match": it
  adds no tag and, unlike a real miss, removes none either, so a slow machine
  cannot strip tags a faster one would keep. It is logged with the rule and
  the contact and counted in the result, and after
  :data:`MATCH_TIMEOUT_GIVE_UP` timeouts a rule is skipped for the rest of
  the run, which bounds what one bad pattern costs the write lock.
- The default rule set (:data:`DEFAULT_PATTERNS`, one ``title`` and one
  ``headline`` rule per tag, because a contact from the connections list has a
  headline and no title until it is enriched) is seeded once per user and
  recorded under the ``settings_kv`` key ``tags.defaults_seeded``. It reuses a
  tag the user already has by that name and is never seeded twice, so a
  default the user deleted stays deleted.

Every function that writes needs a writer session (CLAUDE.md): each reads
first, and on SQLite an unmarked read-then-write can fail with "database is
locked". :func:`run_rules` is the hook for "run on every contact create and
every enrichment" (spec 10.3): whoever creates or enriches contacts calls it
with those contacts' ids.
"""

from __future__ import annotations

import enum
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# ``re.compile``'s own parser (``sre_parse`` is its deprecated alias, and importing
# that warns). Private, but its tree is the honest way to see a nested repeat.
from re import _parser  # type: ignore[attr-defined]
from typing import Any, Final

import regex
from sqlalchemy import ColumnElement, and_, func
from sqlalchemy.orm import Session

from netkeeper.db import is_writer
from netkeeper.models import (
    TAG_NAME_MAX_LENGTH,
    AutotagRule,
    Contact,
    ContactTag,
    ContactTagSuppression,
    RuleField,
    Tag,
    TagKind,
    TagMetSignal,
    TagSource,
    User,
    tag_name_key,
)
from netkeeper.scoping import get_scoped, scoped, scoped_delete
from netkeeper.services.settings_kv import get_setting, set_setting

log = logging.getLogger(__name__)

DEFAULTS_SEEDED_KEY: Final = "tags.defaults_seeded"
"""The ``settings_kv`` key that records that the default rules were seeded for a user."""

PATTERN_MAX_LENGTH: Final = 500
MATCH_TIMEOUT_S: Final = 0.05
"""How long one pattern may spend on one value before it counts as no match."""
MATCH_TIMEOUT_GIVE_UP: Final = 20
"""How many contacts one rule may time out on before the rest of the run skips it."""
MAX_EXPANSION: Final = 1000
"""The largest product of counted repeats a pattern may expand to (see :func:`expansion`)."""
PREVIEW_SAMPLE: Final = 10
"""How many matching contact ids a preview returns alongside the count."""
BATCH_SIZE: Final = 500
"""Contacts per batch when a run loads assignments and suppressions."""

_COLOR: Final = re.compile(r"#[0-9a-fA-F]{6}")

# The default rule set (spec 10.3). Starting points, not a taxonomy: the user
# edits them in the UI, and a headline like "Advisor to CEOs" is a false
# positive the suppression row exists for. Each pattern is searched with
# re.IGNORECASE, so the letter case here is for reading.
DEFAULT_FIELDS: Final[tuple[RuleField, ...]] = (RuleField.TITLE, RuleField.HEADLINE)
"""The fields a default pattern gets a rule for unless it names its own."""


@dataclass(frozen=True, slots=True)
class DefaultRule:
    """One default tag, the fields it is searched in, and what it looks for.

    ``pattern`` is what every field takes unless ``patterns`` names another for
    one of them: a word that means one thing in a job title can mean something
    else in a company name, and the only default that reads a company needs to
    say so rather than reuse a pattern written for titles.
    """

    name: str
    pattern: str
    fields: tuple[RuleField, ...] = DEFAULT_FIELDS
    patterns: Mapping[RuleField, str] = field(default_factory=dict)

    def pattern_for(self, rule_field: RuleField) -> str:
        return self.patterns.get(rule_field, self.pattern)


DEFAULT_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    (
        "c-suite",
        r"\b(CEO|CFO|COO|CTO|CIO|CMO|CPO|CRO|CHRO|CISO|CSO|CDO"
        r"|Chief\s+(\w+[\s-]+){1,2}Officer)\b",
    ),
    ("vp", r"\b(VP|SVP|EVP|AVP|Vice\s+President)\b"),
    ("director", r"\bdirector\b"),
    ("founder", r"\b((co-?)?founders?|founding\s+(partner|engineer|member|team))\b"),
    (
        "investor",
        r"\b(investors?|investing|venture\s+(capital|partner)|VC|angel"
        r"|general\s+partner|private\s+equity)\b",
    ),
    (
        "recruiter",
        r"\b(recruit(er|ers|ing|ment)|talent\s+(acquisition|partner)|sourcer|headhunter"
        r"|staffing)\b",
    ),
    (
        "engineering",
        r"\b(engineer(s|ing)?|developer|software|SRE|devops|programmer"
        r"|data\s+scien(ce|tist)|machine\s+learning)\b",
    ),
    (
        "product",
        r"\b(product\s+(manager|management|owner|lead|leader|director|head|strategy)"
        r"|(head|director|vp|svp|evp|chief)[,\s]+(of\s+)?product|CPO)\b",
    ),
    ("design", r"\b(designers?|design|UX|UI|user\s+experience|creative\s+director)\b"),
    (
        "sales",
        r"\b(sales|account\s+(executive|manager|director)|business\s+development|BDR|SDR"
        r"|revenue|customer\s+success)\b",
    ),
    (
        "marketing",
        r"\b(marketing|marketer|brand|communications|demand\s+gen(eration)?"
        r"|growth\s+(marketing|lead|manager)|SEO|PR)\b",
    ),
    ("consultant", r"\b(consultants?|consulting|advis(or|er|ory)|fractional|freelancer?)\b"),
    (
        "academic",
        r"\b(professor|lecturer|research\s+(scientist|fellow|associate|assistant|professor)"
        r"|PhD|Ph\.?D|postdoc(toral)?|faculty|dean|academic|scholar)\b",
    ),
)
"""``(tag name, pattern)`` for the defaults that take the usual fields."""

DEFAULTS: Final[tuple[DefaultRule, ...]] = (
    *(DefaultRule(name, pattern) for name, pattern in DEFAULT_PATTERNS),
    DefaultRule(
        "retired",
        # ``retired``, ``retiree``, ``retiring`` -- and deliberately not
        # ``retirement``, which is the word in the titles of people who work
        # *in* retirement: a retirement planner is not retired.
        r"\b(retired|retiree|retiring)\b",
        # The one default that searches the company too. "Retired" as an
        # employer is how LinkedIn says it as often as a title does -- in the
        # reference address book, eight people say it in the title and two more
        # say it only there -- and unlike "sales" or "engineering" the company
        # name is the statement rather than the industry somebody works in.
        fields=(RuleField.TITLE, RuleField.HEADLINE, RuleField.COMPANY),
        patterns={
            # Anchored, because in the middle of a company name the same word
            # describes who an organization serves rather than the person:
            # "American Association of Retired Persons" employs people who are
            # working. At the front it is somebody answering "where do you
            # work?" with "Retired" -- "Retired", "Retired Inc.", "Retired!".
            RuleField.COMPANY: r"^\W*(retired|retiree|retiring)\b",
        },
    ),
)
"""Every default tag, in display order."""


# --- errors -----------------------------------------------------------------


class TagError(Exception):
    """Base of everything this module raises on purpose."""


class TagNotFound(TagError, LookupError):
    """No such tag for this user."""


class RuleNotFound(TagError, LookupError):
    """No such rule for this user."""


class ContactNotFound(TagError, LookupError):
    """No such contact for this user."""


class DuplicateTag(TagError, ValueError):
    """The user already has a tag by that name (case-insensitively)."""


class InvalidTagValue(TagError, ValueError):
    """A name, color, or reorder request that cannot be stored."""


class InvalidPattern(TagError, ValueError):
    """A rule pattern that is empty, too long, invalid, or may backtrack catastrophically."""


class TagSuppressed(TagError):
    """An automatic assignment was refused because the user removed that tag before."""


# --- results ----------------------------------------------------------------


@dataclass(frozen=True)
class TagWithCount:
    tag: Tag
    contact_count: int
    """Live contacts (not merged away, not archived) carrying the tag."""


@dataclass(frozen=True)
class RulePreview:
    """What a pattern would match, without writing anything."""

    count: int
    contact_ids: tuple[int, ...]
    """The first :data:`PREVIEW_SAMPLE` matching contacts, by id."""
    timeouts: int = 0
    """Contacts the pattern timed out on, counted as no match."""


@dataclass(frozen=True)
class RuleRun:
    """What a run did."""

    contacts: int
    """Contacts examined."""
    added: int
    """``rule`` assignments created."""
    removed: int
    """``rule`` assignments deleted because no enabled rule matches their tag any more."""
    updated: int
    """``rule`` assignments credited to a different rule."""
    timeouts: int = 0
    """Searches that hit :data:`MATCH_TIMEOUT_S`, each leaving its tag undecided."""


class Unset(enum.Enum):
    """The type of :data:`UNSET`."""

    TOKEN = 0


UNSET: Final = Unset.TOKEN
"""For :func:`update_tag`: "leave ``color`` alone", as distinct from ``None`` ("clear it")."""


# --- validation -------------------------------------------------------------


def _require_writer(session: Session) -> None:
    """Fail at once rather than with "database is locked" on the first write (CLAUDE.md)."""
    if not is_writer(session):
        raise RuntimeError("tag operations need a writer session; use session_scope(write=True)")


def compile_pattern(pattern: str) -> regex.Pattern[str]:
    """``pattern`` compiled for a case-insensitive search, or :class:`InvalidPattern` saying why.

    The standard library's parser checks the syntax and provides the tree that
    :func:`has_nested_unbounded_repeat` walks; the ``regex`` package does the
    compiling, because its ``search`` takes a timeout (see :func:`search`).
    """
    if not pattern.strip():
        raise InvalidPattern("pattern is empty")
    if len(pattern) > PATTERN_MAX_LENGTH:
        raise InvalidPattern(f"pattern is longer than {PATTERN_MAX_LENGTH} characters")
    try:
        tree = _parser.parse(pattern, re.IGNORECASE)
    except (re.error, OverflowError) as exc:
        # A repeat count at or above MAXREPEAT raises OverflowError, which is not an re.error.
        raise InvalidPattern(f"invalid regular expression: {exc}") from exc
    if has_nested_unbounded_repeat(tree):
        raise InvalidPattern(
            "pattern may run slowly: an unbounded repeat (+, *, {n,}) inside another one, "
            "as in (a+)+, can take exponential time; rewrite it without the nesting"
        )
    if expansion(tree) > MAX_EXPANSION:
        raise InvalidPattern(
            f"pattern may use too much memory: nested counted repeats multiply out to more "
            f"than {MAX_EXPANSION} copies, as in (?:a{{100}}){{100}}; lower the counts"
        )
    try:
        return regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        raise InvalidPattern(f"invalid regular expression: {exc}") from exc


_REPEATS: Final[frozenset[Any]] = frozenset(
    {_parser.MAX_REPEAT, _parser.MIN_REPEAT, _parser.POSSESSIVE_REPEAT}
)


def has_nested_unbounded_repeat(tree: Any, *, inside_unbounded: bool = False) -> bool:
    """True when a repeat with no upper bound contains another one, anywhere below it.

    ``tree`` is what ``re._parser.parse`` returns: a sequence of ``(opcode,
    arguments)`` pairs. ``(a+)+``, ``(a*)*``, ``(a+)*``, ``(a*)+``, and
    ``(a{2,})+`` are the shapes; a bounded outer repeat (``(\\w+\\s+){1,2}``) is
    not, and neither is ``a+b+``. The walk looks through groups, alternations,
    lookarounds, atomic groups, and conditionals.
    """
    for op, args in tree:
        if op in _REPEATS:
            _low, high, body = args
            unbounded = high == _parser.MAXREPEAT
            if unbounded and inside_unbounded:
                return True
            if has_nested_unbounded_repeat(body, inside_unbounded=inside_unbounded or unbounded):
                return True
        elif op is _parser.SUBPATTERN:
            if has_nested_unbounded_repeat(args[3], inside_unbounded=inside_unbounded):
                return True
        elif op is _parser.BRANCH:
            if any(
                has_nested_unbounded_repeat(branch, inside_unbounded=inside_unbounded)
                for branch in args[1]
            ):
                return True
        elif op in (_parser.ASSERT, _parser.ASSERT_NOT):
            if has_nested_unbounded_repeat(args[1], inside_unbounded=inside_unbounded):
                return True
        elif op is _parser.ATOMIC_GROUP:
            if has_nested_unbounded_repeat(args, inside_unbounded=inside_unbounded):
                return True
        elif op is _parser.GROUPREF_EXISTS:
            _group, yes, no = args
            for branch in (yes, no):
                if branch is not None and has_nested_unbounded_repeat(
                    branch, inside_unbounded=inside_unbounded
                ):
                    return True
    return False


def expansion(tree: Any) -> int:
    """How many copies of the most-repeated branch a counted repeat multiplies out to.

    ``regex`` expands ``a{n}`` at compile time, so nested counted repeats cost
    memory as their product: ``(?:(?:a{300}){300}){300}`` is twenty-four
    characters and needs about six gigabytes, which :data:`MATCH_TIMEOUT_S`
    does not bound because it is spent before the first search. Unbounded
    repeats are not counted here; :func:`has_nested_unbounded_repeat` owns
    those. The result saturates at :data:`MAX_EXPANSION` so a deep tree cannot
    build a huge integer on the way to being refused.
    """
    total = 1
    for op, args in tree:
        if op in _REPEATS:
            _low, high, body = args
            inner = expansion(body)
            total *= inner if high == _parser.MAXREPEAT else min(high, MAX_EXPANSION + 1) * inner
        elif op is _parser.SUBPATTERN:
            total *= expansion(args[3])
        elif op is _parser.BRANCH:
            total *= max((expansion(branch) for branch in args[1]), default=1)
        elif op in (_parser.ASSERT, _parser.ASSERT_NOT):
            total *= expansion(args[1])
        elif op is _parser.ATOMIC_GROUP:
            total *= expansion(args)
        elif op is _parser.GROUPREF_EXISTS:
            _group, yes, no = args
            total *= max(expansion(b) for b in (yes, no) if b is not None)
        if total > MAX_EXPANSION:
            return MAX_EXPANSION + 1
    return total


def search(compiled: regex.Pattern[str], value: str) -> bool | None:
    """Whether ``compiled`` matches somewhere in ``value``; None when the search timed out.

    The timeout is :data:`MATCH_TIMEOUT_S`, read at call time so a test can
    shorten it. A timeout is the caller's to log and count; it never raises.
    """
    try:
        return compiled.search(value, timeout=MATCH_TIMEOUT_S) is not None
    except TimeoutError:
        return None


def clean_tag_name(name: str) -> str:
    """``name`` stripped, or :class:`InvalidTagValue` when empty or too long."""
    cleaned = name.strip()
    if not cleaned:
        raise InvalidTagValue("tag name is empty")
    if len(cleaned) > TAG_NAME_MAX_LENGTH:
        raise InvalidTagValue(f"tag name is longer than {TAG_NAME_MAX_LENGTH} characters")
    return cleaned


def clean_color(color: str | None) -> str | None:
    """``color`` as lowercase ``#rrggbb``, None as None, else :class:`InvalidTagValue`."""
    if color is None:
        return None
    if not _COLOR.fullmatch(color):
        raise InvalidTagValue("color must be #rrggbb")
    return color.lower()


# --- tags -------------------------------------------------------------------


def list_tags(session: Session, user: User) -> list[TagWithCount]:
    """Every tag of ``user`` with its live contact count, by name."""
    tags = list(session.scalars(scoped(user, Tag).order_by(Tag.name_key, Tag.id)))
    counts = contact_counts(session, user, [tag.id for tag in tags])
    return [TagWithCount(tag, counts.get(tag.id, 0)) for tag in tags]


def get_tag(session: Session, user: User, tag_id: int) -> Tag:
    """The tag, or :class:`TagNotFound`."""
    tag = get_scoped(session, user, Tag, tag_id)
    if tag is None:
        raise TagNotFound(f"no tag {tag_id}")
    return tag


def find_tag(session: Session, user: User, name: str) -> Tag | None:
    """The user's tag named ``name``, matched without regard to case, or None."""
    return session.scalars(
        scoped(user, Tag).where(Tag.name_key == tag_name_key(name))
    ).one_or_none()


def contact_counts(session: Session, user: User, tag_ids: Iterable[int]) -> dict[int, int]:
    """Tag id to the number of live contacts carrying it; tags with none are absent."""
    ids = list(tag_ids)
    if not ids:
        return {}
    statement = (
        scoped(user, ContactTag)
        .with_only_columns(ContactTag.tag_id, func.count())
        .join(Contact, Contact.id == ContactTag.contact_id)
        .where(ContactTag.tag_id.in_(ids), Contact.user_id == user.id, _live())
        .group_by(ContactTag.tag_id)
    )
    return {int(tag_id): int(count) for tag_id, count in session.execute(statement).all()}


def create_tag(
    session: Session,
    user: User,
    name: str,
    *,
    color: str | None = None,
    kind: TagKind = TagKind.MANUAL,
    met_signal: TagMetSignal | None = None,
) -> Tag:
    """A new tag, flushed. :class:`DuplicateTag` when the name is taken (any case)."""
    _require_writer(session)
    cleaned = clean_tag_name(name)
    if find_tag(session, user, cleaned) is not None:
        raise DuplicateTag(f"a tag named {cleaned!r} already exists")
    tag = Tag(
        user_id=user.id,
        name=cleaned,
        color=clean_color(color),
        kind=kind,
        met_signal=None if met_signal is None else TagMetSignal(met_signal),
    )
    session.add(tag)
    session.flush()
    log.debug("created tag %d %r for user %d", tag.id, tag.name, user.id)
    return tag


def update_tag(
    session: Session,
    user: User,
    tag_id: int,
    *,
    name: str | None = None,
    color: str | Unset | None = UNSET,
    met_signal: TagMetSignal | Unset | None = UNSET,
) -> Tag:
    """Rename, recolor, or give a tag its triage meaning.

    ``color=None`` and ``met_signal=None`` clear those; omitted leaves them.
    ``met_signal`` is the user saying what the tag means for triage (spec 10.2):
    it puts a batch on offer and takes one off, and it decides nobody by itself.
    """
    _require_writer(session)
    tag = get_tag(session, user, tag_id)
    if name is not None:
        cleaned = clean_tag_name(name)
        other = find_tag(session, user, cleaned)
        if other is not None and other.id != tag.id:
            raise DuplicateTag(f"a tag named {cleaned!r} already exists")
        tag.name = cleaned
    if not isinstance(color, Unset):
        tag.color = clean_color(color)
    if not isinstance(met_signal, Unset):
        tag.met_signal = None if met_signal is None else TagMetSignal(met_signal)
    session.flush()
    return tag


def delete_tag(session: Session, user: User, tag_id: int) -> None:
    """Delete a tag; the database cascades to its assignments, suppressions, and rules."""
    _require_writer(session)
    tag = get_tag(session, user, tag_id)
    session.execute(scoped_delete(user, Tag).where(Tag.id == tag.id))
    session.expunge(tag)
    log.debug("deleted tag %d for user %d", tag_id, user.id)


# --- assignments ------------------------------------------------------------


def tag_contact(
    session: Session,
    user: User,
    contact_id: int,
    tag_id: int,
    *,
    source: TagSource = TagSource.MANUAL,
) -> ContactTag:
    """Put ``tag_id`` on ``contact_id`` and return the assignment (existing or new).

    A ``manual`` assignment clears a suppression of the tag on the contact and
    takes over an existing automatic assignment. An automatic source
    (``rule``, ``llm``) leaves an existing assignment as it is and raises
    :class:`TagSuppressed` when the user removed that tag from that contact.
    """
    _require_writer(session)
    _contact(session, user, contact_id)
    get_tag(session, user, tag_id)
    if source is TagSource.MANUAL:
        session.execute(
            scoped_delete(user, ContactTagSuppression).where(
                ContactTagSuppression.contact_id == contact_id,
                ContactTagSuppression.tag_id == tag_id,
            )
        )
    elif _is_suppressed(session, user, contact_id, tag_id):
        raise TagSuppressed(f"tag {tag_id} was removed from contact {contact_id} by the user")
    existing = _assignment(session, user, contact_id, tag_id)
    if existing is not None:
        if source is TagSource.MANUAL and existing.source is not TagSource.MANUAL:
            existing.source = TagSource.MANUAL
            existing.rule_id = None
            session.flush()
        return existing
    row = ContactTag(user_id=user.id, contact_id=contact_id, tag_id=tag_id, source=source)
    session.add(row)
    session.flush()
    return row


def contact_tags(session: Session, user: User, contact_id: int) -> list[ContactTag]:
    """Every tag assignment on ``contact_id``, by tag name; :class:`ContactNotFound`.

    The Contacts detail screen reads a contact's tags with this (spec 10.1);
    ``ContactRow`` deliberately carries none, so the table asks per contact.
    """
    _contact(session, user, contact_id)
    return list(
        session.scalars(
            scoped(user, ContactTag)
            .join(Tag, Tag.id == ContactTag.tag_id)
            .where(ContactTag.contact_id == contact_id, Tag.user_id == user.id)
            .order_by(Tag.name_key, Tag.id)
        )
    )


def untag_contact(session: Session, user: User, contact_id: int, tag_id: int) -> bool:
    """Remove ``tag_id`` from ``contact_id``; True when there was an assignment.

    Removing an automatic assignment writes a suppression, so no run re-adds it.
    """
    _require_writer(session)
    row = _assignment(session, user, contact_id, tag_id)
    if row is None:
        return False
    if row.source is not TagSource.MANUAL and not _is_suppressed(session, user, contact_id, tag_id):
        session.add(ContactTagSuppression(user_id=user.id, contact_id=contact_id, tag_id=tag_id))
    session.delete(row)
    session.flush()
    return True


def _assignment(session: Session, user: User, contact_id: int, tag_id: int) -> ContactTag | None:
    return session.scalars(
        scoped(user, ContactTag).where(
            ContactTag.contact_id == contact_id, ContactTag.tag_id == tag_id
        )
    ).one_or_none()


def _is_suppressed(session: Session, user: User, contact_id: int, tag_id: int) -> bool:
    return (
        session.scalars(
            scoped(user, ContactTagSuppression).where(
                ContactTagSuppression.contact_id == contact_id,
                ContactTagSuppression.tag_id == tag_id,
            )
        ).first()
        is not None
    )


def _contact(session: Session, user: User, contact_id: int) -> Contact:
    contact = get_scoped(session, user, Contact, contact_id)
    if contact is None:
        raise ContactNotFound(f"no contact {contact_id}")
    return contact


# --- rules ------------------------------------------------------------------


def list_rules(session: Session, user: User) -> list[AutotagRule]:
    """Every rule of ``user`` in position order."""
    return list(
        session.scalars(scoped(user, AutotagRule).order_by(AutotagRule.position, AutotagRule.id))
    )


def get_rule(session: Session, user: User, rule_id: int) -> AutotagRule:
    """The rule, or :class:`RuleNotFound`."""
    rule = get_scoped(session, user, AutotagRule, rule_id)
    if rule is None:
        raise RuleNotFound(f"no rule {rule_id}")
    return rule


def create_rule(
    session: Session,
    user: User,
    tag_id: int,
    field: RuleField,
    pattern: str,
    *,
    enabled: bool = True,
) -> AutotagRule:
    """A new rule at the end of the order, flushed. Validates the pattern first."""
    _require_writer(session)
    compile_pattern(pattern)
    get_tag(session, user, tag_id)
    last = session.scalar(
        scoped(user, AutotagRule).with_only_columns(func.max(AutotagRule.position))
    )
    rule = AutotagRule(
        user_id=user.id,
        tag_id=tag_id,
        field=field,
        pattern=pattern,
        enabled=enabled,
        position=0 if last is None else int(last) + 1,
    )
    session.add(rule)
    session.flush()
    return rule


def update_rule(
    session: Session,
    user: User,
    rule_id: int,
    *,
    tag_id: int | None = None,
    field: RuleField | None = None,
    pattern: str | None = None,
    enabled: bool | None = None,
) -> AutotagRule:
    """Change any of a rule's tag, field, pattern, and enabled flag. Validates the pattern."""
    _require_writer(session)
    rule = get_rule(session, user, rule_id)
    if pattern is not None:
        compile_pattern(pattern)
        rule.pattern = pattern
    if tag_id is not None:
        get_tag(session, user, tag_id)
        rule.tag_id = tag_id
    if field is not None:
        rule.field = field
    if enabled is not None:
        rule.enabled = enabled
    session.flush()
    return rule


def delete_rule(session: Session, user: User, rule_id: int) -> None:
    """Delete a rule. Its assignments stay, with ``rule_id`` cleared, until the next run."""
    _require_writer(session)
    rule = get_rule(session, user, rule_id)
    session.execute(scoped_delete(user, AutotagRule).where(AutotagRule.id == rule.id))
    session.expunge(rule)


def reorder_rules(session: Session, user: User, rule_ids: Sequence[int]) -> list[AutotagRule]:
    """Put the rules in ``rule_ids`` first, in that order; the rest keep their order after them.

    :class:`RuleNotFound` for an id that is not one of ``user``'s rules,
    :class:`InvalidTagValue` for a repeated id.
    """
    _require_writer(session)
    if len(set(rule_ids)) != len(rule_ids):
        raise InvalidTagValue("rule_ids repeats an id")
    rules = list_rules(session, user)
    by_id = {rule.id: rule for rule in rules}
    missing = [rule_id for rule_id in rule_ids if rule_id not in by_id]
    if missing:
        raise RuleNotFound(f"no rule {missing[0]}")
    listed = set(rule_ids)
    ordered = [by_id[rule_id] for rule_id in rule_ids]
    ordered.extend(rule for rule in rules if rule.id not in listed)
    for position, rule in enumerate(ordered):
        if rule.position != position:
            rule.position = position
    session.flush()
    return ordered


# --- running ----------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One contact's searchable fields, as a run or a preview reads them."""

    id: int
    values: dict[RuleField, str | None]

    def search(self, field: RuleField, pattern: regex.Pattern[str]) -> bool | None:
        """:func:`search` over the field's value; False for an empty field."""
        value = self.values.get(field)
        return False if value is None else search(pattern, value)


def _live() -> ColumnElement[bool]:
    return and_(Contact.merged_into_id.is_(None), Contact.archived_at.is_(None))


def _candidates(
    session: Session, user: User, contact_ids: Sequence[int] | None
) -> list[_Candidate]:
    """The live contacts a run or preview looks at, by id, with the three searchable fields."""
    statement = (
        scoped(user, Contact)
        .with_only_columns(
            Contact.id, Contact.current_title, Contact.headline, Contact.current_company
        )
        .where(_live())
        .order_by(Contact.id)
    )
    if contact_ids is not None:
        if not contact_ids:
            return []
        statement = statement.where(Contact.id.in_(list(contact_ids)))
    return [
        _Candidate(
            int(row.id),
            {
                RuleField.TITLE: row.current_title,
                RuleField.HEADLINE: row.headline,
                RuleField.COMPANY: row.current_company,
            },
        )
        for row in session.execute(statement)
    ]


def preview_matches(
    session: Session,
    user: User,
    field: RuleField,
    pattern: str,
    *,
    sample: int = PREVIEW_SAMPLE,
) -> RulePreview:
    """How many live contacts ``pattern`` matches in ``field``, and the first ``sample`` ids."""
    compiled = compile_pattern(pattern)
    matched: list[int] = []
    timeouts = 0
    for candidate in _candidates(session, user, None):
        hit = candidate.search(field, compiled)
        if hit is None:
            timeouts += 1
            log.warning(
                "pattern preview timed out after %d ms on contact %d; counted as no match",
                MATCH_TIMEOUT_S * 1000,
                candidate.id,
            )
        elif hit:
            matched.append(candidate.id)
    return RulePreview(count=len(matched), contact_ids=tuple(matched[:sample]), timeouts=timeouts)


def preview_rule(session: Session, user: User, field: RuleField, pattern: str) -> int:
    """The "matches N contacts" number for the rule editor (spec 10.3). Writes nothing."""
    return preview_matches(session, user, field, pattern, sample=0).count


def run_rules(session: Session, user: User, contact_ids: Sequence[int] | None = None) -> RuleRun:
    """Apply every enabled rule to ``contact_ids`` (default: every live contact).

    See the module docstring for what is added, kept, and removed. Runs in the
    caller's transaction; ``RuntimeError`` when ``session`` is not a writer.
    """
    _require_writer(session)
    enabled = [rule for rule in list_rules(session, user) if rule.enabled]
    return _reconcile(session, user, enabled, contact_ids, tag_ids=None)


def run_rule(
    session: Session, user: User, rule_id: int, contact_ids: Sequence[int] | None = None
) -> RuleRun:
    """Reconcile the tag ``rule_id`` feeds, using every enabled rule for that tag.

    Assignments of other tags are untouched. A disabled rule contributes
    nothing, so running it removes what no enabled rule for its tag justifies,
    exactly as a full run would.
    """
    _require_writer(session)
    rule = get_rule(session, user, rule_id)
    enabled = [
        other
        for other in list_rules(session, user)
        if other.enabled and other.tag_id == rule.tag_id
    ]
    return _reconcile(session, user, enabled, contact_ids, tag_ids={rule.tag_id})


def _reconcile(
    session: Session,
    user: User,
    rules: Sequence[AutotagRule],
    contact_ids: Sequence[int] | None,
    *,
    tag_ids: set[int] | None,
) -> RuleRun:
    """The run: ``rules`` are the enabled rules that count; ``tag_ids`` limits which
    tags' ``rule`` assignments are reconciled (None: all of them)."""
    compiled = [(rule, compile_pattern(rule.pattern)) for rule in rules]
    candidates = _candidates(session, user, contact_ids)
    added = removed = updated = timeouts = 0
    per_rule_timeouts: dict[int, int] = {}
    for start in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[start : start + BATCH_SIZE]
        ids = [c.id for c in batch]
        assignments = _assignments_by_contact(session, user, ids, tag_ids)
        suppressed = _suppressions_by_contact(session, user, ids)
        for candidate in batch:
            matched: dict[int, list[int]] = {}  # tag id -> ids of the rules that matched
            unknown: set[int] = set()  # tag ids a timeout left undecided for this contact
            for rule, pattern in compiled:
                if per_rule_timeouts.get(rule.id, 0) >= MATCH_TIMEOUT_GIVE_UP:
                    unknown.add(rule.tag_id)
                    continue
                hit = candidate.search(rule.field, pattern)
                if hit is None:
                    timeouts += 1
                    unknown.add(rule.tag_id)
                    seen = per_rule_timeouts[rule.id] = per_rule_timeouts.get(rule.id, 0) + 1
                    log.warning(
                        "auto-tag rule %d timed out after %d ms on contact %d; no tag is added "
                        "or removed for it",
                        rule.id,
                        MATCH_TIMEOUT_S * 1000,
                        candidate.id,
                    )
                    if seen >= MATCH_TIMEOUT_GIVE_UP:
                        log.warning(
                            "auto-tag rule %d has timed out on %d contacts; skipping it for the "
                            "rest of this run so it cannot hold the write lock",
                            rule.id,
                            seen,
                        )
                elif hit:
                    matched.setdefault(rule.tag_id, []).append(rule.id)
            existing = assignments.get(candidate.id, {})
            for tag_id, rule_ids in matched.items():
                row = existing.get(tag_id)
                if row is None:
                    if tag_id in suppressed.get(candidate.id, set()):
                        continue
                    session.add(
                        ContactTag(
                            user_id=user.id,
                            contact_id=candidate.id,
                            tag_id=tag_id,
                            source=TagSource.RULE,
                            rule_id=rule_ids[0],
                        )
                    )
                    added += 1
                elif row.source is TagSource.RULE and row.rule_id not in rule_ids:
                    row.rule_id = rule_ids[0]
                    updated += 1
            for tag_id, row in existing.items():
                if row.source is TagSource.RULE and tag_id not in matched and tag_id not in unknown:
                    session.delete(row)
                    removed += 1
        session.flush()
    result = RuleRun(
        contacts=len(candidates),
        added=added,
        removed=removed,
        updated=updated,
        timeouts=timeouts,
    )
    log.info(
        "auto-tag run for user %d over %d contacts: %d added, %d removed, %d re-credited, "
        "%d timed out",
        user.id,
        result.contacts,
        result.added,
        result.removed,
        result.updated,
        result.timeouts,
    )
    return result


def _assignments_by_contact(
    session: Session, user: User, contact_ids: Sequence[int], tag_ids: set[int] | None
) -> dict[int, dict[int, ContactTag]]:
    statement = scoped(user, ContactTag).where(ContactTag.contact_id.in_(contact_ids))
    if tag_ids is not None:
        statement = statement.where(ContactTag.tag_id.in_(tag_ids))
    found: dict[int, dict[int, ContactTag]] = {}
    for row in session.scalars(statement):
        found.setdefault(row.contact_id, {})[row.tag_id] = row
    return found


def _suppressions_by_contact(
    session: Session, user: User, contact_ids: Sequence[int]
) -> dict[int, set[int]]:
    statement = (
        scoped(user, ContactTagSuppression)
        .with_only_columns(ContactTagSuppression.contact_id, ContactTagSuppression.tag_id)
        .where(ContactTagSuppression.contact_id.in_(contact_ids))
    )
    found: dict[int, set[int]] = {}
    for contact_id, tag_id in session.execute(statement):
        found.setdefault(int(contact_id), set()).add(int(tag_id))
    return found


# --- defaults ---------------------------------------------------------------


def ensure_default_rules(session: Session, user: User) -> list[AutotagRule]:
    """Seed the default tags and rules for ``user`` once; return the rules created.

    Idempotent: after the first call ``settings_kv`` records
    ``tags.defaults_seeded`` and later calls return ``[]``, so a default the
    user deleted is never re-created. A tag the user already has by a default's
    name is reused as it is.
    """
    _require_writer(session)
    seeded = _seeded_names(session, user)
    wanted = [default for default in DEFAULTS if default.name not in seeded]
    if not wanted:
        return []
    created: list[AutotagRule] = []
    # What this user already has, so seeding twice cannot mean the same rule
    # twice. A tag is reused by name and a rule has no such key, so without
    # this a record that came back unreadable -- or an empty list, which reads
    # as "nothing offered yet" and cannot be told from a new user -- would add
    # another copy of every default rule on every run.
    existing = {(rule.tag_id, rule.field, rule.pattern) for rule in list_rules(session, user)}
    for default in wanted:
        tag = find_tag(session, user, default.name)
        if tag is None:
            tag = create_tag(session, user, default.name, kind=TagKind.AUTO)
        for rule_field in default.fields:
            pattern = default.pattern_for(rule_field)
            if (tag.id, rule_field, pattern) in existing:
                continue
            created.append(create_rule(session, user, tag.id, rule_field, pattern))
    set_setting(
        session, user, DEFAULTS_SEEDED_KEY, sorted(seeded | {default.name for default in wanted})
    )
    log.info("seeded %d default auto-tag rules for user %d", len(created), user.id)
    return created


def _seeded_names(session: Session, user: User) -> set[str]:
    """Which defaults this user has ever been offered, by tag name.

    The record used to be one boolean, which answered "has this user been
    seeded" and could not answer "with what" -- so a default added later never
    reached anybody who had started before it existed. It is a list of names
    now, and ``True`` from the old shape means every default that existed when
    it was written, which is all of them but the ones added since.

    A value in neither shape -- a number, a string, a list with something
    other than names in it, or an empty list, which this never writes because a
    seeding that happened always named something -- is read as **everything**,
    so a database whose record cannot be understood is left alone. The other direction is worse
    than it looks: "seeded nothing" reseeds, and while a tag is reused by name,
    a rule is not, so every run would add another copy of all of them. Missing
    entirely is a different thing from unreadable, and still means a new user.
    """
    stored = get_setting(session, user, DEFAULTS_SEEDED_KEY)
    if stored is None:
        return set()
    if isinstance(stored, list) and stored and all(isinstance(name, str) for name in stored):
        return set(stored)
    if stored is True:
        return {name for name, _ in DEFAULT_PATTERNS}
    log.warning(
        "user %d has an unreadable %s (%r); leaving the defaults alone",
        user.id,
        DEFAULTS_SEEDED_KEY,
        stored,
    )
    return {default.name for default in DEFAULTS}
