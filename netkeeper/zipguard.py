"""Refuse a hostile zip on its declared metadata, before any member is opened.

Shared by the LinkedIn archive importer and the history workbook reader (an
xlsx file is a zip). Nothing here decompresses a member.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from typing import Final

# A zip member larger than this, uncompressed, is refused before it is read.
# messages.csv for a heavy user is tens of megabytes; this is far above that
# and keeps a crafted archive from expanding without bound.
MAX_MEMBER_BYTES: Final = 512 * 1024 * 1024
# Every declared uncompressed size in a zip, summed, refused before any member
# is opened. A real export's total is a couple of megabytes (the sample this
# was checked against: about 1.4 MB across thirty-odd members, 34 files); 64 MiB
# is still about 45x that, but it also directly bounds how much decompression a
# hostile zip can buy under the ratio guard below, so it is kept much closer to
# a real export's size than :data:`MAX_MEMBER_BYTES` is.
MAX_TOTAL_UNCOMPRESSED_BYTES: Final = 64 * 1024 * 1024
# How many members a zip may declare. The sample above has 34 files, and a
# "Basic" LinkedIn export is not the largest kind: a "Complete" export, or one
# with message attachments, plausibly has several times that. This is checked
# from the End Of Central Directory record before :class:`zipfile.ZipFile` ever
# parses an entry (the archive importer's declared-member-count check), so raising it does not
# raise the cost of refusing a "many tiny files" zip — that cost is paid only
# up to whatever a hostile zip declares, and a zip declaring more than this is
# refused without a single entry being materialized.
MAX_MEMBERS: Final = 5000
# A member's declared uncompressed size divided by its declared compressed
# size, refused above this ratio — the classic zip bomb shape, a few bytes of
# input inflating to gigabytes. Ordinary CSV text under DEFLATE rarely clears
# single digits. The ratio is only judged once a member is big enough for the
# question to matter (below the floor, a high ratio is just DEFLATE doing well
# on a short repetitive cell, not a bomb); the sample archive's largest member,
# messages.csv, compresses at under 6x and stays well under the floor too, so
# nothing in a real export exercises this branch — the boundary is exercised by
# test, not by any export this was checked against.
MAX_COMPRESSION_RATIO: Final = 100
COMPRESSION_RATIO_FLOOR_BYTES: Final = 8 * 1024 * 1024


class ZipRefusal(ValueError):
    """A zip is refused. ``code`` is one of ``too_large``, ``too_many_members``,
    ``compression_ratio_too_high`` or ``unsafe_member_path``."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def guard_zip_members(infos: Sequence[zipfile.ZipInfo], name: str) -> None:
    """Refuse a hostile zip on its declared metadata, before any member is opened.

    By the time this runs, ``infos`` already exists — the archive importer's declared-member
    count check is what keeps a zip declaring far too many members from paying to build it
    in the first place, checked before :class:`zipfile.ZipFile` parses a single
    entry. The count is checked again here in case that estimate and what
    :class:`zipfile.ZipFile` actually materialized disagree; every other check
    reads only what the central directory already states for each member — a
    declared path, a declared size, a declared compressed size — so refusing
    one costs no decompression. Checked over every member, not only the
    ``.csv``-looking ones, because a bomb does not have to look like a table to
    cost something if it were opened.
    """
    if len(infos) > MAX_MEMBERS:
        raise ZipRefusal(
            f"{name}: {len(infos)} members in the zip, over the {MAX_MEMBERS} member limit",
            "too_many_members",
        )
    total = 0
    for info in infos:
        if not is_safe_member_path(info.filename):
            raise ZipRefusal(
                f"{name}: member {info.filename!r} has an unsafe path",
                "unsafe_member_path",
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise ZipRefusal(
                f"{name}: member {info.filename} is {info.file_size} bytes uncompressed, "
                f"over the {MAX_MEMBER_BYTES} byte limit",
                "too_large",
            )
        if info.file_size > COMPRESSION_RATIO_FLOOR_BYTES:
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > MAX_COMPRESSION_RATIO:
                raise ZipRefusal(
                    f"{name}: member {info.filename} compresses {ratio:.0f}x, over the "
                    f"{MAX_COMPRESSION_RATIO}x ratio a real export never approaches",
                    "compression_ratio_too_high",
                )
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ZipRefusal(
                f"{name}: more than {MAX_TOTAL_UNCOMPRESSED_BYTES} bytes uncompressed "
                "across its members",
                "too_large",
            )


def is_safe_member_path(filename: str) -> bool:
    """False for a path that would escape wherever a member's name was ever used as a path.

    Nothing here extracts a member to disk — :func:`_zip_opener` reads it into
    memory — but a name has to earn the right to be treated as one before it
    is used in a log line or an error message either, and ``zipfile`` only
    sanitizes a path on ``extract``, never on ``infolist`` or ``open``.
    """
    normalized = filename.replace("\\", "/")
    if normalized.startswith("/"):
        return False
    if len(normalized) > 1 and normalized[1] == ":":  # a drive letter, e.g. "C:/"
        return False
    return ".." not in normalized.split("/")
