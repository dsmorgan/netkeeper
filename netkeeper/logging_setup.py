"""Process-wide logging: one line per record, level from ``NETKEEPER_LOG_LEVEL``."""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

LEVEL_ENV = "NETKEEPER_LOG_LEVEL"
HANDLER_NAME = "netkeeper"
DEFAULT_LEVEL = logging.INFO

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

log = logging.getLogger(__name__)


class _StderrHandler(logging.StreamHandler[TextIO]):
    """Write to whatever ``sys.stderr`` is at emit time.

    Binding the stream lazily (as ``logging.lastResort`` does) means redirection and
    test capture that swap ``sys.stderr`` after setup still see the records.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        """Ignore the stream the base class assigns; the property always reads sys.stderr."""


def setup_logging(level: str | int | None = None) -> None:
    """Configure the root logger. Safe to call more than once.

    ``level`` is a level name (``DEBUG``, ``info``), a number, or None to read
    ``NETKEEPER_LOG_LEVEL``. An unrecognized value logs a warning and uses INFO.
    """
    requested: str | int | None = level if level is not None else os.environ.get(LEVEL_ENV)
    resolved, complaint = _resolve_level(requested)
    root = logging.getLogger()
    if not any(handler.get_name() == HANDLER_NAME for handler in root.handlers):
        handler = _StderrHandler()
        handler.set_name(HANDLER_NAME)
        handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        root.addHandler(handler)
    root.setLevel(resolved)
    if complaint is not None:
        log.warning("%s", complaint)


def _resolve_level(value: str | int | None) -> tuple[int, str | None]:
    """Return the numeric level and, when ``value`` is unusable, a complaint to log."""
    if value is None:
        return DEFAULT_LEVEL, None
    if isinstance(value, int):
        if value >= 0:
            return value, None
        return DEFAULT_LEVEL, f"ignoring invalid log level {value!r}; using INFO"
    text = value.strip()
    if not text:
        return DEFAULT_LEVEL, None
    named = logging.getLevelNamesMapping().get(text.upper())
    if named is not None:
        return named, None
    if text.isdigit():
        return int(text), None
    return DEFAULT_LEVEL, f"ignoring invalid {LEVEL_ENV} {value!r}; using INFO"
