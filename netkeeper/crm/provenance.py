"""Per-field provenance for LinkedIn fields (spec 10.5).

``contacts.field_sources`` maps a scalar column of ``contacts`` to the source
that last wrote it. The rule: an imported value never overwrites a value from a
more authoritative source. For LinkedIn fields, ``sync`` beats ``archive``
beats ``csv`` beats ``manual``. The fields a person owns (``preferred_name``,
``notes``, ``met``, and tags) are never in ``field_sources``: ``manual`` always
wins there, and no import touches them.

This module is the rule alone. Applying it to an incoming row, and recording
the source that wins, is identity resolution and import (P1-02 onward).
"""

from __future__ import annotations

from typing import Final

from netkeeper.models import Contact, ContactSource

# Higher wins. Equal rank may overwrite: a newer sync updates an older one.
SOURCE_RANK: Final[dict[str, int]] = {"sync": 3, "archive": 2, "csv": 1, "manual": 0}

# The scalar columns of ``contacts`` that ``field_sources`` tracks.
PROVENANCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
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
    }
)

# Owned by the person, never by an import; tags (P1-07) join them outside ``contacts``.
PERSON_OWNED_FIELDS: Final[frozenset[str]] = frozenset({"preferred_name", "notes", "met"})


def may_overwrite(field: str, incoming_source: str, contact: Contact) -> bool:
    """True when a value from ``incoming_source`` may replace ``contact``'s ``field``.

    A person-owned field takes ``manual`` and nothing else. A provenance field
    with no value yet (``None`` or an empty string), or with no recorded source,
    is free to any source. Otherwise the incoming source must rank at least as
    high as the one that last wrote the field. ``ValueError`` for a field that
    carries no provenance or a source that is not a :class:`ContactSource`.
    """
    source = ContactSource(incoming_source)
    if field in PERSON_OWNED_FIELDS:
        return source is ContactSource.MANUAL
    if field not in PROVENANCE_FIELDS:
        raise ValueError(f"{field!r} carries no provenance; see PROVENANCE_FIELDS")
    current = getattr(contact, field)
    if current is None or current == "":
        return True
    # Unset on a contact that was never flushed: the column default fills it at insert.
    recorded = (contact.field_sources or {}).get(field)
    if recorded is None:
        return True
    return SOURCE_RANK[source.value] >= SOURCE_RANK[ContactSource(recorded).value]
