"""Which users' settings ``serve`` could not read, for the person to see (#464).

``serve``'s scheduler, mailbox monitor and reply poll read each user's own Settings-page
values as they run, and skip that user when a read fails. A failure that persists would
otherwise show only in the log, so each read goes through :meth:`ReadFailures.watching`
and the posture report names what is failing for the user. In memory: a restart starts clear.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager


class ReadFailures:
    """The settings reads that last failed, per user. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failing: dict[int, dict[str, str]] = {}

    @contextmanager
    def watching(self, user_id: int, what: str) -> Iterator[None]:
        """Record a failure of ``what`` for ``user_id`` and re-raise it; clear it on success."""
        try:
            yield
        except Exception as exc:
            with self._lock:
                self._failing.setdefault(user_id, {})[what] = type(exc).__name__
            raise
        with self._lock:
            failing = self._failing.get(user_id)
            if failing is not None:
                failing.pop(what, None)
                if not failing:
                    del self._failing[user_id]

    def describe(self, user_id: int) -> list[str]:
        """One sentence per read of ``user_id`` that is failing, or none."""
        with self._lock:
            failing = dict(self._failing.get(user_id, {}))
        return [
            f"netkeeper serve cannot read your {what} setting ({error}), so it skips that"
            " for you until it can"
            for what, error in sorted(failing.items())
        ]
