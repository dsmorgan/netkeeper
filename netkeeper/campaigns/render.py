"""Template rendering and lint (spec 11.1; item P3-03). Pure: no session, no I/O.

The allowlist
-------------
A message template is text with merge fields in it, so the template language
it gets is the small part of Jinja that needs, and nothing else. Every node of
a parsed template must be on :data:`ALLOWED_NODES`, checked by one walker
(:func:`_walk`) that runs at save time as lint and again before every render:

- text and ``{{ }}`` output; names; literals that are text (at most
  :data:`MAX_LITERAL_CHARS`), whole numbers, true, false and none;
- ``me.<key>``, ``campaign.name`` and ``step.number``, the only attributes;
- the filters in :data:`ALLOWED_FILTERS` and the tests in :data:`ALLOWED_TESTS`,
  with ``truncate``'s length at most :data:`MAX_TRUNCATE_LENGTH`;
- ``{% if %}`` and ``x if y else z``; comparisons, ``in``, ``and``, ``or``,
  ``not``; ``~`` to join text; ``*`` and ``+`` on whole numbers.

Anything else is an error (:attr:`LintRule.UNSUPPORTED`, or
:attr:`LintRule.ATTRIBUTE_ACCESS` and :attr:`LintRule.UNSAFE_ATTRIBUTE` for
the attribute cases) and the render refuses it outright: ``with``, ``for``,
``set``, ``macro``, ``call``, ``filter`` and ``autoescape`` blocks, lists,
tuples, dicts, calls, subscripts, slices, the other operators, every other
filter (``tojson``, ``pprint``, ``attr``...) and the name ``self``. An
allowlist rather than a list of refusals, so a construct nobody thought of is
refused by default.

The sandbox
-----------
What the allowlist lets through renders in
:class:`jinja2.sandbox.ImmutableSandboxedEnvironment`, which bounds it a second
time. A preview renders inside the shared server, and the P3-06 worker will
render on every send.

- Access to an unsafe attribute raises. The stock sandbox prints it as "".
- No globals, and only the allowlisted filters and tests exist.
- ``*`` and ``+`` refuse anything but whole numbers, and a product or sum
  larger than :data:`MAX_INT_BITS` bits.
- Text only grows through ``~`` and filters, and each counts its result
  against one budget of :data:`MAX_OUTPUT_CHARS` per render as the value is
  built; the output stops at the same limit. The compiler is changed to route
  ``~`` through the environment (:class:`_CodeGenerator`), since Jinja joins
  it in a module-level function the sandbox never sees.
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

:func:`merge_fields` lists them with a description each, for the editor's field
list; it is built from the same names lint allows, so the two cannot drift.

A field with no value renders as an empty string and adds a
:attr:`LintRule.MISSING_VALUE` *warning* to the render. It never raises,
whatever an allowed template does with it (:class:`_Missing`): arithmetic
gives no value, a comparison or ``in`` is false, and ``default`` and
``{% if %}`` work as they would for any undefined value.

Lint
----
:func:`lint` runs at save time over the template text alone. Every rule it
applies is an error, and :func:`has_errors` is what blocks activation:

- :attr:`LintRule.SYNTAX`: the template does not parse.
- :attr:`LintRule.UNSUPPORTED`: anything off the allowlist.
- :attr:`LintRule.UNSAFE_ATTRIBUTE`: a ``_``-prefixed attribute.
- :attr:`LintRule.ATTRIBUTE_ACCESS`: an attribute of anything but ``me``,
  ``campaign`` and ``step``. Merge fields are plain values.
- :attr:`LintRule.UNDEFINED_VARIABLE`: a name that is not a merge field.
- :attr:`LintRule.NO_CONTACT_FIELD`: a body that names no per-contact field.
  Identical bulk mail is a spam signal.
- :attr:`LintRule.MISSING_SUBJECT`: an email template with no subject.
- :attr:`LintRule.BAD_LINK`: an ``http``/``https`` link that does not parse.

Each issue about one place carries its one-based ``line`` in the part; an issue
about the whole part (a missing subject, a body with no per-contact field) has
none.

:func:`render` adds the render-time warnings: fields with no value for this
contact, and links that came out broken once merge values were filled in.
"""

from __future__ import annotations

import bisect
import enum
import functools
import operator
import re
import sys
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, Final
from urllib.parse import urlsplit

from jinja2 import ChainableUndefined, TemplateError, TemplateSyntaxError, nodes
from jinja2.compiler import CodeGenerator, Frame
from jinja2.exceptions import FilterArgumentError, SecurityError
from jinja2.filters import FILTERS
from jinja2.runtime import Context, Undefined
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.tests import TESTS

from netkeeper.config import MeSettings, is_me_key
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

NUMBER_FIELDS: Final = frozenset({"connected_year", "years_since_connected", "step.number"})
"""The merge fields that hold whole numbers, the only ones ``*`` and ``+`` accept."""


class FieldGroup(enum.StrEnum):
    """Where a merge field's value comes from."""

    CONTACT = "contact"
    PERSONAL = "personal"
    ME = "me"
    CAMPAIGN = "campaign"


@dataclass(frozen=True, slots=True)
class MergeField:
    """One merge field a template may name, for the editor's field list (#344).

    ``insert`` is what goes between the braces: the name, or for
    ``previous_send_date`` the name with the ``ago`` filter spec 11.1 pairs it with.
    """

    name: str
    group: FieldGroup
    description: str
    insert: str


_FIELD_DESCRIPTIONS: Final[Mapping[str, str]] = {
    "first_name": "The contact's first name, or their preferred name when they have one.",
    "last_name": "The contact's last name.",
    "company": "The contact's current company.",
    "title": "The contact's current job title.",
    "location": "Where the contact is based, as their profile says.",
    "connected_year": "The year you connected with the contact.",
    "years_since_connected": "Whole years since you connected with the contact.",
    "last_position_change": "The latest date the contact started or left a job.",
    PERSONAL_LINE: "A line written for this contact at preview time (spec 12).",
    "me.name": "Your name, from [me] in the config.",
    "me.website": "Your website, from [me] in the config.",
    "me.scheduling_link": "Your scheduling link, from [me] in the config.",
    "me.signature": "Your signature, from [me] in the config.",
    "me.city": "Your city, from [me] in the config.",
    "campaign.name": "The name of the campaign sending the message.",
    "step.number": "Which step of the campaign this message is, counting from 1.",
    PREVIOUS_SEND_DATE: 'When the previous step went to this contact, as words like "last week".',
}

PLACEHOLDER_EXAMPLES: Final[Mapping[str, str]] = {
    "first_name": "Alex",
    "last_name": "Example",
    "company": "Example Co",
    "title": "Product Manager",
    "location": "Springfield",
    "connected_year": "2019",
    "years_since_connected": "6",
    "last_position_change": "2025-03-01",
    PERSONAL_LINE: "Congratulations on the new role at Example Co.",
    "me.name": "Your Name",
    "me.website": "https://example.com",
    "me.scheduling_link": "https://example.com/meet",
    "me.signature": "Your Name",
    "me.city": "Your City",
    "campaign.name": "Example campaign",
    "step.number": "2",
    PREVIOUS_SEND_DATE: "3 weeks ago",
}
"""Invented example values for the editor's field list when no contact is picked. Every one
is made up: none is anyone's real data."""


_GROUPS: Final[Mapping[str, FieldGroup]] = {
    **dict.fromkeys(CONTACT_FIELDS, FieldGroup.CONTACT),
    PERSONAL_LINE: FieldGroup.PERSONAL,
    "me": FieldGroup.ME,
    "campaign": FieldGroup.CAMPAIGN,
    "step": FieldGroup.CAMPAIGN,
    PREVIOUS_SEND_DATE: FieldGroup.CAMPAIGN,
}
"""The group of each scalar field and each namespace. Only says where a field goes in the
editor's list; which fields exist is :data:`SCALAR_FIELDS` and :data:`NAMESPACE_FIELDS`."""

_GROUP_ORDER: Final = tuple(FieldGroup)

# The order spec 11.1 lists fields in, within a group. A field not here sorts after these.
_SPEC_ORDER: Final = (
    *CONTACT_FIELDS,
    PERSONAL_LINE,
    *(f"me.{key}" for key in ME_FIELDS),
    "campaign.name",
    "step.number",
    PREVIOUS_SEND_DATE,
)

# What goes between the braces when it is more than the name (spec 11.1).
_INSERTS: Final[Mapping[str, str]] = {PREVIOUS_SEND_DATE: f"{PREVIOUS_SEND_DATE} | ago"}


def _group(name: str) -> FieldGroup:
    """The group of ``name`` (a scalar field or a namespace); raises for one with none."""
    try:
        return _GROUPS[name]
    except KeyError:
        raise ValueError(f"merge field {name!r} has no group in the editor's field list") from None


def merge_fields(me_keys: Collection[str]) -> tuple[MergeField, ...]:
    """Every merge field a template may name, in the order spec 11.1 lists them.

    Derived from the names the lint walker allows: :data:`SCALAR_FIELDS`, and each
    namespace of :data:`NAMESPACE_FIELDS` with its keys, ``me`` with ``me_keys`` too.
    So a field removed there leaves this list, and one added there must be given a
    group and a description here or this raises. ``me_keys`` are the ``me.<key>``
    names that exist, as for :func:`lint`; one a template cannot name
    (:func:`is_me_key`) is left out.
    """
    found: list[tuple[str, FieldGroup]] = [(name, _group(name)) for name in SCALAR_FIELDS]
    for namespace, keys in NAMESPACE_FIELDS.items():
        group = _group(namespace)
        extra = me_keys if namespace == "me" else ()
        for key in dict.fromkeys((*keys, *extra)):
            if is_me_key(key):
                found.append((f"{namespace}.{key}", group))
    rank = {name: index for index, name in enumerate(_SPEC_ORDER)}
    found.sort(
        key=lambda item: (_GROUP_ORDER.index(item[1]), rank.get(item[0], len(rank)), item[0])
    )
    fields: list[MergeField] = []
    for name, group in found:
        if group is FieldGroup.ME and name not in _FIELD_DESCRIPTIONS:
            key = name.removeprefix("me.")
            description = f"Your value for `{key}`, from [me] in the config."
        else:
            description = _FIELD_DESCRIPTIONS[name]
        fields.append(MergeField(name, group, description, _INSERTS.get(name, name)))
    return tuple(fields)


def placeholder_example(name: str) -> str:
    """An invented example value for the merge field ``name``."""
    if name in PLACEHOLDER_EXAMPLES:
        return PLACEHOLDER_EXAMPLES[name]
    return f"(your {name.removeprefix('me.')})"


MAX_OUTPUT_CHARS: Final = 100_000
"""The most one render may produce, and the budget text-building operations share."""

MAX_INT_BITS: Final = 10_000
"""The largest whole number ``*`` or ``+`` may make, in bits: about 3,000 digits."""

MAX_LITERAL_CHARS: Final = 1_000
"""The longest text literal a template may contain."""

MAX_TRUNCATE_LENGTH: Final = 1_000
"""The largest length ``truncate`` may be given."""

MAX_NESTING: Final = 50
"""The deepest a template's tree may go. ``a * b * c`` nests one level per term, and Python's
own compiler refuses a template nested past about 200 (and gets slow well before that)."""

ALLOWED_NODES: Final[frozenset[type[nodes.Node]]] = frozenset(
    {
        nodes.Template,
        nodes.Output,
        nodes.TemplateData,
        nodes.Name,
        nodes.Const,
        nodes.Getattr,  # on me, campaign and step only
        nodes.Filter,  # ALLOWED_FILTERS only
        nodes.Test,  # ALLOWED_TESTS only
        nodes.Keyword,  # a filter's or test's keyword argument
        nodes.If,
        nodes.CondExpr,
        nodes.Compare,
        nodes.Operand,
        nodes.And,
        nodes.Or,
        nodes.Not,
        nodes.Concat,
        nodes.Mul,  # whole numbers only
        nodes.Add,  # whole numbers only
    }
)
"""Every node type a template may contain. Checked by exact type, never by subclass."""

ALLOWED_FILTERS: Final = frozenset(
    {"default", "upper", "lower", "title", "capitalize", "trim", "truncate", "ago"}
)
ALLOWED_TESTS: Final = frozenset(
    {"defined", "undefined", "none", "number", "string", "even", "odd", "divisibleby"}
)

# How a refused node is named in a lint message. Anything not here goes by its class name.
_NODE_NAMES: Final[Mapping[type[nodes.Node], str]] = {
    nodes.For: "for",
    nodes.With: "with",
    nodes.Macro: "macro",
    nodes.CallBlock: "call",
    nodes.Assign: "set",
    nodes.AssignBlock: "set",
    nodes.FilterBlock: "filter",
    nodes.ScopedEvalContextModifier: "autoescape",
    nodes.EvalContextModifier: "autoescape",
    nodes.Block: "block",
    nodes.Extends: "extends",
    nodes.Include: "include",
    nodes.Import: "import",
    nodes.FromImport: "import",
    nodes.List: "list",
    nodes.Tuple: "tuple",
    nodes.Dict: "dict",
    nodes.Call: "call",
    nodes.Getitem: "subscript",
    nodes.Slice: "slice",
    nodes.Sub: "-",
    nodes.Div: "/",
    nodes.FloorDiv: "//",
    nodes.Mod: "%",
    nodes.Pow: "**",
    nodes.Neg: "unary -",
    nodes.Pos: "unary +",
}
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
    """One finding. ``field`` names the merge field or link it is about, when there is one.

    ``line`` is the one-based line of ``part`` the finding is about, when it is about
    one place: the first place, when the same finding applies to several. A finding
    about the whole part, such as a missing subject, has none.
    """

    rule: LintRule
    severity: Severity
    part: Part
    message: str
    field: str | None = None
    line: int | None = None

    def to_json(self) -> dict[str, str | int | None]:
        return {
            "rule": self.rule.value,
            "severity": self.severity.value,
            "part": self.part.value,
            "message": self.message,
            "field": self.field,
            "line": self.line,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> LintIssue:
        return cls(
            rule=LintRule(data["rule"]),
            severity=Severity(data["severity"]),
            part=Part(data["part"]),
            message=str(data["message"]),
            field=None if data.get("field") is None else str(data["field"]),
            # Lint stored before #344 has no line.
            line=None if data.get("line") is None else int(data["line"]),
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
    """The template cannot be rendered: it does not parse, uses something off the allowlist,
    reached past the sandbox, or went over a size limit."""


# Rules whose templates the render refuses outright, rather than rendering with the error.
_RENDER_REFUSES: Final = frozenset(
    {
        LintRule.SYNTAX,
        LintRule.UNSUPPORTED,
        LintRule.UNSAFE_ATTRIBUTE,
        LintRule.ATTRIBUTE_ACCESS,
    }
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
    lookups; this also survives what an allowed template does with a value.
    Arithmetic gives itself back, ordering comparisons are false, and ``in``
    with it on either side is false (:meth:`_Sandbox.compare`, since ``in``
    asks the right-hand side, which a missing field cannot answer for).
    """

    __slots__ = ()

    def _self(self, *_args: Any, **_kwargs: Any) -> _Missing:
        return self

    def _false(self, _other: Any) -> bool:
        return False

    # jinja2 types each of these on Undefined as raising (``-> Never``); not raising is
    # this class's whole purpose, so each override is a deliberate break with that type.
    __add__ = __radd__ = __mul__ = __rmul__ = _self  # type: ignore[assignment]
    __sub__ = __rsub__ = __truediv__ = __rtruediv__ = _self  # type: ignore[assignment]
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


_ORDERING: Final[Mapping[str, Callable[[Any, Any], Any]]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gteq": operator.ge,
    "lt": operator.lt,
    "lteq": operator.le,
}


class _CodeGenerator(CodeGenerator):
    """Jinja's compiler, with ``~`` and comparisons routed through the environment.

    Stock Jinja compiles ``a ~ b`` to a module-level ``str_join`` and ``a in b`` to
    Python's own ``in``; the sandbox sees neither. Here they call
    :meth:`_Sandbox.join_text`, which counts the text against the budget as it is
    built, and :meth:`_Sandbox.compare`, which lets a missing field answer ``in``.
    """

    def visit_Concat(self, node: nodes.Concat, frame: Frame) -> None:
        self.write("environment.join_text((")
        for arg in node.nodes:
            self.visit(arg, frame)
            self.write(", ")
        self.write("))")

    def visit_Compare(self, node: nodes.Compare, frame: Frame) -> None:
        self.write("environment.compare(")
        self.visit(node.expr, frame)
        for op in node.ops:
            self.write(f", {op.op!r}, ")
            self.visit(op.expr, frame)
        self.write(")")


class _Sandbox(ImmutableSandboxedEnvironment):
    """The sandbox (see the module docstring). One per render: it carries the budget."""

    code_generator_class = _CodeGenerator
    intercepted_binops = frozenset({"*", "+"})

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.budget = MAX_OUTPUT_CHARS

    def spend(self, chars: int, what: str) -> None:
        """Take ``chars`` from this render's budget; refuse once it would go below zero."""
        self.budget -= max(chars, 0)
        if self.budget < 0:
            raise SecurityError(f"{what} would take the output past {MAX_OUTPUT_CHARS} characters")

    def join_text(self, values: tuple[Any, ...]) -> str:
        text = "".join(str(value) for value in values)
        self.spend(len(text), "joining text with ~")
        return text

    def compare(self, left: Any, *rest: Any) -> bool:
        for op, right in zip(rest[::2], rest[1::2], strict=True):
            if op in ("in", "notin"):
                missing = isinstance(left, Undefined) or isinstance(right, Undefined)
                found = False if missing else left in right
                result = found if op == "in" else not found
            else:
                result = bool(_ORDERING[op](left, right))
            if not result:
                return False
            left = right
        return True

    def unsafe_undefined(self, obj: Any, attribute: str) -> Undefined:
        # The stock sandbox returns an undefined value here, which prints as "".
        raise SecurityError(
            f"access to attribute {attribute!r} of {type(obj).__name__!r} object is unsafe"
        )

    def is_safe_attribute(self, obj: Any, attr: str, value: Any) -> bool:
        # Only me, campaign and step have attributes; a missing field answers any.
        return isinstance(obj, SimpleNamespace | Undefined) and super().is_safe_attribute(
            obj, attr, value
        )

    def wrap_str_format(self, value: Any) -> Callable[..., str] | None:
        # Unreachable while only namespaces have attributes; kept so it stays refused.
        if super().wrap_str_format(value) is not None:
            raise SecurityError("str.format is refused")
        return None

    def call_binop(self, context: Context, operator: str, left: Any, right: Any) -> Any:
        for value in (left, right):
            if not isinstance(value, int | Undefined):
                raise SecurityError(
                    f"{operator} is for whole numbers only; join text with ~ instead"
                )
        if isinstance(left, int) and isinstance(right, int):
            bits = left.bit_length() + right.bit_length()
            if bits > MAX_INT_BITS:
                raise SecurityError(f"a number larger than {MAX_INT_BITS} bits is refused")
        return super().call_binop(context, operator, left, right)


def _counted(env: _Sandbox, name: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """``func`` as a filter whose text result is counted against the render's budget."""

    @functools.wraps(func)  # copies jinja_pass_arg, so Jinja still passes what it needs
    def counted(*args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        if isinstance(result, str):
            env.spend(len(result), f"the {name} filter")
        return result

    return counted


def _environment(today: date) -> _Sandbox:
    env = _Sandbox(undefined=_Missing, autoescape=False, keep_trailing_newline=True)
    env.globals.clear()
    stock: dict[str, Callable[..., Any]] = {**FILTERS, "ago": partial(ago, today=today)}
    env.filters = {name: _counted(env, name, stock[name]) for name in ALLOWED_FILTERS}
    env.tests = {name: TESTS[name] for name in ALLOWED_TESTS}
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
    # Each merge field the part names, in order, with the line it is first named on.
    references: dict[str, int | None] = field(default_factory=dict)
    compiled: bool = True

    def error(
        self,
        rule: LintRule,
        part: Part,
        message: str,
        name: str | None = None,
        line: int | None = None,
    ) -> None:
        """Add an error, once: the same finding again, on a later line, keeps the first line."""
        issue = LintIssue(rule, Severity.ERROR, part, message, name, line)
        if not any(_same_finding(issue, seen) for seen in self.issues):
            self.issues.append(issue)

    def refer(self, name: str, line: int | None) -> None:
        self.references.setdefault(name, line)


def _same_finding(a: LintIssue, b: LintIssue) -> bool:
    return (a.rule, a.part, a.message, a.field) == (b.rule, b.part, b.message, b.field)


def _node_name(node: nodes.Node) -> str:
    return _NODE_NAMES.get(type(node), type(node).__name__.lower())


def _is_number(node: nodes.Node) -> bool:
    """True for an operand ``*`` and ``+`` accept: a whole number, as far as lint can tell."""
    if isinstance(node, nodes.Const):
        return isinstance(node.value, int)  # bool is an int too
    if isinstance(node, nodes.Name):
        return node.name in NUMBER_FIELDS
    if isinstance(node, nodes.Getattr):
        return isinstance(node.node, nodes.Name) and f"{node.node.name}.{node.attr}" in (
            NUMBER_FIELDS
        )
    return type(node) in (nodes.Mul, nodes.Add)


class _Walker:
    """Checks one part's tree against the allowlist, and collects the fields it names."""

    def __init__(self, analysis: _Analysis, part: Part, me_keys: Collection[str]) -> None:
        self.analysis = analysis
        self.part = part
        self.allowed: dict[str, Collection[str]] = {
            **NAMESPACE_FIELDS,
            "me": (*ME_FIELDS, *me_keys),
        }

    def refuse(self, node: nodes.Node, what: str, name: str | None) -> None:
        self.analysis.error(
            LintRule.UNSUPPORTED,
            self.part,
            f"line {node.lineno}: {what} is not available in a message template",
            name,
            node.lineno,
        )

    def walk(self, node: nodes.Node, parent: nodes.Node | None, depth: int = 0) -> None:
        kind = type(node)
        if depth > MAX_NESTING:
            self.refuse(node, f"nesting deeper than {MAX_NESTING} levels", "nesting")
            return
        if kind not in ALLOWED_NODES:
            name = _node_name(node)
            label = f"`{name}`" if kind in _NODE_NAMES else f"`{name}` ({kind.__name__})"
            self.refuse(node, label, name)
            return  # its insides are refused with it
        if isinstance(node, nodes.Keyword) and not isinstance(parent, nodes.Filter | nodes.Test):
            self.refuse(node, "a keyword argument", "keyword")
            return
        if isinstance(node, nodes.Name):
            self.name(node)
            return
        if isinstance(node, nodes.Const):
            self.const(node)
            return
        if isinstance(node, nodes.Getattr):
            self.getattr(node)
            return
        if isinstance(node, nodes.Filter | nodes.Test) and not self.filter_or_test(node):
            return
        if isinstance(node, nodes.Mul | nodes.Add):
            for operand in (node.left, node.right):
                if not _is_number(operand):
                    self.refuse(
                        node,
                        f"`{node.operator}` on something other than a whole number "
                        "(join text with ~)",
                        node.operator,
                    )
                    break
        for child in node.iter_child_nodes():
            self.walk(child, node, depth + 1)

    def name(self, node: nodes.Name) -> None:
        name = node.name
        if node.ctx != "load":
            self.refuse(node, f"assigning `{name}`", name)
        elif name == "self":
            self.refuse(node, "`self`", name)
        elif name in SCALAR_FIELDS:
            self.analysis.refer(name, node.lineno)
        elif name in self.allowed:
            self.analysis.error(
                LintRule.UNDEFINED_VARIABLE,
                self.part,
                f"`{name}` is a group of fields; name one, like "
                f"`{name}.{NAMESPACE_FIELDS[name][0]}`",
                name,
                node.lineno,
            )
        else:
            self.analysis.error(
                LintRule.UNDEFINED_VARIABLE,
                self.part,
                f"`{name}` is not a merge field",
                name,
                node.lineno,
            )

    def const(self, node: nodes.Const) -> None:
        value = node.value
        if isinstance(value, str):
            if len(value) > MAX_LITERAL_CHARS:
                self.refuse(node, f"text longer than {MAX_LITERAL_CHARS} characters", "text")
        elif isinstance(value, int):
            if value.bit_length() > MAX_INT_BITS:
                self.refuse(node, f"a number larger than {MAX_INT_BITS} bits", "number")
        elif value is not None:
            self.refuse(node, f"a {type(value).__name__} value", type(value).__name__)

    def getattr(self, node: nodes.Getattr) -> None:
        key = node.attr
        base = node.node
        if key.startswith("_"):
            self.analysis.error(
                LintRule.UNSAFE_ATTRIBUTE,
                self.part,
                f"`{key}`: names starting with _ are refused",
                key,
                node.lineno,
            )
        elif not isinstance(base, nodes.Name) or base.name not in self.allowed:
            dotted = f"{base.name}.{key}" if isinstance(base, nodes.Name) else key
            self.analysis.error(
                LintRule.ATTRIBUTE_ACCESS,
                self.part,
                f"`{dotted}`: merge fields are plain values, with no attributes or methods",
                dotted,
                node.lineno,
            )
        elif key not in self.allowed[base.name]:
            dotted = f"{base.name}.{key}"
            self.analysis.error(
                LintRule.UNDEFINED_VARIABLE,
                self.part,
                f"`{dotted}` is not a merge field",
                dotted,
                node.lineno,
            )
        else:
            self.analysis.refer(f"{base.name}.{key}", node.lineno)

    def filter_or_test(self, node: nodes.Filter | nodes.Test) -> bool:
        """False when ``node`` is refused, so the walk does not go into it."""
        is_filter = isinstance(node, nodes.Filter)
        allowed = ALLOWED_FILTERS if is_filter else ALLOWED_TESTS
        what = "filter" if is_filter else "test"
        if node.name not in allowed:
            self.refuse(node, f"the `{node.name}` {what}", node.name)
            return False
        if node.dyn_args is not None or node.dyn_kwargs is not None:
            self.refuse(node, f"`*args` or `**kwargs` in the `{node.name}` {what}", node.name)
            return False
        if node.name == "truncate":
            length = node.args[0] if node.args else None
            for keyword in node.kwargs:
                if keyword.key == "length":
                    length = keyword.value
            if length is not None and not (
                isinstance(length, nodes.Const)
                and isinstance(length.value, int)
                and length.value <= MAX_TRUNCATE_LENGTH
            ):
                self.refuse(
                    node,
                    f"a `truncate` length that is not a number up to {MAX_TRUNCATE_LENGTH}",
                    "truncate",
                )
                return False
        return True


def refused_nodes(source: str) -> Iterator[str]:
    """The name of every node in ``source`` the allowlist refuses; for tests and tooling."""
    for issue in lint(TemplateChannel.LINKEDIN, None, source, ()):
        if issue.rule is LintRule.UNSUPPORTED and issue.field is not None:
            yield issue.field


def _analyse(source: str, part: Part, me_keys: Collection[str]) -> _Analysis:
    analysis = _Analysis()
    env = _environment(date.min)
    try:
        tree = env.parse(source)
    except TemplateSyntaxError as exc:
        analysis.error(LintRule.SYNTAX, part, f"line {exc.lineno}: {exc.message}", line=exc.lineno)
        analysis.compiled = False
        return analysis
    except RecursionError:
        analysis.error(LintRule.SYNTAX, part, "the template nests too deeply to read")
        analysis.compiled = False
        return analysis
    except ValueError as exc:  # a number literal past Python's int conversion limit
        analysis.error(LintRule.SYNTAX, part, f"the template does not parse: {exc}")
        analysis.compiled = False
        return analysis
    _Walker(analysis, part, me_keys).walk(tree, None)
    if not has_errors(analysis.issues):
        # The allowlist and the nesting limit should leave nothing for this to find.
        try:
            env.compile(source)
        except TemplateSyntaxError as exc:
            analysis.error(
                LintRule.SYNTAX, part, f"line {exc.lineno}: {exc.message}", line=exc.lineno
            )
        except (SyntaxError, RecursionError) as exc:
            analysis.error(LintRule.SYNTAX, part, f"the template does not compile: {exc}")
    text = _TextWithPlaceholders.of(env, source)
    for link, offset in _bad_links(text.text):
        analysis.error(
            LintRule.BAD_LINK,
            part,
            f"`{link}` is not a link that parses",
            link,
            text.line_at(offset),
        )
    return analysis


@dataclass(frozen=True, slots=True)
class _TextWithPlaceholders:
    """A template's text with each ``{{ }}`` replaced by a placeholder and each tag by a
    space, and where each piece of it came from, so a link found in it has a line."""

    text: str
    # (offset in ``text``, line in the source) where each piece starts, in order.
    starts: tuple[tuple[int, int], ...]

    @classmethod
    def of(cls, env: _Sandbox, source: str) -> _TextWithPlaceholders:
        out: list[str] = []
        starts: list[tuple[int, int]] = []
        size = 0
        inside: str | None = None
        for lineno, kind, value in env.lex(source):
            if inside is not None:
                if kind == inside:
                    inside = None
                continue
            piece: str | None = None
            if kind == "data":
                piece = value
            elif kind == "variable_begin":
                piece = _PLACEHOLDER
                inside = "variable_end"
            elif kind == "block_begin":
                piece = " "
                inside = "block_end"
            elif kind == "comment_begin":
                inside = "comment_end"
            if piece:
                starts.append((size, lineno))
                out.append(piece)
                size += len(piece)
        return cls("".join(out), tuple(starts))

    def line_at(self, offset: int) -> int | None:
        """The source line the character at ``offset`` of :attr:`text` came from."""
        index = bisect.bisect_right(self.starts, (offset, sys.maxsize)) - 1
        if index < 0:
            return None
        start, lineno = self.starts[index]
        return lineno + self.text.count("\n", start, offset)


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


def _bad_links(text: str) -> list[tuple[str, int]]:
    """Each link in ``text`` that does not parse, once, with the offset it first appears at."""
    bad: dict[str, int] = {}
    for match in _HTTP_LINK.finditer(text):
        link = match.group(0).rstrip(_TRAILING)
        if not _link_parses(link) and link not in bad:
            bad[link] = match.start()
    return list(bad.items())


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


def fields_used(channel: TemplateChannel, subject: str | None, body: str) -> frozenset[str]:
    """The merge fields a template names, in its subject or its body.

    A part that does not parse names nothing here; lint reports it as an error.
    """
    _, analyses = _lint(channel, subject, body, ())
    return frozenset(name for analysis in analyses.values() for name in analysis.references)


def uses_personal_line(channel: TemplateChannel, subject: str | None, body: str) -> bool:
    """Whether a template names ``{{ personal_line }}``: its messages differ one by one,
    so the review gate approves each of them, never the step as a whole (#339)."""
    return PERSONAL_LINE in fields_used(channel, subject, body)


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
        for name, line in analysis.references.items():
            if _missing(_value_of(values, name)):
                warnings.append(
                    LintIssue(
                        LintRule.MISSING_VALUE,
                        Severity.WARNING,
                        part,
                        f"`{name}` has no value here, so it renders empty",
                        name,
                        line,
                    )
                )
    for part, text in ((Part.SUBJECT, rendered_subject), (Part.BODY, rendered_body)):
        if text is None:
            continue
        # A line of the rendered text is not a line of the template, so these have none.
        for link, _offset in _bad_links(text):
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
