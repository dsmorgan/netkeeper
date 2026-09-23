"""The browser rules a reviewer would otherwise have to hold in their head.

ADR 0002 says netkeeper attaches to a browser the user runs and never starts one,
because a second browser identity on a LinkedIn account is what gets accounts
restricted. Spec 9.1 adds the invariants that keep the attached profile looking like
itself, and spec 9.10 keeps the database out of ``netkeeper/linkedin/``.

Those rules are about code that does not exist rather than behavior that does, so
they are checked by walking the package's syntax tree: the tests below fail on the
line that breaks a rule, whoever writes it and whichever item it lands in. Each
scanner is itself exercised against a snippet that must be caught, because a scanner
that quietly matches nothing would pass forever.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "netkeeper"
LINKEDIN = PACKAGE / "linkedin"
WEB = PACKAGE / "web"

# Playwright's ways to start a browser process. None of them may appear anywhere.
LAUNCH_CALLS = frozenset({"launch", "launch_persistent_context", "launch_server"})

# Ways to start any process, which is how a browser would be started without
# Playwright. `netkeeper browser launch` prints a command; it never runs one.
PROCESS_MODULES = frozenset({"subprocess", "pty", "webbrowser"})
PROCESS_CALLS = frozenset(
    {
        "system",
        "popen",
        "fork",
        "forkpty",
        "execl",
        "execle",
        "execlp",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnv",
        "spawnve",
        "spawnvp",
        "posix_spawn",
        "posix_spawnp",
        "startfile",
    }
)

# Spec 9.1: reuse the context that is there, and change nothing about it. A second
# context is a second identity; the rest would make the profile stop looking like the
# profile the user browses with.
CONTEXT_MUTATORS = frozenset(
    {
        "new_context",
        "add_init_script",
        "add_cookies",
        "clear_cookies",
        "route",
        "unroute",
        "set_extra_http_headers",
        "set_user_agent",
        "set_geolocation",
        "emulate_media",
    }
)

# Spec 9.10 / ADR 0005: job specs in, dataclasses out. P2-14 generalizes this to the
# whole contract; until then these are the imports that would break it first.
DATABASE_IMPORTS = ("netkeeper.models", "netkeeper.db", "netkeeper.scoping", "sqlalchemy")

# The attach point. Everything else goes through AttachBrowserProvider.
CONNECT_CALL = "connect_over_cdp"
CONNECTOR_MODULE = LINKEDIN / "browser.py"

# Spec 5 and 9.9: a request handler that awaits browser work deadlocks on the tab
# waiting for its own response, so routes enqueue work on the task runner instead.
BROWSER_MODULES = ("netkeeper.linkedin.browser", "netkeeper.linkedin.preflight")


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def called_names(source: str) -> Iterator[tuple[int, str]]:
    """Every called name in ``source``: ``a.b.c()`` yields ``c``, ``f()`` yields ``f``."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            yield node.lineno, node.func.attr
        elif isinstance(node.func, ast.Name):
            yield node.lineno, node.func.id


def imported_names(source: str) -> Iterator[tuple[int, str]]:
    """Every imported dotted name: the module, and ``module.member`` for ``from`` imports."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.lineno, node.module
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def scan(
    roots: list[Path], check: Callable[[str, Path], Iterator[Finding]], *, at_least: int = 20
) -> list[Finding]:
    """Run ``check`` over every Python file under ``roots``.

    ``at_least`` is the "did this scan look at anything" guard: a rule whose files
    moved would otherwise pass by scanning an empty list.
    """
    findings: list[Finding] = []
    files = [path for root in roots for path in python_files(root)]
    assert len(files) >= at_least, f"the scan found only {len(files)} files; is the path right?"
    for path in files:
        findings.extend(check(path.read_text(encoding="utf-8"), path))
    return findings


def launch_calls(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in called_names(source):
        if name in LAUNCH_CALLS:
            yield Finding(path, line, f"{name}() starts a browser (ADR 0002: attach only)")


def process_starts(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in imported_names(source):
        if name.split(".")[0] in PROCESS_MODULES:
            yield Finding(path, line, f"imports {name}, which can start a process")
    for line, name in called_names(source):
        if name in PROCESS_CALLS:
            yield Finding(path, line, f"{name}() starts a process")


def context_mutations(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in called_names(source):
        if name in CONTEXT_MUTATORS:
            yield Finding(path, line, f"{name}() changes the user's browser context (spec 9.1)")


def database_imports(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in imported_names(source):
        if name.startswith(DATABASE_IMPORTS):
            yield Finding(path, line, f"imports {name}: linkedin/ has no database (spec 9.10)")


def browser_imports(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in imported_names(source):
        if name.startswith(BROWSER_MODULES):
            yield Finding(path, line, f"imports {name}; enqueue the work on the task runner")


def connect_calls(source: str, path: Path = Path("<memory>")) -> Iterator[Finding]:
    for line, name in called_names(source):
        if name == CONNECT_CALL:
            yield Finding(path, line, f"{name}() outside the one connector")


def complain(findings: list[Finding], rule: str) -> str:
    return f"{rule}\n" + "\n".join(str(finding) for finding in findings)


# --- the rules ---------------------------------------------------------------


def test_no_code_path_launches_a_browser() -> None:
    """ADR 0002's whole point: nothing in the package starts a browser."""
    findings = scan([PACKAGE], launch_calls)
    assert not findings, complain(findings, "netkeeper may only attach to a browser:")


def test_no_code_path_starts_a_process() -> None:
    """`netkeeper browser launch` prints a command. Nothing runs one."""
    findings = scan([PACKAGE], process_starts)
    assert not findings, complain(findings, "netkeeper starts no processes:")


def test_the_users_browser_context_is_never_mutated() -> None:
    """Spec 9.1: reuse contexts[0] as it is; no second context, no cookies, no UA."""
    findings = scan([PACKAGE], context_mutations)
    assert not findings, complain(findings, "the attached context is the user's:")


def test_only_the_connector_opens_a_cdp_connection() -> None:
    """One attach point, so the activity lock cannot be sidestepped."""
    findings = [
        finding for finding in scan([PACKAGE], connect_calls) if finding.path != CONNECTOR_MODULE
    ]
    assert not findings, complain(findings, f"attach through {CONNECTOR_MODULE.name}:")


def test_nothing_under_linkedin_touches_the_database() -> None:
    """Spec 9.10: job specs in, dataclasses out, no session anywhere in the extractor."""
    findings = scan([LINKEDIN], database_imports, at_least=5)
    assert not findings, complain(findings, "the extractor boundary is one-way:")


def test_no_request_handler_can_await_browser_work() -> None:
    """Spec 9.9: a handler that awaits the browser deadlocks on its own response."""
    findings = scan([WEB, PACKAGE / "crm"], browser_imports, at_least=15)
    assert not findings, complain(findings, "routes enqueue browser work, never await it:")


# --- the scanners themselves --------------------------------------------------
# A scanner that matched nothing would pass every test above forever, so each one
# is shown a snippet it has to catch.


def test_launch_scanner_catches_a_launch() -> None:
    assert list(launch_calls("async def go(p):\n    return await p.chromium.launch()\n"))
    assert list(launch_calls("from playwright.sync_api import x\nx.launch_persistent_context()\n"))
    assert not list(launch_calls("browser.contexts[0]\n"))


def test_process_scanner_catches_a_spawn() -> None:
    assert list(process_starts("import subprocess\n"))
    assert list(process_starts("from subprocess import Popen\n"))
    assert list(process_starts("import os\nos.system('open -a Chrome')\n"))
    assert not list(process_starts("import os\nos.environ.get('X')\n"))


def test_context_scanner_catches_a_mutation() -> None:
    assert list(context_mutations("await browser.new_context()\n"))
    assert list(context_mutations("await context.add_init_script('x')\n"))
    assert not list(context_mutations("await context.new_page()\n"))


def test_database_scanner_catches_an_import() -> None:
    assert list(database_imports("from netkeeper.models import Contact\n"))
    assert list(database_imports("import sqlalchemy\n"))
    assert not list(database_imports("from netkeeper.linkedin.browser import BrowserRun\n"))


def test_browser_import_scanner_catches_an_import() -> None:
    assert list(browser_imports("from netkeeper.linkedin.browser import AttachBrowserProvider\n"))
    assert not list(browser_imports("from netkeeper.services.tasks import TaskRunner\n"))
