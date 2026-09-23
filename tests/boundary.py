"""One definition of the extractor boundary, for every test that checks it.

Spec 9.10 and ADR 0005: nothing under ``netkeeper/linkedin/`` imports the ORM, the
session factory, or the CRM. Two mechanisms check that, and they see different
things, so the project keeps both and they read the same list from here:

- a syntax-tree scan over every file under ``linkedin/``
  (``tests/test_browser_safety.py``) reads the imports that are *written down*,
  including the relative spellings, and covers modules that do not exist yet;
- importing each module in a subprocess and looking at ``sys.modules`` reads what
  those imports *drag in*, which is how a forbidden module arrives through a
  harmless-looking helper.

Until P2-01, three test modules carried a copy of the subprocess check each, one per
module they had written, with a list each. This is the union of those lists, and the
sweep in ``test_browser_safety.py`` imports every module under ``linkedin/`` in a
subprocess of its own, so the per-module copies are gone: one list, one mechanism,
nothing to drift.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "netkeeper"
EXTRACTOR = PACKAGE / "linkedin"

#: What ADR 0005 keeps out of the extractor. "sqlalchemy" is not in the ADR's words
#: but follows from them: importing the ORM is how the models arrive. A module under
#: ``linkedin/`` may not import these, or import anything that does.
FORBIDDEN_IMPORTS = (
    "netkeeper.models",
    "netkeeper.crm",
    "netkeeper.db",
    "netkeeper.scoping",
    "sqlalchemy",
)


def is_forbidden(name: str) -> bool:
    """Whether a dotted name is one of the forbidden modules, or lives inside one.

    Matched on dotted segments rather than characters, so a future ``netkeeper.dbg``
    is not read as ``netkeeper.db``.
    """
    return any(
        name == forbidden or name.startswith(f"{forbidden}.") for forbidden in FORBIDDEN_IMPORTS
    )


def extractor_modules() -> list[str]:
    """Every importable module under ``netkeeper/linkedin/``, dotted."""
    modules = []
    for path in sorted(EXTRACTOR.rglob("*.py")):
        parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        modules.append(".".join(parts))
    return modules


def imports_pulled_in_by(module: str) -> list[str]:
    """The forbidden modules that importing ``module`` loads, directly or through others.

    Runs in a subprocess because ``sys.modules`` in this one is already full of the
    database: the test suite imported it long before this call. One module per
    subprocess, so a clean module cannot be vouched for by the company it keeps.
    """
    probe = (
        "import importlib, sys, json;"
        f"importlib.import_module({module!r});"
        "print(json.dumps(sorted(sys.modules)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=PACKAGE.parent,
    )
    if result.returncode != 0:
        raise AssertionError(f"importing {module} failed:\n{result.stderr}")
    loaded: list[str] = json.loads(result.stdout)
    return [name for name in loaded if is_forbidden(name)]


# --- the one mapping module (P2-14) -------------------------------------------------

#: Spec 9.10's "Out" column as it exists so far: what the extractor hands the core.
#: A module that imports one of these modules, or one of these names, handles
#: extractor results. ``ProfileHarvest``, ``InboxDelta``, and ``MessageOutcome`` join
#: with the jobs that return them (P2-07, P4). ``voyager.ConnectionSummary`` is named
#: alone because the rest of ``voyager`` is request plumbing, not results.
RESULT_MODULES = ("netkeeper.linkedin.connections",)
RESULT_TYPES = ("netkeeper.linkedin.voyager.ConnectionSummary",)

#: The modules allowed to turn an extractor result into rows: spec 9.10's
#: ``crm/apply.py``, and nothing else.
MAPPING_MODULES = ("netkeeper/crm/apply.py",)

#: Models a module may name alongside a result without mapping it onto a table.
#: ``User`` is the owner handle every core function takes, not a place results land.
NOT_A_DESTINATION = ("netkeeper.models.User", "netkeeper.models.user.User")


def maps_results(names: set[str], model_names: set[str]) -> bool:
    """Whether a module that imports and reads ``names`` handles a result and names a table.

    ``names`` is what the module imports (``module`` and ``module.member``) and
    every qualified name it reads; ``model_names`` is every mapped model's
    qualified name. Imports decide the first half because a mapper need not
    spell a result's type anywhere but its import: ``page.connections`` names no
    type at all.
    """
    handles = any(_names(name, RESULT_MODULES) or name in RESULT_TYPES for name in names)
    destinations = set(model_names) - set(NOT_A_DESTINATION)
    return handles and any(_names(name, destinations) for name in names)


def _names(name: str, targets: tuple[str, ...] | set[str]) -> bool:
    """``name`` is one of ``targets`` or inside one (``Contact.li_urn``, ``connections.X``)."""
    return any(name == target or name.startswith(f"{target}.") for target in targets)
