"""Per-field provenance for LinkedIn fields (spec 10.5).

``contacts.field_sources`` maps a scalar column of ``contacts`` to the source
that last wrote it. The rule: an imported value never overwrites a value from a
more authoritative source. For LinkedIn fields a person's own edit is the most
authoritative: once ``manual`` is recorded, only another manual edit or a
revert changes the field (CP1, #28). Among the automated sources ``sync`` beats
``archive`` beats ``csv``. The fields a person owns (``preferred_name``,
``notes``, ``met``, and tags) are never in ``field_sources``: ``manual`` always
wins there, and no import touches them.

So that an override can be undone, ``contacts.synced_values`` keeps the last
value each automated source reported for every LinkedIn field, whether or not
that value reached the live column (:class:`~netkeeper.models.SyncedValue`).
:func:`record_synced_value` writes that ledger, :func:`revert_to_synced` puts
the recorded value and its source back on the contact, and
:func:`overridden_fields` names the fields where an edit hides a different
synced value.

``met`` is a person-owned field with a second column beside it: ``met_source``
says whether the person decided it or a triage batch they accepted did, and
:func:`set_met` is how a decision writes the pair (spec 10.2). Two paths write
the columns directly and each says why: :func:`netkeeper.crm.triage._restore`
puts back what a decision recorded, which is not a new decision, and
:func:`netkeeper.crm.contacts.bulk_update` sets both in one UPDATE over rows it
never loads. No import touches either one.

This module is the rule, :func:`set_manual_field` and :func:`set_met` for what a
person owns, and the ledger. Applying the rule to an incoming row, and recording
the source that wins, is identity resolution and import
(:mod:`netkeeper.crm.identity`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import Date, inspect

from netkeeper.models import Contact, ContactMet, ContactSource, MetSource, SyncedValue

# Higher wins. Equal rank may overwrite: a newer sync updates an older one, and a
# later edit replaces an earlier one. ``manual`` ranks highest for a LinkedIn
# field, so an override sticks until revert_to_synced() (CP1, #28).
SOURCE_RANK: Final[dict[str, int]] = {"manual": 4, "sync": 3, "archive": 2, "csv": 1}

# The scalar columns of ``contacts`` that ``field_sources`` tracks, in the order
# apply() writes them: identity first, so the li_public_id validator has derived
# li_url before li_url itself is assigned.
PROVENANCE_ORDER: Final[tuple[str, ...]] = (
    "li_urn",
    "li_public_id",
    "li_url",
    "first_name",
    "last_name",
    "headline",
    "current_title",
    "current_company",
    "location",
    "connected_on",
)
PROVENANCE_FIELDS: Final[frozenset[str]] = frozenset(PROVENANCE_ORDER)

# Owned by the person, never by an import; tags (P1-07) join them outside ``contacts``.
PERSON_OWNED_FIELDS: Final[frozenset[str]] = frozenset({"preferred_name", "notes", "met"})


def may_overwrite(field: str, incoming_source: str, contact: Contact) -> bool:
    """True when a value from ``incoming_source`` may replace ``contact``'s ``field``.

    A person-owned field takes ``manual`` and nothing else. A provenance field
    with no recorded source is free to any source, and so is one whose value is
    empty (``None`` or an empty string), unless that emptiness is a person's
    doing: a field cleared by hand carries ``manual`` and sticks like any other
    edit. Otherwise the incoming source must rank at least as high as the one
    that last wrote the field, and ``manual`` ranks highest: a field a person
    edited is closed to the sync and to every import until
    :func:`revert_to_synced`, and open only to another edit. ``ValueError`` for
    a field that carries no provenance or a source that is not a
    :class:`ContactSource`.
    """
    source = ContactSource(incoming_source)
    if field in PERSON_OWNED_FIELDS:
        return source is ContactSource.MANUAL
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} carries no provenance; see PROVENANCE_FIELDS")
    # Unset on a contact that was never flushed: the column default fills it at insert.
    recorded = (contact.field_sources or {}).get(field)
    if recorded is None:
        return True
    current = getattr(contact, field)
    if (current is None or current == "") and ContactSource(recorded) is not ContactSource.MANUAL:
        return True  # nothing to protect, unless a person emptied it on purpose
    return SOURCE_RANK[source.value] >= SOURCE_RANK[ContactSource(recorded).value]


def set_met(contact: Contact, value: ContactMet, *, source: MetSource) -> None:
    """Write ``met`` and record who decided it (spec 10.2).

    ``met`` is a field the person owns: no sync, archive, or CSV import writes
    it, and :func:`may_overwrite` says so. But the person is not the only one who
    decides it any more — a triage batch they accepted decides in bulk — so the
    contact carries ``met_source`` beside the value, and every write that is a
    *decision* goes through here (the module docstring names the two that are
    not, and why). ``MetSource.AUTOMATIC`` is netkeeper's own answer, waiting to
    be reviewed; ``MetSource.MANUAL`` is the person's, and replacing an automatic
    answer with a manual one is what closes the review.

    The decision log records both columns, so undo puts the pair back together
    (:mod:`netkeeper.crm.triage`).
    """
    contact.met = ContactMet(value)
    contact.met_source = MetSource(source)


def set_manual_field(contact: Contact, field: str, value: str | date | ContactMet | None) -> None:
    """Write ``field`` on ``contact`` as a person's own edit, unconditionally.

    The contacts PATCH endpoint (P1-05) uses this for every editable column and
    never :func:`netkeeper.crm.identity.apply`, which is for imports and the sync.
    A provenance field takes the value and records ``manual`` in
    ``field_sources``, the highest rank for a LinkedIn field (spec 10.5, CP1
    #28): from then on no sync, archive, or CSV import overwrites it, and that
    holds for a field cleared to ``None`` or ``""`` just the same. Those sources
    still note what they saw in ``synced_values``, so :func:`revert_to_synced`
    can put LinkedIn's value back whenever the person asks. A person-owned field
    (``preferred_name``, ``notes``, ``met``) is just written; no import touches
    those. ``ValueError`` for any other column. A slug written here records no
    alias; that is :func:`~netkeeper.crm.identity.apply`'s job.
    """
    if field in PERSON_OWNED_FIELDS:
        setattr(contact, field, value)
        return
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} is neither a provenance nor a person-owned field")
    setattr(contact, field, value)
    _set_source(contact, field, ContactSource.MANUAL)


# --- the synced-values ledger -----------------------------------------------


def record_synced_value(
    contact: Contact,
    field: str,
    value: str | date | None,
    *,
    source: str | ContactSource,
    observed_at: datetime,
) -> bool:
    """Note that ``source`` reported ``value`` for ``field`` at ``observed_at``.

    Writes ``contact.synced_values[field]`` whether or not the live column takes
    the value (:func:`may_overwrite` decides that separately), so the last synced
    value is always there to revert to. The ledger is chronological, not ranked:
    an observation older than the one recorded is dropped, as a child row's is,
    and False says so; one at the same instant or later replaces it. Repeating
    an observation the ledger already holds, as a second import of the same
    file does, returns True and writes nothing, so it leaves ``updated_at``
    alone. A date is stored in ISO form and ``observed_at`` as an ISO datetime
    in UTC.
    ``ValueError`` for a field that carries no provenance, for ``manual`` (a
    person's edit is what the ledger exists to undo, never an entry in it), for a
    source that is not a :class:`ContactSource`, and for a naive ``observed_at``.
    """
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} carries no provenance; see PROVENANCE_FIELDS")
    reported = ContactSource(source)
    if reported is ContactSource.MANUAL:
        raise ValueError("manual is not a synced source")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    existing = (contact.synced_values or {}).get(field)
    if existing is not None and observed_at < datetime.fromisoformat(existing["observed_at"]):
        return False
    entry: SyncedValue = {
        "value": value.isoformat() if isinstance(value, date) else value,
        "source": reported.value,
        "observed_at": observed_at.astimezone(UTC).isoformat(),
    }
    if existing == entry:
        # The same observation again. ``synced_values`` is a MutableDict, so
        # assigning an equal value still marks the row dirty and fires
        # ``onupdate``; an import that changes nothing would re-stamp
        # ``updated_at`` on every contact it read.
        return True
    if not contact.synced_values:  # unset before the first flush, or empty
        contact.synced_values = {field: entry}
    else:
        contact.synced_values[field] = entry
    return True


def revert_to_synced(contact: Contact, field: str) -> None:
    """Put the last synced value of ``field`` back on ``contact``, with its source.

    The undo for :func:`set_manual_field`: the live column takes the value the
    ledger holds and ``field_sources[field]`` becomes the source that reported
    it, so that source and any higher-ranked one may write the field again. The
    ledger entry stays. ``ValueError`` for a field that carries no provenance and
    for one no automated source has reported (``"<field> has no synced value to
    revert to"``). As with any direct write, a slug reverted here records no
    alias, and a slug or URN another contact holds fails at flush.
    """
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} carries no provenance; see PROVENANCE_FIELDS")
    synced = (contact.synced_values or {}).get(field)
    if synced is None:
        raise ValueError(f"{field} has no synced value to revert to")
    setattr(contact, field, _column_value(field, synced["value"]))
    _set_source(contact, field, ContactSource(synced["source"]))


def overridden_fields(contact: Contact) -> list[str]:
    """The provenance fields a person edited that hide a different synced value.

    In :data:`PROVENANCE_ORDER`. What the UI marks "overridden" with a revert
    control: the recorded source is ``manual`` and the ledger holds a value
    other than the live one. An edit that matches what LinkedIn last reported,
    or one on a field no automated source has reported, is not listed; there is
    nothing to revert to.
    """
    sources = contact.field_sources or {}
    synced = contact.synced_values or {}
    return [
        field
        for field in PROVENANCE_ORDER
        if sources.get(field) == ContactSource.MANUAL.value
        and field in synced
        and getattr(contact, field) != _column_value(field, synced[field]["value"])
    ]


def _set_source(contact: Contact, field: str, source: ContactSource) -> None:
    sources = contact.field_sources
    if not sources:  # unset before the first flush (the column default fills it), or empty
        contact.field_sources = {field: source.value}
    else:
        sources[field] = source.value


def _column_value(field: str, raw: str | None) -> str | date | None:
    """A ledger value as the column stores it: a date parsed, an empty name ``''``."""
    column = inspect(Contact).columns[field]
    if raw is None:
        return None if column.nullable else ""  # names are '' when unknown, never NULL
    if isinstance(column.type, Date):
        return date.fromisoformat(raw)
    return raw
