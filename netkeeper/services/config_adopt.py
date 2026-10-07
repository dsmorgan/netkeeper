"""``netkeeper config adopt``: move what ``config.toml`` sets onto the Settings page (#343).

A one-time helper. For each key the file sets that the Settings page can change, the
value is checked the way the page checks it: one above its hard maximum is stored at
the maximum (and reported); one the page cannot hold otherwise (below its minimum, the
wrong type, a window that is not a window) stays in the file, and is reported. Keys the
page never changes (``campaigns.linkedin_auto_send``, ``web.*``, pacing, ...) stay too.

The file is edited line by line, so its comments and layout survive: each moved key's
line (and the continuation lines of a value that spans several) is removed, and nothing
else. The edit is checked by parsing the result: it must be the original minus exactly
the moved keys. When it is not (a key written as a dotted key or an inline table, for
example), :class:`AdoptError` says so and nothing is changed. Pure: text in, a plan out;
the command writes the rows, the backup and the file.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from netkeeper.config import Settings
from netkeeper.models import JsonValue
from netkeeper.services import ui_settings

_TABLE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\s-]+?)\s*\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


#: The keys whose file value above the hard maximum is stored at the maximum: lowering
#: them is the safer direction (fewer LinkedIn actions, fewer emails, more weekend
#: damping). For any other key a clamp would loosen something or change what it does
#: (shorter spacing, a shorter guard, a slower poll, fewer backups), so such a value
#: stays in the file and is reported, as a value below the minimum is.
CLAMP_SAFELY: Final = frozenset(
    {
        "linkedin.budget.connection_pages_per_day",
        "linkedin.budget.profile_visits_per_day",
        "linkedin.budget.profile_visits_per_week",
        "linkedin.budget.inbox_polls_per_day",
        "linkedin.budget.li_prefills_per_day",
        "linkedin.budget.li_messages_auto_per_day",
        "campaigns.mailbox_daily_cap",
        "linkedin.weekend_multiplier",
    }
)


class AdoptError(ValueError):
    """The file cannot be edited safely; nothing was changed."""


@dataclass(frozen=True, slots=True)
class AdoptPlan:
    moved: dict[str, JsonValue] = field(default_factory=dict)
    """Keys to store as Settings-page values, with the value to store."""
    clamped: dict[str, tuple[Any, JsonValue]] = field(default_factory=dict)
    """Keys whose file value was above the hard maximum: (the file's, the stored)."""
    kept: dict[str, str] = field(default_factory=dict)
    """Editable keys left in the file, and why."""
    new_text: str = ""
    """The file without the moved keys."""


def plan(text: str, settings: Settings) -> AdoptPlan:
    """What adopting ``text`` (the file ``settings`` was loaded from) would do."""
    raw = tomllib.loads(text)
    file_keys = settings.file_keys or frozenset()
    moved: dict[str, JsonValue] = {}
    clamped: dict[str, tuple[Any, JsonValue]] = {}
    kept: dict[str, str] = {}
    for spec in ui_settings.FIELDS:
        if spec.key not in file_keys or not spec.editable:
            continue
        value = _lookup(raw, spec.key)
        try:
            moved[spec.key] = ui_settings.to_json(ui_settings.parse(spec, value))
        except ValueError as exc:
            if (
                spec.key in CLAMP_SAFELY
                and spec.maximum is not None
                and isinstance(value, int | float)
                and not isinstance(value, bool)
                and value > spec.maximum
            ):
                top: JsonValue = spec.maximum if spec.kind == "float" else int(spec.maximum)
                moved[spec.key] = top
                clamped[spec.key] = (value, top)
            else:
                kept[spec.key] = str(exc)
    new_text = remove_keys(text, set(moved))
    return AdoptPlan(moved=moved, clamped=clamped, kept=kept, new_text=new_text)


def remove_keys(text: str, keys: set[str]) -> str:
    """``text`` without the lines that set ``keys``; AdoptError unless that is exactly what
    parsing the result shows."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    table = ""
    skipping = 0  # open brackets of a removed multi-line value
    for line in lines:
        if skipping:
            skipping += _depth(line)
            continue
        header = _TABLE.match(line)
        if header and not line.lstrip().startswith("[["):
            table = re.sub(r"\s+", "", header.group(1))
            out.append(line)
            continue
        key = _KEY.match(line)
        if key is not None:
            dotted = f"{table}.{key.group(1)}" if table else key.group(1)
            if dotted in keys:
                skipping = max(_depth(line.split("=", 1)[1]), 0)
                continue
        out.append(line)
    new_text = "".join(out)
    expected = tomllib.loads(text)
    for moved in keys:
        _drop(expected, moved)
    try:
        actual = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise AdoptError(f"the edited file would not parse: {exc}") from exc
    if actual != expected:
        raise AdoptError(
            "these keys are written in a way this command cannot remove line by line"
            " (a dotted key or an inline table): " + ", ".join(sorted(keys))
        )
    return new_text


def _depth(text: str) -> int:
    """Open minus closed brackets outside strings and comments."""
    depth = 0
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == "#":
            break
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
    return depth


def _lookup(raw: Mapping[str, Any], key: str) -> Any:
    node: Any = raw
    for part in key.split("."):
        node = node[part]
    return node


def _drop(raw: dict[str, Any], key: str) -> None:
    *parents, last = key.split(".")
    node = raw
    for part in parents:
        node = node[part]
    del node[last]


class FileChanged(AdoptError):
    """The file changed after it was read; nothing was written to it."""


def write_backup(path: Path, now: datetime) -> Path:
    """Copy ``path`` to :func:`backup_path`, created exclusively (an existing file of that
    name is an AdoptError, never overwritten), synced, with the original's mode."""
    backup = backup_path(path, now)
    data = path.read_bytes()
    try:
        with backup.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
    except FileExistsError as exc:
        raise AdoptError(f"{backup} already exists") from exc
    backup.chmod(stat.S_IMODE(path.stat().st_mode))
    return backup


def backup_path(path: Path, now: datetime) -> Path:
    """Where the backup goes: beside ``path``, named to the microsecond. AdoptError when
    that name is taken, so a backup is never overwritten."""
    backup = path.with_name(f"{path.name}.{now:%Y%m%d-%H%M%S-%f}.bak")
    if backup.exists():
        raise AdoptError(f"{backup} already exists")
    return backup


def rewrite(path: Path, text: str, *, expected: str) -> None:
    """Replace ``path``'s contents with ``text`` atomically: a temporary file in the same
    directory, flushed and synced, with the original's mode, then ``os.replace``, then the
    directory synced. The file is read again first; :class:`FileChanged` if it is no
    longer ``expected``. On any error before the rename the file is as it was. Only the
    mode carries over: the file's owner, group, ACLs and extended attributes are the new
    file's, not the original's."""
    if path.read_text(encoding="utf-8") != expected:
        raise FileChanged(f"{path} changed since it was read")
    mode = stat.S_IMODE(path.stat().st_mode)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        Path(temporary).chmod(mode)
        Path(temporary).replace(path)  # os.replace: atomic within one directory
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    """Make the rename durable. Best effort: the file is already replaced, so a failure
    here (a file system that cannot sync a directory) must not read as "not changed"."""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)
