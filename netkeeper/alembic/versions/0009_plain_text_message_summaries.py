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

The cleanup logic is duplicated from the importer's own helper rather than
imported: a migration is frozen history and must not track the application's
own modules.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-21 00:00:00 UTC
"""

from __future__ import annotations

from collections.abc import Sequence
from html.parser import HTMLParser

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "tr", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol"}
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def _plain_text(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    lines = (line.strip() for line in parser.text().splitlines())
    return "\n".join(line for line in lines if line)


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
