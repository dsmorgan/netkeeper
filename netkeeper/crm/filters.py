"""The filter language (spec 10.4): a JSON tree of predicates over contacts, compiled to SQLAlchemy.

A :class:`FilterTree` is what a smart list stores, what the Contacts page sends
with each request, what a campaign uses as its audience, and what an export
respects (P1-05, P1-08, P1-11, P3-04). It is Pydantic models, so one definition
validates JSON from the UI, serializes back unchanged, and publishes its JSON
schema for the filter builder (P1-15): ``tests/snapshots/filter_schema.json``
is that schema, and ``tests/test_filters.py`` fails when it drifts.

Shape
-----
The root carries the predicate under ``where`` (``null`` means every contact)
and one option, ``include_archived``. A predicate is an object whose ``op``
says what it is:

- Logic: ``and`` and ``or`` over ``children``, ``not`` over ``child``.
- Comparisons on scalar contact columns from an allowlist (:data:`FIELDS`):
  ``eq``, ``neq``, ``contains``, ``starts_with``, ``is_empty``, ``gt``, ``gte``,
  ``lt``, ``lte``, ``between``. Which of them a field takes depends on its kind.
- Children: ``has_email`` (optionally by ``status``), ``has_phone``,
  ``has_li_url``, ``has_position``, ``email_contains``.
- Relative time: ``last_contacted`` (``within_days``, ``older_than_days``, or
  ``never``), ``connected_within_days``, ``changed_jobs_within_days``.
- Tags (spec 8.3, 10.3): ``tag_any``, ``tag_all``, ``tag_none`` over tag
  ``names``, matched without regard to case; a name the user has no tag for
  matches no contact.
- Lists (spec 10.4): ``list_member`` over a ``list_id``. See "Lists" below.
- Placeholders for tables that do not exist yet: ``enrolled_in`` and
  ``replied_in`` (P3-04). They parse, so the builder's schema is complete, and
  compiling one raises :class:`UnsupportedPredicate` naming the item that
  delivers it.

Semantics
---------
- Every leaf is two-valued. A NULL column never makes a predicate unknown, so
  ``not`` is the exact complement of its child and ``neq`` is ``not eq``.
  ``is_empty`` is NULL or the empty string.
- String comparisons are case-insensitive: ``lower()`` on both sides, which is
  ASCII folding on SQLite. ``contains`` and ``starts_with`` escape ``%`` and
  ``_`` in the value, so the value matches literally.
- Values travel as JSON scalars: strings, integers, booleans. A date is an ISO
  ``YYYY-MM-DD`` string. A datetime is an ISO 8601 string with an offset; a naive
  one is rejected, as everywhere in the schema.
- Relative windows count back from ``now`` (aware UTC; injectable). A window on
  a date column (``connected_on``) counts back from today in the user's timezone.
  ``within_days`` includes the cutoff instant; ``older_than_days`` excludes it.
- Merged-away contacts (``merged_into_id`` set) never appear. Archived contacts
  appear only with ``include_archived``.
- Every subquery on a child table (and on ``contact_tags`` and ``tags``) also
  constrains ``user_id``, and the compiled statements are built on
  :func:`netkeeper.scoping.scoped`, so they pass the scope guard.

Lists
-----
``list_member`` selects the members :func:`netkeeper.crm.lists.list_members`
would return for that list, and then the tree it sits in applies its own rules
to them, as it does to every predicate. For every list whose own membership
excludes archived contacts — every static list, and every smart list at the
default — the predicate and the list page agree outright, and
``tests/test_filters.py`` pins them together. The exception is a smart list
that sets ``include_archived`` itself: it holds people the default tree
excludes, so its page can show someone an export of it does not, until the
filter asks for archived contacts too. That one is not fixable in this
direction — a predicate cannot put back a contact the tree around it has
already excluded — so it is stated rather than claimed away.

- A **static** list is an ``EXISTS`` over ``list_members`` for the id, plus the
  liveness ``netkeeper.crm.lists`` applies to a static list's own page: an
  archived or merged-away contact is not a member, whatever the outer tree's
  ``include_archived`` says, because the ``list_members`` row can outlive the
  contact it names being archived or merged away.
- A **smart** list is its stored tree, inlined here at compile time. That tree
  carries its own ``include_archived``, and it decides membership of *that*
  list; the outer tree still applies its own on top, so the result is the
  intersection.

Inlining is why the compiler takes a ``session``: a smart list's tree lives in
the ``lists`` table, and a predicate over lists cannot be resolved from the
node alone. Nothing else here reads the database, so a tree with no
``list_member`` in it compiles without a single query. What a compile does
cost is one indexed row lookup per *distinct* list id, cached for the compile
— never one per contact. A caller compiling many trees at once (the lists
page; the guard that recompiles every list after a write) hands
:func:`compile_where` the user's lists under ``all_lists`` and pays one query
for the lot: without that, a page over a long chain of lists is quadratic in
the number of lists, which is the shape this whole design exists to avoid.

Two lists can name each other, so :class:`_Compiler` carries the ids it is
already standing in for and raises :class:`ListReferenceError` on a reference
back to one, rather than recursing until Python stops it.
:func:`netkeeper.crm.lists.update_list` seeds that set with the list being
saved, so a cycle is refused at the write that would close it rather than
found later by whoever opens the list. The same error caps how many lists one
filter may pull in (:data:`MAX_LIST_EXPANSIONS`): a chain of lists each naming
the one below it twice would otherwise inline exponentially.

A ``list_member`` naming a list this user does not have (deleted, or someone
else's) matches no contact, the way a tag name they have never used does. It
is not an error: a list deleted out from under a saved filter would otherwise
turn every page that reads that filter into a 422. That is a rule for
*compiling*, which is to say for reads. Storing such a reference is refused
(:func:`netkeeper.crm.lists.check_list_references`), because the id a list is
about to be given does not exist yet either, and a filter allowed to name it
would be handed that very row — which is how two ordinary writes used to close
a cycle. A reference can still come to name a *different* list than it did,
on a backend that hands a deleted id back (SQLite does, a sequence does not);
``netkeeper.crm.lists`` refuses the write when that breaks a list and logs the
lists that lose theirs when one is deleted.

Errors
------
:func:`parse_filter` and :func:`parse_sort` raise :class:`FilterError`. Its
``issues`` carry a JSON path to each offending node (``where.children[1].value``)
and its message lists them, ready to show in the UI. :class:`UnsupportedPredicate`
and :class:`ListReferenceError` are ``FilterError`` too, with the path of the
node at fault; a path inside an inlined list's own tree is prefixed
``list[7].where`` to say whose node it is, since it is not in the caller's
document at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Final, Literal, Self, assert_never, get_args
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    StrictBool,
    StrictInt,
    StrictStr,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic_core import ErrorDetails
from sqlalchemy import ColumnElement, Select, Update, and_, false, func, not_, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from netkeeper.models import (
    Contact,
    ContactEmail,
    ContactList,
    ContactMet,
    ContactPhone,
    ContactPosition,
    ContactSnapshot,
    ContactSource,
    ContactTag,
    EmailStatus,
    ListKind,
    Tag,
    User,
    tag_name_key,
)

# ``ListMember`` below is the predicate; the table it reads needs the other name.
from netkeeper.models import ListMember as ListMemberRow
from netkeeper.models.base import utcnow
from netkeeper.scoping import get_scoped, scoped, scoped_count, scoped_update

log = logging.getLogger(__name__)

# --- the field allowlist ----------------------------------------------------

# One flat Literal per set, so the JSON schema shows one enum and a wrong name
# produces one error. ``FIELDS`` below is derived from the kind tuples; a test
# checks the two spellings agree.
StringField = Literal[
    "first_name",
    "last_name",
    "preferred_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "li_public_id",
]
AnyField = Literal[
    "first_name",
    "last_name",
    "preferred_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "li_public_id",
    "met",
    "source",
    "degree",
    "connected_on",
    "last_contacted_at",
    "last_enriched_at",
    "triaged_at",
    "li_disconnected_at",
    "archived_at",
    "created_at",
    "updated_at",
    "do_not_contact",
]
OrderedField = Literal[
    "degree",
    "connected_on",
    "last_contacted_at",
    "last_enriched_at",
    "triaged_at",
    "li_disconnected_at",
    "archived_at",
    "created_at",
    "updated_at",
]
EmptyableField = Literal[
    "first_name",
    "last_name",
    "preferred_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "li_public_id",
    "connected_on",
    "last_contacted_at",
    "last_enriched_at",
    "triaged_at",
    "li_disconnected_at",
    "archived_at",
    "created_at",
    "updated_at",
]

FieldKind = Literal["string", "enum", "int", "date", "datetime", "bool"]

_STRING_FIELDS: Final[tuple[str, ...]] = get_args(StringField)
_ENUM_FIELDS: Final[dict[str, type[ContactMet] | type[ContactSource]]] = {
    "met": ContactMet,
    "source": ContactSource,
}
_INT_FIELDS: Final[tuple[str, ...]] = ("degree",)
_DATE_FIELDS: Final[tuple[str, ...]] = ("connected_on",)
_DATETIME_FIELDS: Final[tuple[str, ...]] = (
    "last_contacted_at",
    "last_enriched_at",
    "triaged_at",
    "li_disconnected_at",
    "archived_at",
    "created_at",
    "updated_at",
)
_BOOL_FIELDS: Final[tuple[str, ...]] = ("do_not_contact",)

_OPS_BY_KIND: Final[dict[str, tuple[str, ...]]] = {
    "string": ("eq", "neq", "contains", "starts_with", "is_empty"),
    "enum": ("eq", "neq"),
    "int": ("eq", "neq", "gt", "gte", "lt", "lte", "between"),
    "date": ("eq", "neq", "is_empty", "gt", "gte", "lt", "lte", "between"),
    "datetime": ("eq", "neq", "is_empty", "gt", "gte", "lt", "lte", "between"),
    "bool": ("eq", "neq"),
}

_LABELS: Final[dict[str, str]] = {
    "first_name": "first name",
    "last_name": "last name",
    "preferred_name": "preferred name",
    "headline": "headline",
    "current_title": "title",
    "current_company": "company",
    "location": "location",
    "li_public_id": "LinkedIn id",
    "met": "met",
    "source": "source",
    "degree": "degree",
    "connected_on": "connected on",
    "last_contacted_at": "last contacted",
    "last_enriched_at": "last enriched",
    "triaged_at": "triaged",
    "li_disconnected_at": "disconnected on LinkedIn",
    "archived_at": "archived",
    "created_at": "created",
    "updated_at": "updated",
    "do_not_contact": "do not contact",
}


@dataclass(frozen=True)
class FieldSpec:
    """What the builder needs to know about one filterable column."""

    name: str
    kind: FieldKind
    label: str
    ops: tuple[str, ...]
    values: tuple[str, ...] = ()
    """The allowed values of an ``enum`` field, in declaration order."""


def _field_specs() -> dict[str, FieldSpec]:
    specs: dict[str, FieldSpec] = {}
    kinds: list[tuple[FieldKind, tuple[str, ...]]] = [
        ("string", _STRING_FIELDS),
        ("enum", tuple(_ENUM_FIELDS)),
        ("int", _INT_FIELDS),
        ("date", _DATE_FIELDS),
        ("datetime", _DATETIME_FIELDS),
        ("bool", _BOOL_FIELDS),
    ]
    for kind, names in kinds:
        for name in names:
            values = tuple(m.value for m in _ENUM_FIELDS[name]) if kind == "enum" else ()
            specs[name] = FieldSpec(name, kind, _LABELS[name], _OPS_BY_KIND[kind], values)
    return specs


FIELDS: Final[dict[str, FieldSpec]] = _field_specs()
"""Every filterable and sortable column of ``contacts``, by name."""

_COLUMNS: Final[dict[str, InstrumentedAttribute[Any]]] = {
    name: getattr(Contact, name) for name in FIELDS
}


def _json_scalar(value: Any) -> str | int | bool:
    # bool is an int subclass, so it passes; float and everything else is out.
    if isinstance(value, str | int) and not isinstance(value, float):
        return value
    raise ValueError("expects a string, integer, or boolean")


JsonScalar = Annotated[
    str | int | bool,
    PlainValidator(_json_scalar),
    WithJsonSchema({"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "boolean"}]}),
]
"""A comparison value as JSON carries it. Its kind is checked against the field."""

Days = Annotated[StrictInt, Field(ge=0)]
type TypedValue = str | int | bool | date | datetime


def typed_value(field: str, value: str | int | bool) -> TypedValue:
    """``value`` as the Python type of ``field``, or :class:`ValueError` saying what it expects.

    Dates and datetimes arrive as strings; a datetime must carry an offset.
    """
    spec = FIELDS[field]
    match spec.kind:
        case "string":
            if isinstance(value, str):
                return value
            raise ValueError(f"{field} expects a string")
        case "enum":
            if isinstance(value, str) and value in spec.values:
                return value
            raise ValueError(f"{field} expects one of: {', '.join(spec.values)}")
        case "int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            raise ValueError(f"{field} expects an integer")
        case "bool":
            if isinstance(value, bool):
                return value
            raise ValueError(f"{field} expects true or false")
        case "date":
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value)
                except ValueError:
                    pass
            raise ValueError(f"{field} expects an ISO date (YYYY-MM-DD)")
        case "datetime":
            if isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value)
                except ValueError:
                    parsed = None
                if parsed is not None and parsed.tzinfo is not None:
                    return parsed
            raise ValueError(
                f"{field} expects an ISO 8601 datetime with a timezone offset "
                "(2026-09-20T12:00:00Z)"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _in_order(low: TypedValue, high: TypedValue) -> bool:
    """``low <= high`` for the ordered kinds; both come from one field, so they share a type."""
    if isinstance(low, bool) or isinstance(high, bool):
        return True
    if isinstance(low, int) and isinstance(high, int):
        return low <= high
    if isinstance(low, datetime) and isinstance(high, datetime):
        return low <= high
    if isinstance(low, date) and isinstance(high, date):
        return low <= high
    return True


def _check_value(value: str | int | bool, info: ValidationInfo) -> str | int | bool:
    """Field validator body: the value must fit the field's kind. Returns the raw value."""
    field = info.data.get("field")
    if isinstance(field, str):  # absent when ``field`` itself failed validation
        typed_value(field, value)
    return value


# --- the tree ---------------------------------------------------------------


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")


class And(_Node):
    op: Literal["and"]
    children: list[FilterNode] = Field(min_length=1)


class Or(_Node):
    op: Literal["or"]
    children: list[FilterNode] = Field(min_length=1)


class Not(_Node):
    op: Literal["not"]
    child: FilterNode


class Eq(_Node):
    op: Literal["eq"]
    field: AnyField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Neq(_Node):
    op: Literal["neq"]
    field: AnyField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Contains(_Node):
    """Case-insensitive substring; ``%`` and ``_`` in ``value`` match themselves."""

    op: Literal["contains"]
    field: StringField
    value: Annotated[StrictStr, Field(min_length=1)]


class StartsWith(_Node):
    op: Literal["starts_with"]
    field: StringField
    value: Annotated[StrictStr, Field(min_length=1)]


class IsEmpty(_Node):
    """NULL, or the empty string for a string column."""

    op: Literal["is_empty"]
    field: EmptyableField


class Gt(_Node):
    op: Literal["gt"]
    field: OrderedField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Gte(_Node):
    op: Literal["gte"]
    field: OrderedField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Lt(_Node):
    op: Literal["lt"]
    field: OrderedField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Lte(_Node):
    op: Literal["lte"]
    field: OrderedField
    value: JsonScalar

    @field_validator("value")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)


class Between(_Node):
    """``low <= field <= high``, both ends included."""

    op: Literal["between"]
    field: OrderedField
    low: JsonScalar
    high: JsonScalar

    @field_validator("low", "high")
    @classmethod
    def _fits_field(cls, value: str | int | bool, info: ValidationInfo) -> str | int | bool:
        return _check_value(value, info)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not _in_order(typed_value(self.field, self.low), typed_value(self.field, self.high)):
            raise ValueError("high is below low")
        return self


class HasEmail(_Node):
    """At least one email row, optionally with the given ``status``."""

    op: Literal["has_email"]
    status: EmailStatus | None = None


class HasPhone(_Node):
    op: Literal["has_phone"]


class HasLiUrl(_Node):
    op: Literal["has_li_url"]


class HasPosition(_Node):
    op: Literal["has_position"]


class EmailContains(_Node):
    """Any of the contact's emails contains ``value`` (case-insensitive, literal)."""

    op: Literal["email_contains"]
    value: Annotated[StrictStr, Field(min_length=1)]


class LastContacted(_Node):
    """Exactly one of ``within_days``, ``older_than_days``, ``never``.

    ``older_than_days`` needs a value: never-contacted people do not count as
    contacted long ago. Combine with ``never`` under ``or`` for both.
    """

    op: Literal["last_contacted"]
    within_days: Days | None = None
    older_than_days: Days | None = None
    never: StrictBool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        chosen = (self.within_days is not None) + (self.older_than_days is not None) + self.never
        if chosen != 1:
            raise ValueError("set exactly one of within_days, older_than_days, never")
        return self


class ConnectedWithinDays(_Node):
    """``connected_on`` on or after today (in the user's timezone) minus ``days``."""

    op: Literal["connected_within_days"]
    days: Days


class ChangedJobsWithinDays(_Node):
    """A ``contact_snapshots`` row observed in the last ``days`` days (spec 9.8)."""

    op: Literal["changed_jobs_within_days"]
    days: Days


class TagAny(_Node):
    """Carries at least one of the tags named (case-insensitive), whatever its source."""

    op: Literal["tag_any"]
    names: list[StrictStr] = Field(min_length=1)


class TagAll(_Node):
    """Carries every tag named (case-insensitive)."""

    op: Literal["tag_all"]
    names: list[StrictStr] = Field(min_length=1)


class TagNone(_Node):
    """Carries none of the tags named (case-insensitive)."""

    op: Literal["tag_none"]
    names: list[StrictStr] = Field(min_length=1)


class ListMember(_Node):
    op: Literal["list_member"]
    list_id: StrictInt


class EnrolledIn(_Node):
    op: Literal["enrolled_in"]
    campaign_id: StrictInt


class RepliedIn(_Node):
    op: Literal["replied_in"]
    campaign_id: StrictInt


type FilterNode = Annotated[
    And
    | Or
    | Not
    | Eq
    | Neq
    | Contains
    | StartsWith
    | IsEmpty
    | Gt
    | Gte
    | Lt
    | Lte
    | Between
    | HasEmail
    | HasPhone
    | HasLiUrl
    | HasPosition
    | EmailContains
    | LastContacted
    | ConnectedWithinDays
    | ChangedJobsWithinDays
    | TagAny
    | TagAll
    | TagNone
    | ListMember
    | EnrolledIn
    | RepliedIn,
    Field(discriminator="op"),
]

PLACEHOLDERS: Final[dict[str, str]] = {
    "enrolled_in": "P3-04",
    "replied_in": "P3-04",
}
"""Predicates that parse but do not compile yet, and the item that delivers each.

``list_member`` was here until P1-27 (#73), naming P1-08 — which shipped lists
without it, so the message named an item that had already been and gone. It
compiles now; a predicate leaves this table by compiling, never by being
re-pointed at the next item."""

NODE_TYPES: Final[tuple[type[_Node], ...]] = get_args(get_args(FilterNode.__value__)[0])
"""Every predicate model, in the order the union lists them."""

OPS: Final[tuple[str, ...]] = tuple(
    get_args(member.model_fields["op"].annotation)[0] for member in NODE_TYPES
)
"""Every ``op`` value, in the same order."""


def _fields_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-netkeeper-fields"] = {
        name: {
            "kind": spec.kind,
            "label": spec.label,
            "ops": list(spec.ops),
            **({"values": list(spec.values)} if spec.values else {}),
        }
        for name, spec in FIELDS.items()
    }


class FilterTree(_Node):
    """The root: a predicate (or none) and the archived-contacts switch.

    The JSON schema carries ``x-netkeeper-fields``: each field's kind, label,
    the ops it takes, and the values of an enum, for the builder.
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra=_fields_schema_extra)

    where: FilterNode | None = None
    include_archived: StrictBool = False


And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()
FilterTree.model_rebuild()

Direction = Literal["asc", "desc"]


class SortKey(_Node):
    """One ``ORDER BY`` term. Strings sort case-insensitively; empty values sort last."""

    field: AnyField
    direction: Direction = "asc"


_SORT_ADAPTER: Final[TypeAdapter[list[SortKey]]] = TypeAdapter(list[SortKey])


# --- errors -----------------------------------------------------------------


@dataclass(frozen=True)
class FilterIssue:
    """One problem: where in the JSON it is and what is wrong."""

    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


class FilterError(ValueError):
    """An invalid filter or sort. ``issues`` say where; the message lists them."""

    def __init__(self, issues: Sequence[FilterIssue]) -> None:
        self.issues: tuple[FilterIssue, ...] = tuple(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues) or "invalid filter")

    @classmethod
    def from_validation(cls, exc: ValidationError) -> FilterError:
        return cls([FilterIssue(_path(err["loc"]), _message(err)) for err in exc.errors()])


class UnsupportedPredicate(FilterError):
    """A placeholder predicate was compiled; ``item`` is the work item that delivers it."""

    def __init__(self, op: str, path: str, item: str) -> None:
        self.op = op
        self.item = item
        super().__init__([FilterIssue(path, f"{op} is not available yet; {item} delivers it")])


class ListReferenceError(FilterError):
    """A ``list_member`` the compiler will not take: a cycle, too many lists, or no such list.

    The last of those is a *write-time* refusal only
    (:func:`netkeeper.crm.lists.check_list_references`); compiling a stored
    filter that names a list since deleted matches nobody rather than raising.

    ``list_ids`` is the chain it was expanding when it stopped, outermost first.
    """

    def __init__(self, path: str, message: str, list_ids: Sequence[int]) -> None:
        self.list_ids: tuple[int, ...] = tuple(list_ids)
        super().__init__([FilterIssue(path, message)])


_STRIPPED_SEGMENTS: Final[frozenset[str]] = frozenset(OPS)


def _path(loc: Sequence[str | int]) -> str:
    """Pydantic's location as a JSON path, without the discriminator tags it inserts.

    ``('where', 'and', 'children', 1, 'eq', 'value')`` becomes
    ``where.children[1].value``: the tag segments name the branch Pydantic took,
    not a key in the document, and no key of ours is spelled like an op.
    """
    out = ""
    for segment in loc:
        if isinstance(segment, int):
            out += f"[{segment}]"
        elif segment in _STRIPPED_SEGMENTS:
            continue
        else:
            out += f".{segment}" if out else segment
    return out


def _message(err: ErrorDetails) -> str:
    """Pydantic's message, minus the boilerplate the UI does not need."""
    ctx = err.get("ctx") or {}
    match err["type"]:
        case "value_error":
            return str(ctx.get("error", err["msg"]))
        case "union_tag_invalid":
            return f"unknown op {ctx.get('tag')!r}"
        case "union_tag_not_found":
            return "missing op"
        case _:
            return err["msg"]


def parse_filter(data: Any) -> FilterTree:
    """A :class:`FilterTree` from decoded JSON, or :class:`FilterError`."""
    try:
        return FilterTree.model_validate(data)
    except ValidationError as exc:
        raise FilterError.from_validation(exc) from None


def parse_sort(data: Any) -> list[SortKey]:
    """A list of :class:`SortKey` from decoded JSON, or :class:`FilterError`."""
    try:
        return _SORT_ADAPTER.validate_python(data)
    except ValidationError as exc:
        raise FilterError.from_validation(exc) from None


def list_ids_in(tree: FilterTree) -> dict[int, str]:
    """Every list ``tree`` names directly, with the path of the node that names it.

    Only this tree's own nodes: a list it names carries its own tree, which was
    checked when *it* was written. :func:`netkeeper.crm.lists.check_list_references`
    uses this to refuse a write that names a list the user does not have — the
    write-time half of the rule that says compiling such a reference matches
    nobody (see "Lists" in the module docstring).
    """
    found: dict[int, str] = {}

    def walk(node: FilterNode, path: str) -> None:
        match node:
            case And() | Or():
                for index, child in enumerate(node.children):
                    walk(child, f"{path}.children[{index}]")
            case Not():
                walk(node.child, f"{path}.child")
            case ListMember():
                found.setdefault(node.list_id, path)
            case _:
                return

    if tree.where is not None:
        walk(tree.where, "where")
    return found


# --- the compiler -----------------------------------------------------------


def compile_where(
    user: User,
    tree: FilterTree,
    *,
    session: Session,
    now: datetime | None = None,
    expanding: Sequence[int] = (),
    all_lists: Mapping[int, ContactList] | None = None,
) -> ColumnElement[bool]:
    """The ``WHERE`` clause of ``tree`` for ``user``, to put on a scoped statement.

    :func:`compile_filter`, :func:`compile_count`, and :func:`compile_update` are
    built on it. ``now`` is the instant relative windows count back from; aware,
    UTC by default.

    ``session`` resolves ``list_member`` (see "Lists" in the module docstring);
    a tree with no ``list_member`` anywhere in it never touches it, but the
    argument is required rather than optional so that a caller cannot be one
    predicate away from a filter it silently cannot compile. ``expanding`` is
    the list ids this tree already stands for, so a ``list_member`` back to one
    of them is a cycle: :func:`netkeeper.crm.lists.update_list` passes the list
    whose filter it is about to store.

    ``all_lists`` is **every** list of this user, by id, for a caller that
    already holds them: compiling then reads nothing at all, and a reference to
    an id the mapping does not carry means the user has no such list. It is for
    the callers that compile several trees in one breath — the lists page, the
    guard that recompiles every list after a write — where looking each list up
    per compile turns one page into hundreds of statements. A partial mapping
    would quietly make real lists match nobody, so pass all of them or none.
    """
    clock = _Clock.at(user, now)
    clauses = _liveness(tree)
    if tree.where is not None:
        compiler = _Compiler(user, clock, session, expanding, all_lists)
        clauses.append(compiler.node(tree.where, "where"))
    return and_(*clauses)


def _liveness(tree: FilterTree) -> list[ColumnElement[bool]]:
    """The terms every tree carries: never a merged-away contact, archived only on request."""
    clauses: list[ColumnElement[bool]] = [Contact.merged_into_id.is_(None)]
    if not tree.include_archived:
        clauses.append(Contact.archived_at.is_(None))
    return clauses


def compile_filter(
    user: User,
    tree: FilterTree,
    *,
    session: Session,
    now: datetime | None = None,
    all_lists: Mapping[int, ContactList] | None = None,
) -> Select[tuple[Contact]]:
    """``scoped(user, Contact)`` filtered by ``tree``. Sort and page it with the helpers below."""
    where = compile_where(user, tree, session=session, now=now, all_lists=all_lists)
    return scoped(user, Contact).where(where)


def compile_count(
    user: User,
    tree: FilterTree,
    *,
    session: Session,
    now: datetime | None = None,
    all_lists: Mapping[int, ContactList] | None = None,
) -> Select[tuple[int]]:
    """``scoped_count(user, Contact)`` filtered by ``tree``."""
    where = compile_where(user, tree, session=session, now=now, all_lists=all_lists)
    return scoped_count(user, Contact).where(where)


def compile_update(
    user: User, tree: FilterTree, *, session: Session, now: datetime | None = None
) -> Update:
    """``scoped_update(user, Contact)`` filtered by ``tree``, for a bulk action; add ``.values()``.

    The statement carries ``synchronize_session=False``. The ORM's default,
    ``"auto"``, first tries to evaluate the WHERE in Python against the session,
    cannot with an ``EXISTS`` in it, and then issues its own SELECT to find the
    matching rows; that SELECT gets only the call-level execution options, not
    the statement's scope mark, so the guard rejects it. Without synchronization
    the session's loaded ``Contact`` objects keep their old attribute values
    until they are expired or reloaded; ``session.expire_all()`` after a bulk
    write, or a fresh session, is the pattern.
    """
    return (
        scoped_update(user, Contact)
        .where(compile_where(user, tree, session=session, now=now))
        .execution_options(synchronize_session=False)
    )


def apply_sort(
    statement: Select[tuple[Contact]], sort: Sequence[SortKey]
) -> Select[tuple[Contact]]:
    """Replace the ordering of ``statement`` with ``sort``, then ``id`` ascending as the tiebreak.

    Strings order by ``lower()``; NULLs go last in either direction (SQLite and
    PostgreSQL disagree on the default), so the page is the same on both.
    """
    clauses: list[ColumnElement[Any]] = []
    for key in sort:
        column = _COLUMNS[key.field]
        expr = func.lower(column) if FIELDS[key.field].kind == "string" else column
        ordered = expr.desc() if key.direction == "desc" else expr.asc()
        clauses.append(ordered.nulls_last())
    clauses.append(Contact.id.asc())
    return statement.order_by(None).order_by(*clauses)


def paginate(
    statement: Select[tuple[Contact]], *, limit: int, offset: int = 0
) -> Select[tuple[Contact]]:
    """One page. ``limit`` is at least 1 and ``offset`` at least 0, else :class:`ValueError`."""
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must not be negative")
    return statement.limit(limit).offset(offset)


@dataclass(frozen=True)
class _Clock:
    now: datetime
    today: date

    @classmethod
    def at(cls, user: User, now: datetime | None) -> _Clock:
        if now is None:
            now = utcnow()
        elif now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        try:
            zone = ZoneInfo(user.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            log.warning(
                "user %s has unknown timezone %r; day windows count from UTC",
                user.id,
                user.timezone,
            )
            zone = ZoneInfo("UTC")
        return cls(now=now.astimezone(UTC), today=now.astimezone(zone).date())


MAX_LIST_EXPANSIONS: Final = 32
"""How many smart lists one compile may inline. See "Lists" in the module docstring."""


class _Compiler:
    def __init__(
        self,
        user: User,
        clock: _Clock,
        session: Session,
        expanding: Sequence[int] = (),
        all_lists: Mapping[int, ContactList] | None = None,
    ) -> None:
        self.user = user
        self.clock = clock
        self.session = session
        self._expanding: tuple[int, ...] = tuple(expanding)
        self._all_lists = all_lists
        self._lists: dict[int, ContactList | None] = {}
        self._expansions = 0

    def node(self, node: FilterNode, path: str) -> ColumnElement[bool]:
        match node:
            case And():
                return and_(*self._children(node.children, path))
            case Or():
                return or_(*self._children(node.children, path))
            case Not():
                return not_(self.node(node.child, f"{path}.child"))
            case Eq():
                return self._eq(node.field, node.value)
            case Neq():
                return not_(self._eq(node.field, node.value))
            case Contains():
                column = _COLUMNS[node.field]
                return _present(column, column.icontains(node.value, autoescape=True))
            case StartsWith():
                column = _COLUMNS[node.field]
                return _present(column, column.istartswith(node.value, autoescape=True))
            case IsEmpty():
                column = _COLUMNS[node.field]
                if FIELDS[node.field].kind == "string":
                    return or_(column.is_(None), column == "")
                return column.is_(None)
            case Gt():
                column = _COLUMNS[node.field]
                return _present(column, column > typed_value(node.field, node.value))
            case Gte():
                column = _COLUMNS[node.field]
                return _present(column, column >= typed_value(node.field, node.value))
            case Lt():
                column = _COLUMNS[node.field]
                return _present(column, column < typed_value(node.field, node.value))
            case Lte():
                column = _COLUMNS[node.field]
                return _present(column, column <= typed_value(node.field, node.value))
            case Between():
                column = _COLUMNS[node.field]
                low, high = typed_value(node.field, node.low), typed_value(node.field, node.high)
                return _present(column, column.between(low, high))
            case HasEmail():
                emails = self._child_rows(ContactEmail)
                if node.status is not None:
                    emails = emails.where(ContactEmail.status == node.status)
                return emails.exists()
            case HasPhone():
                return self._child_rows(ContactPhone).exists()
            case HasLiUrl():
                return and_(Contact.li_url.is_not(None), Contact.li_url != "")
            case HasPosition():
                return self._child_rows(ContactPosition).exists()
            case EmailContains():
                return (
                    self._child_rows(ContactEmail)
                    .where(ContactEmail.email.icontains(node.value, autoescape=True))
                    .exists()
                )
            case LastContacted():
                column = Contact.last_contacted_at
                if node.never:
                    return column.is_(None)
                if node.within_days is not None:
                    return _present(column, column >= self._ago(node.within_days))
                if node.older_than_days is not None:
                    return _present(column, column < self._ago(node.older_than_days))
                raise AssertionError("validated: exactly one option is set")
            case ConnectedWithinDays():
                cutoff = self.clock.today - timedelta(days=node.days)
                return _present(Contact.connected_on, Contact.connected_on >= cutoff)
            case ChangedJobsWithinDays():
                return (
                    self._child_rows(ContactSnapshot)
                    .where(ContactSnapshot.observed_at >= self._ago(node.days))
                    .exists()
                )
            case TagAny():
                return self._tagged(node.names).exists()
            case TagAll():
                # One EXISTS per distinct name: a contact must carry each of them.
                keys = sorted({tag_name_key(name) for name in node.names})
                return and_(*(self._tagged([key]).exists() for key in keys))
            case TagNone():
                return not_(self._tagged(node.names).exists())
            case ListMember():
                return self._list_member(node.list_id, path)
            case EnrolledIn() | RepliedIn():
                raise UnsupportedPredicate(node.op, path, PLACEHOLDERS[node.op])
            case _ as unreachable:
                assert_never(unreachable)

    def _children(self, children: Sequence[FilterNode], path: str) -> list[ColumnElement[bool]]:
        return [self.node(child, f"{path}.children[{i}]") for i, child in enumerate(children)]

    def _eq(self, field: str, value: str | int | bool) -> ColumnElement[bool]:
        column = _COLUMNS[field]
        typed = typed_value(field, value)
        if FIELDS[field].kind == "string":
            return _present(column, func.lower(column) == str(typed).lower())
        return _present(column, column == typed)

    def _child_rows(
        self, model: type[ContactEmail | ContactPhone | ContactPosition | ContactSnapshot]
    ) -> Select[tuple[int]]:
        """``SELECT id FROM <child> WHERE contact_id = contacts.id AND user_id = :user``.

        The ``user_id`` term is defense in depth: the outer statement is already
        scoped, and a child can only belong to its parent's user.
        """
        return (
            select(model.id)
            .where(model.contact_id == Contact.id, model.user_id == self.user.id)
            .correlate(Contact)
        )

    def _list_member(self, list_id: int, path: str) -> ColumnElement[bool]:
        """Membership of list ``list_id``, meaning whatever ``lists.list_members`` means.

        A list this user does not have matches nobody (see the module
        docstring), which is also what makes another user's list id safe to
        name: the lookup is scoped, so it finds nothing and compiles to
        ``false`` rather than to somebody else's members.
        """
        row = self._list(list_id)
        if row is None:
            return false()
        if row.kind is ListKind.STATIC:
            # The EXISTS says "has a row"; ``archived_at`` says "and is still in
            # the address book", which is what netkeeper.crm.lists
            # ._static_members_base adds to the same join. It is repeated here
            # because it is the *predicate's* term, not the tree's: a membership
            # row outlives its contact being archived, and ``include_archived``
            # on the tree around this node must not turn an archived contact
            # back into a member of a static list. ``merged_into_id`` needs no
            # such term: _liveness() adds it on every path and no tree can turn
            # it off.
            return and_(Contact.archived_at.is_(None), self._members_of(row.id).exists())
        return self._inline(row, path)

    def _list(self, list_id: int) -> ContactList | None:
        """The user's list, or None. One query per distinct id per compile, not per row.

        ``no_autoflush`` because compiling is not the caller's write: a filter
        validated halfway through an edit (``netkeeper.crm.lists.update_list``
        sets the new name, then compiles the new filter) must not flush that
        half-done work just because the compiler read a row. Every caller
        flushes a list it has just created before it compiles anything naming
        it, so nothing is missed by not flushing here.
        """
        if self._all_lists is not None:
            return self._all_lists.get(list_id)
        if list_id not in self._lists:
            with self.session.no_autoflush:
                self._lists[list_id] = get_scoped(self.session, self.user, ContactList, list_id)
        return self._lists[list_id]

    def _members_of(self, list_id: int) -> Select[tuple[int]]:
        """``list_members`` rows of this contact in that list; correlated, like a child subquery.

        ``(user_id, list_id, contact_id)`` is the table's unique constraint, so
        this is an index lookup per candidate row rather than a scan, and the
        ``user_id`` term is the same defense in depth :meth:`_child_rows` keeps.
        """
        return (
            select(ListMemberRow.id)
            .where(
                ListMemberRow.contact_id == Contact.id,
                ListMemberRow.user_id == self.user.id,
                ListMemberRow.list_id == list_id,
            )
            .correlate(Contact)
        )

    def _inline(self, row: ContactList, path: str) -> ColumnElement[bool]:
        """A smart list's stored tree, compiled in place, with the cycle and size guards."""
        if row.id in self._expanding:
            chain = " -> ".join(str(list_id) for list_id in (*self._expanding, row.id))
            raise ListReferenceError(
                path,
                f"list {row.id} is defined in terms of itself ({chain})",
                (*self._expanding, row.id),
            )
        if self._expansions >= MAX_LIST_EXPANSIONS:
            raise ListReferenceError(
                path,
                f"this filter pulls in more than {MAX_LIST_EXPANSIONS} lists",
                (*self._expanding, row.id),
            )
        self._expansions += 1
        # The CHECK constraint on ``lists`` keeps filter_json and kind in step.
        assert row.filter_json is not None, "a smart list always carries a filter"
        tree = parse_filter(row.filter_json)
        clauses = _liveness(tree)
        if tree.where is not None:
            self._expanding += (row.id,)
            try:
                clauses.append(self.node(tree.where, f"list[{row.id}].where"))
            finally:
                self._expanding = self._expanding[:-1]
        return and_(*clauses)

    def _tagged(self, names: Sequence[str]) -> Select[tuple[int]]:
        """``contact_tags`` rows of this contact whose tag is named in ``names``.

        Names match ``tags.name_key`` (the lowercased name), so the comparison is
        case-insensitive. Both tables carry the ``user_id`` term, as every child
        subquery does.
        """
        keys = {tag_name_key(name) for name in names}
        return (
            select(ContactTag.id)
            .join(Tag, Tag.id == ContactTag.tag_id)
            .where(
                ContactTag.contact_id == Contact.id,
                ContactTag.user_id == self.user.id,
                Tag.user_id == self.user.id,
                Tag.name_key.in_(keys),
            )
            .correlate(Contact)
        )

    def _ago(self, days: int) -> datetime:
        return self.clock.now - timedelta(days=days)


def _present(column: InstrumentedAttribute[Any], test: ColumnElement[bool]) -> ColumnElement[bool]:
    """``test`` made two-valued: false, not NULL, when ``column`` is NULL."""
    if Contact.__table__.c[column.key].nullable:
        return and_(column.is_not(None), test)
    return test


# --- describe ---------------------------------------------------------------

_MET_PHRASES: Final[dict[str, str]] = {
    "unknown": "untriaged",
    "met": "met",
    "not_met": "not met",
    "skip": "skipped",
}
_ORDER_WORDS: Final[dict[str, tuple[str, str]]] = {
    # op: (for dates and datetimes, for numbers)
    "gt": ("after", "more than"),
    "gte": ("since", "at least"),
    "lt": ("before", "less than"),
    "lte": ("up to", "at most"),
}
_EMAIL_STATUS_PHRASES: Final[dict[str, str]] = {
    "ok": "a working email",
    "bounced": "a bounced email",
    "invalid": "an invalid email",
}


def describe(tree: FilterTree) -> str:
    """A short reading for a list header: "met, has email, connected in the last 365 days".

    Top-level ``and`` terms are comma-separated; nested groups are parenthesized
    with ``and`` / ``or`` spelled out. Placeholders are described like any other
    predicate.
    """
    text = "all contacts" if tree.where is None else _describe(tree.where, top=True)
    return f"{text}, including archived" if tree.include_archived else text


def _describe(node: FilterNode, *, top: bool = False) -> str:
    match node:
        case And():
            parts = [_describe(child) for child in node.children]
            return ", ".join(parts) if top else "(" + " and ".join(parts) + ")"
        case Or():
            joined = " or ".join(_describe(child) for child in node.children)
            return joined if top else f"({joined})"
        case Not():
            return f"not {_describe(node.child)}"
        case Eq():
            return _describe_eq(node.field, node.value, negated=False)
        case Neq():
            return _describe_eq(node.field, node.value, negated=True)
        case Contains():
            return f'{_LABELS[node.field]} contains "{node.value}"'
        case StartsWith():
            return f'{_LABELS[node.field]} starts with "{node.value}"'
        case IsEmpty():
            return f"{_LABELS[node.field]} is empty"
        case Gt() | Gte() | Lt() | Lte():
            words = _ORDER_WORDS[node.op]
            word = words[1] if FIELDS[node.field].kind == "int" else words[0]
            return f"{_LABELS[node.field]} {word} {node.value}"
        case Between():
            return f"{_LABELS[node.field]} between {node.low} and {node.high}"
        case HasEmail():
            what = "email" if node.status is None else _EMAIL_STATUS_PHRASES[node.status.value]
            return f"has {what}"
        case HasPhone():
            return "has phone"
        case HasLiUrl():
            return "has LinkedIn URL"
        case HasPosition():
            return "has a position"
        case EmailContains():
            return f'email contains "{node.value}"'
        case LastContacted():
            if node.never:
                return "never contacted"
            if node.within_days is not None:
                return f"contacted in the last {node.within_days} days"
            return f"last contacted more than {node.older_than_days} days ago"
        case ConnectedWithinDays():
            return f"connected in the last {node.days} days"
        case ChangedJobsWithinDays():
            return f"changed jobs in the last {node.days} days"
        case TagAny():
            return f"tagged any of {', '.join(node.names)}"
        case TagAll():
            return f"tagged all of {', '.join(node.names)}"
        case TagNone():
            return f"not tagged {', '.join(node.names)}"
        case ListMember():
            return f"in list #{node.list_id}"
        case EnrolledIn():
            return f"enrolled in campaign #{node.campaign_id}"
        case RepliedIn():
            return f"replied in campaign #{node.campaign_id}"
        case _ as unreachable:
            assert_never(unreachable)


def _describe_eq(field: str, value: str | int | bool, *, negated: bool) -> str:
    label = _LABELS[field]
    if field == "met" and isinstance(value, str):
        phrase = _MET_PHRASES[value]
        return f"{label} is not {value}" if negated else phrase
    if field == "do_not_contact":
        wanted = bool(value) != negated
        return "do not contact" if wanted else "ok to contact"
    if field == "source":
        return f"not from {value}" if negated else f"from {value}"
    shown = f'"{value}"' if isinstance(value, str) else str(value)
    return f"{label} is not {shown}" if negated else f"{label} is {shown}"
