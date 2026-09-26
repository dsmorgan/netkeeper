"""Template rendering and lint (spec 11.1; item P3-03). Pure: no session, no I/O.

The sandbox
-----------
Every template renders in :class:`jinja2.sandbox.ImmutableSandboxedEnvironment`,
with three changes:

- Access to an unsafe attribute *raises*. The stock sandbox returns an
  undefined value that prints as an empty string, so ``{{ ''.__class__ }}``
  would render as nothing and look harmless. Here it fails the render
  (:class:`TemplateRenderError`), and lint refuses any ``_``-prefixed name
  before a template is ever rendered (:attr:`LintRule.UNSAFE_ATTRIBUTE`).
- No globals. ``range``, ``cycler``, ``joiner``, ``namespace``, ``dict`` and
  ``lipsum`` are gone, so the only names a template can reach are the merge
  fields below; anything else is :attr:`LintRule.UNDEFINED_VARIABLE`.
- Merge values are data, never template source. A contact whose name is
  ``{{ me.name }}`` (imported data is not trusted) renders that text literally.

Autoescape is off: every template is plain text today (email and LinkedIn).
Spec 11.1 turns it on for HTML email, which netkeeper does not build yet.

Merge fields (spec 11.1)
------------------------
- Contact: :data:`CONTACT_FIELDS`. ``first_name`` resolves to the contact's
  preferred name.
- You: ``me.<key>`` for :data:`ME_FIELDS` and any extra key under ``[me]``.
- Campaign: ``campaign.name``, ``step.number``, ``previous_send_date``.
- ``personal_line`` (spec 12), written per contact at preview time.

A field with no value renders as an empty string and adds a
:attr:`LintRule.MISSING_VALUE` *warning* to the render; it never raises. So
``{{ company | default("your team") }}`` and ``{% if company %}`` work as
they would for any undefined value.

Lint
----
:func:`lint` runs at save time over the template text alone. Every rule it
applies is an error, and :func:`has_errors` is what blocks activation:

- :attr:`LintRule.SYNTAX`: the template does not compile (this includes an
  unknown filter), or it extends, includes, or imports another template.
- :attr:`LintRule.UNSAFE_ATTRIBUTE`: a ``_``-prefixed attribute or key.
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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, Final
from urllib.parse import urlsplit

from jinja2 import ChainableUndefined, TemplateError, TemplateSyntaxError, meta, nodes
from jinja2.exceptions import FilterArgumentError, SecurityError
from jinja2.runtime import Undefined
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
    UNSAFE_ATTRIBUTE = "unsafe_attribute"
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
    """The template cannot be rendered: it does not compile, or it reached past the sandbox."""


def me_fields(me: MeSettings) -> dict[str, str]:
    """The ``me.<key>`` values from ``[me]``: :data:`ME_FIELDS`, then any extra keys."""
    values = {name: str(getattr(me, name)) for name in ME_FIELDS}
    values.update(me.extra)
    return values


# --- the environment ----------------------------------------------------------


class _Sandbox(ImmutableSandboxedEnvironment):
    def unsafe_undefined(self, obj: Any, attribute: str) -> Undefined:
        # The stock sandbox returns an undefined value here, which prints as "".
        raise SecurityError(
            f"access to attribute {attribute!r} of {type(obj).__name__!r} object is unsafe"
        )


def _environment(today: date) -> _Sandbox:
    env = _Sandbox(undefined=ChainableUndefined, autoescape=False, keep_trailing_newline=True)
    env.globals.clear()
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
    # These compile, then fail at render: there is no loader, and no other template to reach.
    for reach in tree.find_all((nodes.Extends, nodes.Include, nodes.Import, nodes.FromImport)):
        analysis.error(
            LintRule.SYNTAX,
            part,
            f"line {reach.lineno}: a template cannot extend, include, or import another",
        )

    lookups = [
        node
        for node in tree.find_all((nodes.Getattr, nodes.Getitem))
        if isinstance(node, nodes.Getattr | nodes.Getitem)
    ]
    # ``x | attr("__class__")`` is a lookup too; the sandbox refuses it at render.
    attr_names = [
        node.args[0].value
        for node in tree.find_all(nodes.Filter)
        if isinstance(node, nodes.Filter)
        and node.name == "attr"
        and node.args
        and isinstance(node.args[0], nodes.Const)
        and isinstance(node.args[0].value, str)
    ]
    for key in [_static_key(lookup) for lookup in lookups] + attr_names:
        if key is not None and key.startswith("_"):
            analysis.error(
                LintRule.UNSAFE_ATTRIBUTE, part, f"`{key}`: names starting with _ are refused", key
            )

    undeclared = meta.find_undeclared_variables(tree)
    looked_into = {id(lookup.node) for lookup in lookups}
    allowed: dict[str, Collection[str]] = {**NAMESPACE_FIELDS, "me": (*ME_FIELDS, *me_keys)}

    for node in tree.find_all(nodes.Name):
        name = node.name
        if name not in undeclared:
            continue  # set or looped over inside the template
        if name in SCALAR_FIELDS:
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
        if not isinstance(base, nodes.Name) or base.name not in undeclared:
            continue
        if base.name not in allowed:
            continue  # reported above, as the name itself
        key = _static_key(lookup)
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


def _render_one(env: _Sandbox, source: str, context: Mapping[str, object]) -> str:
    try:
        return env.from_string(source).render(context)
    except SecurityError as exc:
        raise TemplateRenderError(f"refused by the sandbox: {exc}") from exc
    except TemplateError as exc:
        raise TemplateRenderError(str(exc)) from exc
    except Exception as exc:  # an operator error in the template, such as "a" + 1
        raise TemplateRenderError(f"{type(exc).__name__}: {exc}") from exc


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
        if issue.rule in (LintRule.SYNTAX, LintRule.UNSAFE_ATTRIBUTE):
            raise TemplateRenderError(f"{issue.part.value}: {issue.message}")

    env = _environment(today)
    context = _context(values)
    rendered_subject = None if subject is None else _render_one(env, subject, context)
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
