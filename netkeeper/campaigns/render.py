"""Template rendering and lint (spec 11.1; item P3-03). Pure: no session, no I/O.

The sandbox
-----------
Every template renders in :class:`jinja2.sandbox.ImmutableSandboxedEnvironment`,
changed so that it cannot be walked out of and cannot be made to run long or
allocate much. A preview renders inside the shared server, and the P3-06
worker will render on every send.

- Access to an unsafe attribute *raises*. The stock sandbox returns an
  undefined value that prints as an empty string, so ``{{ ''.__class__ }}``
  would render as nothing and look harmless. Here it fails the render
  (:class:`TemplateRenderError`), and lint refuses any ``_``-prefixed name
  before a template is ever rendered (:attr:`LintRule.UNSAFE_ATTRIBUTE`).
- No globals. ``range``, ``cycler``, ``joiner``, ``namespace``, ``dict`` and
  ``lipsum`` are gone, so the only names a template can reach are the merge
  fields below; anything else is :attr:`LintRule.UNDEFINED_VARIABLE`.
- No control structures that repeat or define: ``for``, ``macro``, ``call``,
  ``set``, ``block``, ``include``, ``extends``, ``import``, and the name
  ``self`` are refused by lint and by the render (:attr:`LintRule.UNSUPPORTED`).
  Merge fields are scalars, so a message has nothing to loop over; without
  loops and recursion a render's work is bounded by the template's length.
- Every operation that can make a value much larger than its inputs is
  bounded. ``*`` on a string or list, the ``center``, ``indent``,
  ``truncate``, ``replace``, ``join`` and ``wordwrap`` filters all draw on one
  budget of :data:`MAX_OUTPUT_CHARS` per render; ``**`` is capped at
  :data:`MAX_POWER_BITS`; ``%`` string formatting, ``str.format``, and the
  ``batch``, ``slice`` and ``format`` filters are gone. A method call on a
  value is allowed only for methods whose result is no larger than the value
  (:data:`BOUNDED_ATTRIBUTES`), and the output stops at
  :data:`MAX_OUTPUT_CHARS`. Hitting any of these is a :class:`TemplateRenderError`.
- Merge values are data, never template source. A contact whose name is
  ``{{ me.name }}`` (imported data is not trusted) renders that text literally.
- The rendered subject is one line: CR, LF and the other line breaks become
  a space, so no merge value can add an email header.

Autoescape is off: every template is plain text today (email and LinkedIn).
An HTML body needs it on (spec 11.1), which netkeeper does not build yet.

Merge fields (spec 11.1)
------------------------
- Contact: :data:`CONTACT_FIELDS`. ``first_name`` resolves to the contact's
  preferred name.
- You: ``me.<key>`` for :data:`ME_FIELDS` and any extra key under ``[me]``.
- Campaign: ``campaign.name``, ``step.number``, ``previous_send_date``.
- ``personal_line`` (spec 12), written per contact at preview time.

A field with no value renders as an empty string and adds a
:attr:`LintRule.MISSING_VALUE` *warning* to the render. It never raises,
whatever the template does with it: arithmetic, comparison, a method call, or
a filter such as ``int`` or ``round`` (:class:`_Missing`). So
``{{ company | default("your team") }}`` and ``{% if company %}`` work as they
would for any undefined value.

Lint
----
:func:`lint` runs at save time over the template text alone. Every rule it
applies is an error, and :func:`has_errors` is what blocks activation:

- :attr:`LintRule.SYNTAX`: the template does not compile (this includes an
  unknown filter).
- :attr:`LintRule.UNSUPPORTED`: a construct the sandbox refuses (above).
- :attr:`LintRule.UNSAFE_ATTRIBUTE`: a ``_``-prefixed attribute or key.
- :attr:`LintRule.ATTRIBUTE_ACCESS`: an attribute or method of anything but
  ``me``, ``campaign`` and ``step``. Merge fields are plain values.
- :attr:`LintRule.UNDEFINED_VARIABLE`: a name that is not a merge field.
- :attr:`LintRule.NO_CONTACT_FIELD`: a body that names no per-contact field.
  Identical bulk mail is a spam signal.
- :attr:`LintRule.MISSING_SUBJECT`: an email template with no subject.
- :attr:`LintRule.BAD_LINK`: an ``http``/``https`` link that does not parse.

:func:`render` adds the render-time warnings: fields with no value for this
contact, and links that came out broken once merge values were filled in.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, Final
from urllib.parse import urlsplit

from jinja2 import (
    ChainableUndefined,
    TemplateError,
    TemplateSyntaxError,
    nodes,
    pass_environment,
    pass_eval_context,
)
from jinja2.environment import Environment
from jinja2.exceptions import FilterArgumentError, SecurityError
from jinja2.nodes import EvalContext
from jinja2.runtime import Context, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment

from netkeeper.config import MeSettings
from netkeeper.models.campaigns import TemplateChannel

CONTACT_FIELDS: Final = (
    "first_name",
    "last_name",
    "company",
    "title",
    "location",
    "connected_year",
    "years_since_connected",
    "last_position_change",
)
"""The contact's merge fields, as a template names them (spec 11.1)."""

PERSONAL_LINE: Final = "personal_line"
PREVIOUS_SEND_DATE: Final = "previous_send_date"

PER_CONTACT_FIELDS: Final = frozenset((*CONTACT_FIELDS, PERSONAL_LINE))
"""Fields that differ from one contact to the next. A body must name at least one."""

ME_FIELDS: Final = ("name", "website", "scheduling_link", "signature", "city")
"""``me.<key>`` fields that always exist. Extra keys under ``[me]`` join them."""

NAMESPACE_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    "me": ME_FIELDS,
    "campaign": ("name",),
    "step": ("number",),
}

SCALAR_FIELDS: Final = frozenset((*CONTACT_FIELDS, PERSONAL_LINE, PREVIOUS_SEND_DATE))

MAX_OUTPUT_CHARS: Final = 100_000
"""The most one render may produce, and the budget its amplifying operations share."""

MAX_POWER_BITS: Final = 10_000
"""The largest integer ``**`` may make, in bits: enough for any real use, cheap to compute."""

BOUNDED_ATTRIBUTES: Final = frozenset(
    {
        # str methods whose result is no larger than the string
        "capitalize", "casefold", "count", "endswith", "find", "index", "isalnum",
        "isalpha", "isdigit", "islower", "isspace", "istitle", "isupper", "lower",
        "lstrip", "partition", "removeprefix", "removesuffix", "rfind", "rindex",
        "rpartition", "rsplit", "rstrip", "split", "splitlines", "startswith",
        "strip", "swapcase", "title", "upper",
        # date parts
        "day", "month", "year", "isoformat", "weekday", "isoweekday",
    }
)  # fmt: skip
"""What a template may reach on a merge value at render. Lint refuses all of them anyway
(:attr:`LintRule.ATTRIBUTE_ACCESS`); this bounds what a preview of a template with lint
errors can do."""

# Constructs lint and the render refuse: each either repeats, defines, or reaches for
# another template, and a message built from scalar merge fields needs none of them.
_REFUSED_NODES: Final[Mapping[type[nodes.Node], str]] = {
    nodes.For: "for",
    nodes.Macro: "macro",
    nodes.CallBlock: "call",
    nodes.Assign: "set",
    nodes.AssignBlock: "set",
    nodes.Block: "block",
    nodes.Extends: "extends",
    nodes.Include: "include",
    nodes.Import: "import",
    nodes.FromImport: "import",
}
# Filters with no bounded use in a message: ``batch`` and ``slice`` pad to a size the
# template chooses, and ``format`` takes widths.
_REMOVED_FILTERS: Final = ("batch", "slice", "format")
# Every line break a header could be split on.
_LINE_BREAKS = re.compile(r"[\r\n\x0b\x0c\x1c-\x1e\x85\u2028\u2029]+")

# A candidate http(s) link: the scheme and whatever follows up to whitespace. Matched
# case-insensitively and loosely on purpose, so ``http:/example.com`` is caught as broken
# rather than skipped as not being a link at all.
_HTTP_LINK = re.compile(r"\bhttps?:[^\s<>\"']*", re.IGNORECASE)
# Punctuation that ends a sentence rather than a URL.
_TRAILING = ".,;:!?)]}'\""
_HOST_LABEL = re.compile(r"^[\w-]+$")
# What a merge expression becomes when links are checked in the template text before
# rendering: a value of the right shape, so ``https://{{ me.website }}`` is not
# reported as a link with no host.
_PLACEHOLDER = "x"


class Severity(enum.StrEnum):
    ERROR = "error"
    WARNING = "warning"


class LintRule(enum.StrEnum):
    SYNTAX = "syntax"
    UNSUPPORTED = "unsupported"
    UNSAFE_ATTRIBUTE = "unsafe_attribute"
    ATTRIBUTE_ACCESS = "attribute_access"
    UNDEFINED_VARIABLE = "undefined_variable"
    NO_CONTACT_FIELD = "no_contact_field"
    MISSING_SUBJECT = "missing_subject"
    BAD_LINK = "bad_link"
    MISSING_VALUE = "missing_value"


class Part(enum.StrEnum):
    """Which text of the template an issue is about."""

    SUBJECT = "subject"
    BODY = "body"


@dataclass(frozen=True, slots=True)
class LintIssue:
    """One finding. ``field`` names the merge field or link it is about, when there is one."""

    rule: LintRule
    severity: Severity
    part: Part
    message: str
    field: str | None = None

    def to_json(self) -> dict[str, str | None]:
        return {
            "rule": self.rule.value,
            "severity": self.severity.value,
            "part": self.part.value,
            "message": self.message,
            "field": self.field,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LintIssue:
        return cls(
            rule=LintRule(data["rule"]),
            severity=Severity(data["severity"]),
            part=Part(data["part"]),
            message=str(data["message"]),
            field=None if data.get("field") is None else str(data["field"]),
        )


def has_errors(issues: Collection[LintIssue]) -> bool:
    """True when any issue is an error: the template may not be activated."""
    return any(issue.severity is Severity.ERROR for issue in issues)


@dataclass(frozen=True, slots=True)
class MergeValues:
    """Everything one render can fill in. ``None`` or a blank string means "no value".

    ``contact`` is keyed by :data:`CONTACT_FIELDS`; a key left out has no value.
    ``me`` is :func:`me_fields` of the config. The campaign fields are ``None``
    outside a campaign, which is every preview until campaigns exist.
    """

    contact: Mapping[str, object]
    me: Mapping[str, str]
    campaign_name: str | None = None
    step_number: int | None = None
    previous_send_date: date | datetime | None = None
    personal_line: str | None = None


@dataclass(frozen=True, slots=True)
class Rendered:
    """A rendered template and every issue found: lint's, then the render's own warnings."""

    subject: str | None
    body: str
    issues: tuple[LintIssue, ...]


class TemplateRenderError(Exception):
    """The template cannot be rendered: it does not compile, uses a construct the sandbox
    refuses, reached past the sandbox, or went over a size limit."""


# Rules whose templates the render refuses outright, rather than rendering with the error.
_RENDER_REFUSES: Final = frozenset(
    {LintRule.SYNTAX, LintRule.UNSUPPORTED, LintRule.UNSAFE_ATTRIBUTE}
)


def me_fields(me: MeSettings) -> dict[str, str]:
    """The ``me.<key>`` values from ``[me]``: :data:`ME_FIELDS`, then any extra keys."""
    values = {name: str(getattr(me, name)) for name in ME_FIELDS}
    values.update(me.extra)
    return values


# --- the environment ----------------------------------------------------------


class _Missing(ChainableUndefined):
    """A merge field with no value. It renders as "" and never raises (P3-03 done-when).

    :class:`jinja2.ChainableUndefined` already survives attribute and item
    lookups; this also survives what a template does with a value: arithmetic
    and unary operators give itself back, so does calling it (a method of a
    missing field) and ``round``; ordering comparisons are false; ``int``,
    ``float`` and ``abs`` give 0.
    """

    __slots__ = ()

    def _self(self, *_args: Any, **_kwargs: Any) -> _Missing:
        return self

    def _false(self, _other: Any) -> bool:
        return False

    # jinja2 types each of these on Undefined as raising (``-> Never``); not raising is
    # this class's whole purpose, so each override is a deliberate break with that type.
    __add__ = __radd__ = __sub__ = __rsub__ = _self  # type: ignore[assignment]
    __mul__ = __rmul__ = __truediv__ = __rtruediv__ = _self  # type: ignore[assignment]
    __floordiv__ = __rfloordiv__ = __mod__ = __rmod__ = __pow__ = __rpow__ = _self  # type: ignore[assignment]
    __pos__ = __neg__ = __call__ = __round__ = _self  # type: ignore[assignment]
    __lt__ = __le__ = __gt__ = __ge__ = _false  # type: ignore[assignment]

    def __int__(self) -> int:  # type: ignore[override]
        return 0

    def __float__(self) -> float:  # type: ignore[override]
        return 0.0

    def __complex__(self) -> complex:  # type: ignore[override]
        return 0j

    def __abs__(self) -> int:
        return 0


class _Sandbox(ImmutableSandboxedEnvironment):
    """The sandbox (see the module docstring). One per render: it carries the budget."""

    intercepted_binops = frozenset({"*", "**", "%"})

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.budget = MAX_OUTPUT_CHARS

    def spend(self, chars: int, what: str) -> None:
        """Take ``chars`` from this render's budget; refuse once it would go below zero."""
        self.budget -= max(chars, 0)
        if self.budget < 0:
            raise SecurityError(f"{what} would take the output past {MAX_OUTPUT_CHARS} characters")

    def unsafe_undefined(self, obj: Any, attribute: str) -> Undefined:
        # The stock sandbox returns an undefined value here, which prints as "".
        raise SecurityError(
            f"access to attribute {attribute!r} of {type(obj).__name__!r} object is unsafe"
        )

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        if not super().is_safe_attribute(obj, attr, value):
            return False
        if isinstance(obj, SimpleNamespace | Undefined):
            return True  # me, campaign, step; or a field with no value
        return attr in BOUNDED_ATTRIBUTES

    def wrap_str_format(self, value: Any) -> Callable[..., str] | None:
        # A format spec takes a width, and the sandbox's own formatter honors it.
        if super().wrap_str_format(value) is not None:
            raise SecurityError("str.format is refused")
        return None

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        if operator == "*":
            for sequence, times in ((left, right), (right, left)):
                if isinstance(sequence, str | list | tuple) and isinstance(times, int):
                    self.spend(len(sequence) * times, "repeating a value")
        elif operator == "**":
            if (
                isinstance(left, int)
                and isinstance(right, int)
                and right > 0
                and abs(left) > 1
                and left.bit_length() * right > MAX_POWER_BITS
            ):
                raise SecurityError(f"a power larger than {MAX_POWER_BITS} bits is refused")
        elif operator == "%" and isinstance(left, str):
            raise SecurityError("% string formatting is refused")
        return super().call_binop(context, operator, left, right)


def _bound_filters(env: _Sandbox) -> None:
    """Replace the filters that can widen a value with ones that spend the budget first."""
    stock = dict(env.filters)
    for name in _REMOVED_FILTERS:
        del env.filters[name]

    def center(value: Any, width: int = 80) -> Any:
        if isinstance(value, Undefined):
            return value
        if isinstance(width, int):
            env.spend(width, "center")
        return stock["center"](value, width)

    def indent(s: Any, width: int | str = 4, first: bool = False, blank: bool = False) -> Any:
        if isinstance(s, Undefined):
            return s
        text = str(s)
        pad = width if isinstance(width, int) else len(str(width))
        env.spend(len(text) + pad * (text.count("\n") + 1), "indent")
        return stock["indent"](s, width, first, blank)

    @pass_environment
    def truncate(
        environment: Environment,
        s: Any,
        length: int = 255,
        killwords: bool = False,
        end: str = "...",
        leeway: int | None = None,
    ) -> Any:
        if isinstance(s, Undefined):
            return s
        env.spend(min(len(str(s)), max(length, 0)) + len(str(end)), "truncate")
        return stock["truncate"](environment, s, length, killwords, end, leeway)

    @pass_eval_context
    def replace(eval_ctx: EvalContext, s: Any, old: Any, new: Any, count: int | None = None) -> Any:
        if isinstance(s, Undefined):
            return s
        text, old_text, new_text = str(s), str(old), str(new)
        hits = len(text) + 1 if not old_text else text.count(old_text)
        if count is not None and count >= 0:
            hits = min(hits, count)
        env.spend(len(text) + hits * (len(new_text) - len(old_text)), "replace")
        return stock["replace"](eval_ctx, s, old, new, count)

    @pass_eval_context
    def join(
        eval_ctx: EvalContext, value: Iterable[Any], d: str = "", attribute: Any = None
    ) -> Any:
        if isinstance(value, Undefined):
            return value
        items = list(value)
        size = sum(len(str(item)) for item in items) + len(str(d)) * max(len(items) - 1, 0)
        env.spend(size, "join")
        return stock["join"](eval_ctx, items, d, attribute)

    @pass_environment
    def wordwrap(
        environment: Environment,
        s: Any,
        width: int = 79,
        break_long_words: bool = True,
        wrapstring: str | None = None,
        break_on_hyphens: bool = True,
    ) -> Any:
        if isinstance(s, Undefined):
            return s
        text = str(s)
        separator = environment.newline_sequence if wrapstring is None else str(wrapstring)
        # Words can leave a line half empty, so a line holds at least width // 2 characters.
        lines = 2 * len(text) // max(width, 1) + text.count("\n") + 1
        env.spend(len(text) + len(separator) * lines, "wordwrap")
        return stock["wordwrap"](
            environment, s, width, break_long_words, wrapstring, break_on_hyphens
        )

    env.filters.update(
        center=center,
        indent=indent,
        truncate=truncate,
        replace=replace,
        join=join,
        wordwrap=wordwrap,
    )


def _environment(today: date) -> _Sandbox:
    env = _Sandbox(undefined=_Missing, autoescape=False, keep_trailing_newline=True)
    env.globals.clear()
    _bound_filters(env)
    env.filters["ago"] = partial(ago, today=today)
    return env


# --- the ago filter -------------------------------------------------------------


def ago(value: object, *, today: date) -> str:
    """``{{ previous_send_date | ago }}``: how long before ``today``, in words.

    Counted in whole UTC days. No value renders as an empty string, like any
    missing field. A date on or after ``today`` is "today".

    ======== ===================
    days     phrasing
    ======== ===================
    0        today
    1        yesterday
    2-6      N days ago
    7-13     last week
    14-27    N weeks ago
    28-59    last month
    60-364   N months ago (30-day months)
    365-729  last year
    730+     N years ago (365-day years)
    ======== ===================
    """
    if value is None or isinstance(value, Undefined) or value == "":
        return ""
    if isinstance(value, datetime):
        day = value.astimezone(UTC).date() if value.tzinfo is not None else value.date()
    elif isinstance(value, date):
        day = value
    else:
        raise FilterArgumentError(f"ago needs a date, not {type(value).__name__}")
    days = (today - day).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days} days ago"
    if days < 14:
        return "last week"
    if days < 28:
        return f"{days // 7} weeks ago"
    if days < 60:
        return "last month"
    if days < 365:
        return f"{days // 30} months ago"
    if days < 730:
        return "last year"
    return f"{days // 365} years ago"


# --- lint -------------------------------------------------------------------


@dataclass(slots=True)
class _Analysis:
    """What lint learned about one part: its issues and the merge fields it names."""

    issues: list[LintIssue] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    compiled: bool = True

    def error(self, rule: LintRule, part: Part, message: str, name: str | None = None) -> None:
        issue = LintIssue(rule, Severity.ERROR, part, message, name)
        if issue not in self.issues:
            self.issues.append(issue)

    def refer(self, name: str) -> None:
        if name not in self.references:
            self.references.append(name)


def _static_key(node: nodes.Getattr | nodes.Getitem) -> str | None:
    """The attribute or string key a lookup names, or None when it is computed."""
    if isinstance(node, nodes.Getattr):
        return node.attr
    if isinstance(node.arg, nodes.Const) and isinstance(node.arg.value, str):
        return node.arg.value
    return None


def _analyse(source: str, part: Part, me_keys: Collection[str]) -> _Analysis:
    analysis = _Analysis()
    env = _environment(date.min)
    try:
        tree = env.parse(source)
        env.compile(source)  # parsing alone accepts an unknown filter
    except TemplateSyntaxError as exc:
        analysis.error(LintRule.SYNTAX, part, f"line {exc.lineno}: {exc.message}")
        analysis.compiled = False
        return analysis
    for node_type, tag in _REFUSED_NODES.items():
        for refused in tree.find_all(node_type):
            analysis.error(
                LintRule.UNSUPPORTED,
                part,
                f"line {refused.lineno}: `{tag}` is not available in a message template",
                tag,
            )

    lookups = [
        node
        for node in tree.find_all((nodes.Getattr, nodes.Getitem))
        if isinstance(node, nodes.Getattr | nodes.Getitem)
    ]
    attr_filters = [node for node in tree.find_all(nodes.Filter) if isinstance(node, nodes.Filter)]
    attr_filters = [node for node in attr_filters if node.name == "attr"]
    # ``x | attr("__class__")`` is a lookup too; the sandbox refuses it at render.
    attr_names = [
        use.args[0].value
        for use in attr_filters
        if use.args and isinstance(use.args[0], nodes.Const) and isinstance(use.args[0].value, str)
    ]
    for key in [_static_key(lookup) for lookup in lookups] + attr_names:
        if key is not None and key.startswith("_"):
            analysis.error(
                LintRule.UNSAFE_ATTRIBUTE, part, f"`{key}`: names starting with _ are refused", key
            )

    looked_into = {id(lookup.node) for lookup in lookups}
    allowed: dict[str, Collection[str]] = {**NAMESPACE_FIELDS, "me": (*ME_FIELDS, *me_keys)}

    for node in tree.find_all(nodes.Name):
        if not isinstance(node, nodes.Name) or node.ctx != "load":
            continue  # a name being assigned, which only a refused construct does
        name = node.name
        if name == "self":
            analysis.error(
                LintRule.UNSUPPORTED,
                part,
                f"line {node.lineno}: `self` is not available in a message template",
                name,
            )
        elif name in SCALAR_FIELDS:
            analysis.refer(name)
        elif name in allowed:
            if id(node) not in looked_into:
                analysis.error(
                    LintRule.UNDEFINED_VARIABLE,
                    part,
                    f"`{name}` is a group of fields; name one, like "
                    f"`{name}.{NAMESPACE_FIELDS[name][0]}`",
                    name,
                )
        else:
            analysis.error(
                LintRule.UNDEFINED_VARIABLE, part, f"`{name}` is not a merge field", name
            )

    for lookup in lookups:
        base = lookup.node
        key = _static_key(lookup)
        if isinstance(base, nodes.Name) and base.name in allowed:
            if key is None:
                analysis.error(
                    LintRule.UNDEFINED_VARIABLE,
                    part,
                    f"`{base.name}[...]` needs the field name written out",
                    base.name,
                )
            elif key.startswith("_"):
                continue  # reported as unsafe
            elif key not in allowed[base.name]:
                dotted = f"{base.name}.{key}"
                analysis.error(
                    LintRule.UNDEFINED_VARIABLE, part, f"`{dotted}` is not a merge field", dotted
                )
            else:
                analysis.refer(f"{base.name}.{key}")
        elif key is not None and not key.startswith("_"):
            # An index (``first_name[0]``) is a plain value's own; an attribute is not.
            dotted = f"{base.name}.{key}" if isinstance(base, nodes.Name) else key
            analysis.error(
                LintRule.ATTRIBUTE_ACCESS,
                part,
                f"`{dotted}`: merge fields are plain values, with no attributes or methods",
                dotted,
            )
    for use in attr_filters:
        analysis.error(
            LintRule.ATTRIBUTE_ACCESS,
            part,
            f"line {use.lineno}: the `attr` filter is not available in a message template",
            "attr",
        )

    for link in _bad_links(_text_with_placeholders(env, source)):
        analysis.error(LintRule.BAD_LINK, part, f"`{link}` is not a link that parses", link)
    return analysis


def _text_with_placeholders(env: _Sandbox, source: str) -> str:
    """``source`` with each ``{{ }}`` replaced by a placeholder and each tag by a space."""
    out: list[str] = []
    inside: str | None = None
    for _lineno, kind, value in env.lex(source):
        if inside is not None:
            if kind == inside:
                inside = None
            continue
        if kind == "data":
            out.append(value)
        elif kind == "variable_begin":
            out.append(_PLACEHOLDER)
            inside = "variable_end"
        elif kind == "block_begin":
            out.append(" ")
            inside = "block_end"
        elif kind == "comment_begin":
            inside = "comment_end"
    return "".join(out)


def _link_parses(link: str) -> bool:
    lowered = link.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        return False
    try:
        parts = urlsplit(link)
        parts.port  # noqa: B018 - raises ValueError for a port out of range
    except ValueError:
        return False
    host = parts.hostname
    if not host:
        return False
    if ":" in host:  # an IPv6 literal, which urlsplit has already checked
        return True
    return all(_HOST_LABEL.match(label) for label in host.split("."))


def _bad_links(text: str) -> list[str]:
    bad: list[str] = []
    for match in _HTTP_LINK.finditer(text):
        link = match.group(0).rstrip(_TRAILING)
        if not _link_parses(link) and link not in bad:
            bad.append(link)
    return bad


def _lint(
    channel: TemplateChannel, subject: str | None, body: str, me_keys: Collection[str]
) -> tuple[list[LintIssue], dict[Part, _Analysis]]:
    issues: list[LintIssue] = []
    if channel is TemplateChannel.EMAIL and not (subject or "").strip():
        issues.append(
            LintIssue(
                LintRule.MISSING_SUBJECT, Severity.ERROR, Part.SUBJECT, "an email needs a subject"
            )
        )
    analyses: dict[Part, _Analysis] = {}
    if subject is not None:
        analyses[Part.SUBJECT] = _analyse(subject, Part.SUBJECT, me_keys)
    analyses[Part.BODY] = _analyse(body, Part.BODY, me_keys)
    for analysis in analyses.values():
        issues.extend(analysis.issues)

    body_analysis = analyses[Part.BODY]
    if body_analysis.compiled and not PER_CONTACT_FIELDS.intersection(body_analysis.references):
        issues.append(
            LintIssue(
                LintRule.NO_CONTACT_FIELD,
                Severity.ERROR,
                Part.BODY,
                "the body names no per-contact field, so every contact gets the same message; "
                "add one, like {{ first_name }}",
            )
        )
    return issues, analyses


def lint(
    channel: TemplateChannel, subject: str | None, body: str, me_keys: Collection[str]
) -> list[LintIssue]:
    """Save-time lint of a template's text. ``me_keys`` are the ``me.<key>`` names that exist.

    Every issue it returns is an error; :func:`has_errors` of the result is what
    blocks activation.
    """
    issues, _ = _lint(channel, subject, body, me_keys)
    return issues


# --- render -----------------------------------------------------------------


def _missing(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _value_of(values: MergeValues, name: str) -> object:
    if name == PERSONAL_LINE:
        return values.personal_line
    if name == PREVIOUS_SEND_DATE:
        return values.previous_send_date
    if name == "campaign.name":
        return values.campaign_name
    if name == "step.number":
        return values.step_number
    if name.startswith("me."):
        return values.me.get(name.removeprefix("me."))
    return values.contact.get(name)


def _context(values: MergeValues) -> dict[str, object]:
    """The render context. A field with no value is left out, so it is undefined."""
    context: dict[str, object] = {
        name: value
        for name, value in values.contact.items()
        if name in CONTACT_FIELDS and not _missing(value)
    }
    context["me"] = SimpleNamespace(
        **{key: value for key, value in values.me.items() if not _missing(value)}
    )
    if not _missing(values.campaign_name):
        context["campaign"] = SimpleNamespace(name=values.campaign_name)
    if values.step_number is not None:
        context["step"] = SimpleNamespace(number=values.step_number)
    if values.previous_send_date is not None:
        context[PREVIOUS_SEND_DATE] = values.previous_send_date
    if not _missing(values.personal_line):
        context[PERSONAL_LINE] = values.personal_line
    return context


def _one_line(subject: str) -> str:
    """A subject with every line break made a space: a merge value cannot add a header."""
    return _LINE_BREAKS.sub(" ", subject)


def _render_one(env: _Sandbox, source: str, context: Mapping[str, object]) -> str:
    chunks: list[str] = []
    size = 0
    try:
        for chunk in env.from_string(source).generate(context):
            size += len(chunk)
            if size > MAX_OUTPUT_CHARS:
                raise TemplateRenderError(
                    f"the output is longer than {MAX_OUTPUT_CHARS} characters"
                )
            chunks.append(chunk)
    except TemplateRenderError:
        raise
    except SecurityError as exc:
        raise TemplateRenderError(f"refused by the sandbox: {exc}") from exc
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc
    except Exception as exc:  # an operator error in the template, such as "a" + 1
        raise TemplateRenderError(f"{type(exc).__name__}: {exc}") from exc
    return "".join(chunks)


def render(
    channel: TemplateChannel,
    subject: str | None,
    body: str,
    values: MergeValues,
    *,
    today: date,
) -> Rendered:
    """Render a template for one set of merge values.

    Raises :class:`TemplateRenderError` for a template that does not compile or
    that reaches past the sandbox. Everything else is an issue on the result:
    lint's errors, then a :attr:`LintRule.MISSING_VALUE` warning for each named
    field with no value here, then a :attr:`LintRule.BAD_LINK` warning for each
    link that came out broken once values were filled in.
    """
    issues, analyses = _lint(channel, subject, body, values.me.keys())
    for issue in issues:
        if issue.rule in _RENDER_REFUSES:
            raise TemplateRenderError(f"{issue.part.value}: {issue.message}")

    env = _environment(today)  # one budget for the subject and the body together
    context = _context(values)
    rendered_subject = None
    if subject is not None:
        rendered_subject = _one_line(_render_one(env, subject, context))
    rendered_body = _render_one(env, body, context)

    warnings: list[LintIssue] = []
    for part, analysis in analyses.items():
        for name in analysis.references:
            if _missing(_value_of(values, name)):
                warnings.append(
                    LintIssue(
                        LintRule.MISSING_VALUE,
                        Severity.WARNING,
                        part,
                        f"`{name}` has no value here, so it renders empty",
                        name,
                    )
                )
    for part, text in ((Part.SUBJECT, rendered_subject), (Part.BODY, rendered_body)):
        if text is None:
            continue
        for link in _bad_links(text):
            issue = LintIssue(
                LintRule.BAD_LINK,
                Severity.WARNING,
                part,
                f"`{link}` is not a link that parses once filled in",
                link,
            )
            if not any(i.rule is LintRule.BAD_LINK and i.field == link for i in issues):
                warnings.append(issue)
    return Rendered(rendered_subject, rendered_body, (*issues, *warnings))
