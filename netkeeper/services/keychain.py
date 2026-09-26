"""Secrets in the Keychain through ``keyring`` (spec 15; CLAUDE.md "Secrets").

Every secret is stored under the service :data:`SERVICE` with the name
``<user_id>/<name>``, so two users' secrets never share an entry. Nothing here
logs a secret's value; a log line names the entry at most.

On macOS ``keyring`` uses the login Keychain. In the container it uses the file
backend under the data volume (spec 15). Tests install an in-memory backend
(``tests/conftest.py``), so no test touches a real Keychain.
"""

from __future__ import annotations

import logging
from typing import Final

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger(__name__)

SERVICE: Final = "netkeeper"


class KeychainUnavailable(RuntimeError):
    """The Keychain refused, or no usable backend is installed."""


def entry_name(user_id: int, name: str) -> str:
    """The entry's name under :data:`SERVICE`: ``<user_id>/<name>``."""
    if not name or name.startswith("/"):
        raise ValueError(f"a secret needs a relative name, not {name!r}")
    return f"{user_id}/{name}"


def get_secret(user_id: int, name: str) -> str | None:
    """The secret stored as ``name`` for ``user_id``, or None when there is none."""
    try:
        return keyring.get_password(SERVICE, entry_name(user_id, name))
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "read", user_id, name)) from exc


def set_secret(user_id: int, name: str, value: str) -> None:
    """Store ``value`` as ``name`` for ``user_id``, replacing what was there."""
    if not value:
        raise ValueError("refusing to store an empty secret")
    try:
        keyring.set_password(SERVICE, entry_name(user_id, name), value)
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "write", user_id, name)) from exc
    log.info("stored secret %s", entry_name(user_id, name))


def delete_secret(user_id: int, name: str) -> bool:
    """Remove ``name`` for ``user_id``. Returns whether there was one to remove."""
    try:
        keyring.delete_password(SERVICE, entry_name(user_id, name))
    except PasswordDeleteError:
        return False
    except KeyringError as exc:
        raise KeychainUnavailable(_why(exc, "delete", user_id, name)) from exc
    log.info("deleted secret %s", entry_name(user_id, name))
    return True


def _why(exc: KeyringError, verb: str, user_id: int, name: str) -> str:
    # The backend's own message names the backend, never the value.
    return f"could not {verb} {entry_name(user_id, name)} in the Keychain: {exc}"
