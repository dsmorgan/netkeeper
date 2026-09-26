"""Strip HTML from existing archive-sourced interaction summaries (#75).

The LinkedIn archive importer used to store a message body as the raw HTML
fragment LinkedIn exports it as, cut on a raw character boundary -- which
could land mid-tag. The import path now converts to plain text before that
cut ever happens, but the rows an earlier import already wrote are stuck the
old way: the importer's own natural-key matching treats an already-present
row as done and never rewrites its ``summary``, so telling someone to
re-import their archive would not fix them. This is a one-time data backfill
instead.

Restricted to ``source = 'archive'``: those are the only rows this code path
ever wrote, and a person's own typed note or manually-entered interaction is
never touched, even if it happens to contain an angle bracket.

The cleanup logic mirrors the archive importer's own ``_html_to_text`` helper
rather than importing it: a migration is frozen history and must not track
the application's own modules. Only a tag
LinkedIn's own rich-text editor is actually known to emit is stripped; an
unrecognized bracketed word is put back verbatim rather than guessed at,
because a person's own plain text -- an email address in angle brackets, a
placeholder -- must not be deleted by a cleanup pass. See the importer's
docstring for the full reasoning.

The mirror is frozen on purpose and does not track later fixes to the
importer. A migration that has run cannot run again, so editing this copy
would change nothing for the databases it already cleaned and would make
the same revision mean different things on different machines. #231's fix to
how ``_TextExtractor.close()`` settles an unterminated tag at the end of the
input, for instance, is in ``crm/archive.py`` and deliberately not here. A
later fix that existing rows need ships as a new migration.

Not a perfect match for a re-import, though: a summary already cut to
``SUMMARY_MAX_CHARS`` at the old, raw character boundary lost whatever came
after that cut long before this migration runs, so a long body ends up
trimmed at a different point than a fresh import of the same archive would
produce today. Nothing to do about it -- the original text is gone -- but
worth knowing rather than discovering by diffing the two.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}
)
_INLINE_TAGS = frozenset({"a", "strong", "em", "u", "span", "img", "script", "style"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _INLINE_TAGS:
            pass
        else:
            self._parts.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in _INLINE_TAGS:
            pass
        else:
            self._parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _plain_pass(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (line.strip() for line in parser.text().splitlines())
    return "\n".join(line for line in lines if line)


def _plain_text(value: str) -> str:
    # Run twice: see the archive importer's _html_to_text on why one pass is not
    # enough for a tag spelled out with entities (e.g. "&lt;p&gt;").
    return _plain_pass(_plain_pass(value))


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, summary FROM interactions WHERE source = 'archive' AND summary IS NOT NULL"
        )
    ).all()
    for row_id, summary in rows:
        cleaned = _plain_text(summary) or None
        if cleaned != summary:
            connection.execute(
                sa.text("UPDATE interactions SET summary = :summary WHERE id = :id"),
                {"summary": cleaned, "id": row_id},
            )


def downgrade() -> None:
    # Lossy: the tags and entities this stripped are gone, so there is nothing
    # to put back, and no schema changed for there to be anything else to undo.
    pass
