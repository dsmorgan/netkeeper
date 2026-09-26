"""Refuse a zip whose central directory lost entries, before it imports partly (#138).

:mod:`zipfile` reads a central directory until it has consumed the byte size
the End Of Central Directory record declares. When corruption garbles a
record's name, extra, or comment length, one record swallows the ones after it
and the parse carries on: the zip opens, the tables whose records survived
import correctly, and the rest simply vanish, not counted and not in
``ignored_files``. A garbled name in a record flagged UTF-8 fails differently,
with a :class:`UnicodeDecodeError` no caller expects.

:func:`check_zip_directory` walks the directory the same way :mod:`zipfile`
does, reading only the fixed part and the name of each record, and compares
the number of records it finds with the number the End Of Central Directory
record declares. When they disagree, or a name cannot be decoded, the archive
is refused as ``damaged`` with both numbers in the message. No member is
decompressed, which keeps the guarantee :mod:`netkeeper.linkedin.archive`'s
guards give: a hostile archive is refused on declared metadata alone. Anything
else wrong with the zip is left for :func:`~netkeeper.linkedin.archive.open_archive`
to refuse in its own words, so its refusal codes do not change.

This belongs with the zip reader in :mod:`netkeeper.linkedin.archive`, whose
``_open_zip`` already reads the same record. It lives here because that module
is outside the lane this fix was made in; moving it there, as a check inside
``_open_zip``, changes nothing a caller sees. Both entry points, ``POST
/imports/archive`` and ``netkeeper import archive``, open archives through
:func:`open_checked_archive`.
"""

from __future__ import annotations

import io
import logging
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from netkeeper.linkedin.archive import (
    MAX_MEMBERS,
    Archive,
    ArchiveFormatError,
    ArchiveRefusalCode,
    open_archive,
)

log = logging.getLogger(__name__)

_EOCD_SIGNATURE: Final = b"PK\x05\x06"
_EOCD_FIXED_SIZE: Final = 22
_EOCD_MAX_COMMENT: Final = 65535
_CENTRAL_DIR_SIGNATURE: Final = b"PK\x01\x02"
_LOCAL_HEADER_SIGNATURE: Final = b"PK\x03\x04"
_LOCAL_HEADER_FIXED_SIZE: Final = 30
_CENTRAL_DIR_FIXED_SIZE: Final = 46
_UTF8_FLAG: Final = 0x800
_ZIP64_ENTRIES: Final = 0xFFFF
_ZIP64_OFFSET: Final = 0xFFFFFFFF


@contextmanager
def open_checked_archive(
    source: Path | IO[bytes], *, filename: str | None = None
) -> Iterator[Archive]:
    """:func:`~netkeeper.linkedin.archive.open_archive`, after :func:`check_zip_directory`.

    Takes and yields exactly what ``open_archive`` does. A directory, or a file
    that is not a zip, is passed through unchecked.
    """
    name = filename or (source.name if isinstance(source, Path) else "upload")
    if isinstance(source, Path):
        if source.is_file():
            with source.open("rb") as handle:
                check_zip_directory(handle, name)
    else:
        check_zip_directory(source, name)
    with open_archive(source, filename=filename) as archive:
        yield archive


def check_zip_directory(handle: IO[bytes], name: str) -> None:
    """``ArchiveFormatError`` (``damaged``) when the central directory lost entries.

    Reads the End Of Central Directory record, then each central directory
    record's fixed part and name, as :mod:`zipfile` would, and the name in the
    local file header each record points at. Refuses when the records found
    are fewer or more than the record count declared, when a record's name is
    not the one its local header carries (a garbled name that no longer ends
    in ``.csv`` would be skipped as noise), or when a name flagged UTF-8 is
    not. Returns quietly for anything it is not the one
    to judge: a file that is not a zip, a zip64 archive (far past every size
    and member limit a LinkedIn export is held to, so refused on those
    grounds), or a directory declaring more members than
    :data:`~netkeeper.linkedin.archive.MAX_MEMBERS`, which ``open_archive``
    refuses as ``too_many_members``. Leaves ``handle`` at its start.
    """
    try:
        found = _read_directory(handle)
    finally:
        handle.seek(0)
    if found is None:
        return
    declared, read = found.declared, found.read
    if read != declared:
        log.warning("%s: zip directory declares %d entries, found %d", name, declared, read)
        found_text = f"only {read} could be read" if read < declared else "more are listed"
        raise ArchiveFormatError(
            f"{name}: the zip's directory is damaged: it declares {declared} files, but "
            f"{found_text}, so some would be silently left out. Download the export again.",
            ArchiveRefusalCode.DAMAGED,
        )
    if found.undecodable or found.mismatched:
        broken = max(found.mismatched, 1)
        raise ArchiveFormatError(
            f"{name}: the zip's directory is damaged: {broken} of its {read} file names no "
            "longer match the files they point at, so those files would be silently left "
            "out. Download the export again.",
            ArchiveRefusalCode.DAMAGED,
        )


@dataclass(frozen=True)
class _Directory:
    """What walking a central directory found."""

    declared: int
    """Records the End Of Central Directory record says there are."""
    read: int
    """Records the walk found."""
    undecodable: bool
    """A name flagged UTF-8 is not."""
    mismatched: int
    """Records whose name is not the one their local file header carries."""


def _read_directory(handle: IO[bytes]) -> _Directory | None:
    """What the central directory holds, or None when this zip is not ours to judge.

    Stops as soon as the walk goes past what the directory declares, or finds a
    record :mod:`zipfile` would itself refuse (a bad signature, a record cut
    short): that zip is ``open_archive``'s to refuse, in its own words.
    """
    handle.seek(0, io.SEEK_END)
    file_size = handle.tell()
    window = min(file_size, _EOCD_FIXED_SIZE + _EOCD_MAX_COMMENT)
    handle.seek(file_size - window)
    tail = handle.read(window)
    index = tail.rfind(_EOCD_SIGNATURE)
    if index == -1 or len(tail) - index < _EOCD_FIXED_SIZE:
        return None
    eocd_at = file_size - window + index
    declared: int
    size: int
    offset: int
    declared, size, offset = struct.unpack_from("<HII", tail, index + 10)
    if declared == _ZIP64_ENTRIES or offset == _ZIP64_OFFSET or declared > MAX_MEMBERS:
        return None
    # Where the directory really starts, allowing for bytes prepended to the zip,
    # as zipfile works it out: it ends where the End Of Central Directory begins.
    start = eocd_at - size
    if start < 0:
        return None
    read = 0
    consumed = 0
    mismatched = 0
    undecodable = False
    handle.seek(start)
    while consumed < size:
        record = handle.read(_CENTRAL_DIR_FIXED_SIZE)
        if len(record) < _CENTRAL_DIR_FIXED_SIZE or record[:4] != _CENTRAL_DIR_SIGNATURE:
            return None
        flags: int = struct.unpack_from("<H", record, 8)[0]
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", record, 28)
        (local_offset,) = struct.unpack_from("<I", record, 42)
        raw_name = handle.read(name_length)
        if flags & _UTF8_FLAG:
            try:
                raw_name.decode("utf-8")
            except UnicodeDecodeError:
                undecodable = True
        after = handle.tell() + extra_length + comment_length
        if _local_name(handle, local_offset + start - offset) != raw_name:
            mismatched += 1
        handle.seek(after)
        consumed += _CENTRAL_DIR_FIXED_SIZE + name_length + extra_length + comment_length
        read += 1
        if read > declared:
            break
    return _Directory(declared, read, undecodable, mismatched)


def _local_name(handle: IO[bytes], at: int) -> bytes | None:
    """The file name the local file header at ``at`` carries, or None when there is none."""
    if at < 0:
        return None
    handle.seek(at)
    header = handle.read(_LOCAL_HEADER_FIXED_SIZE)
    if len(header) < _LOCAL_HEADER_FIXED_SIZE or header[:4] != _LOCAL_HEADER_SIGNATURE:
        return None
    (name_length,) = struct.unpack_from("<H", header, 26)
    return handle.read(name_length)


__all__ = ["check_zip_directory", "open_checked_archive"]
