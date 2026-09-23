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

P2-03's classification work (#143/#144) carries its own copy of the subprocess check
for a single module, with its own list. This list is the union of the two, and
``netkeeper.crm`` came from theirs. Whichever branch lands second should delete that
copy and read this one instead: the check here covers every module under
``linkedin/``, theirs included.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "netkeeper"
EXTRACTOR = PACKAGE / "linkedin"

#: Import prefixes that put a database on the wrong side of the boundary. A module
#: under ``linkedin/`` may not import these, or import anything that does.
FORBIDDEN_IMPORTS = (
    "netkeeper.models",
    "netkeeper.crm",
    "netkeeper.db",
    "netkeeper.scoping",
    "sqlalchemy",
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
    database: the test suite imported it long before this call.
    """
    probe = (
        "import importlib, sys, json;"
        f"importlib.import_module({module!r});"
        f"print(json.dumps(sorted(name for name in sys.modules"
        f" if name.startswith({FORBIDDEN_IMPORTS!r}))))"
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
    return loaded
