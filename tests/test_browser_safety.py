"""The browser rules a reviewer would otherwise have to hold in their head.

ADR 0002 says netkeeper attaches to a browser the user runs and never starts one,
because a second browser identity on a LinkedIn account is what gets accounts
restricted. Spec 9.1 adds the invariants that keep the attached profile looking like
itself, and spec 9.10 keeps the database out of ``netkeeper/linkedin/``.

Those rules are about code that does not exist rather than behavior that does, so
they are checked by walking the package's syntax tree: the tests below fail on the
line that breaks a rule, whoever writes it and whichever item it lands in. Each
scanner is itself exercised against a snippet that must be caught, because a scanner
that quietly matches nothing would pass forever — and each rule that names one file
also asserts that the file still matches, so renaming it cannot make the rule vacuous.

Two limits are deliberate. The scan covers ``netkeeper/`` and not ``tests/``: test
code starts processes (the boundary probe below runs one) and drives fakes that
imitate the calls the rules forbid. And it reads names, not meanings, so it catches
the spellings a person would actually write, not every way Python can reach a symbol.
The offline behavior tests and the opt-in smoke suite are the other two layers.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import boundary
import pytest
from boundary import is_forbidden

PACKAGE = Path(__file__).resolve().parents[1] / "netkeeper"
REPO_ROOT = PACKAGE.parent
LINKEDIN = PACKAGE / "linkedin"
WEB = PACKAGE / "web"

#: Stands in for a file when a scanner is being shown a snippet.
MEMORY = Path("<memory>")

# Playwright's ways to start a browser process. None of them may appear anywhere.
LAUNCH_CALLS = frozenset({"launch", "launch_persistent_context", "launch_server"})

# Ways to start any process, which is how a browser would be started without
# Playwright. `netkeeper browser launch` prints a command; it never runs one. The
# asyncio spellings matter most: this package is async throughout and already imports
# asyncio, so `create_subprocess_exec` is the shortest path from here to a launch.
PROCESS_MODULES = frozenset({"subprocess", "pty", "webbrowser"})
PROCESS_CALLS = frozenset(
    {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "subprocess_exec",
        "subprocess_shell",
        "Popen",
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
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "posix_spawn",
        "posix_spawnp",
        "startfile",
    }
)

# Spec 9.1: reuse the context that is there, and change nothing about it. A second
# context is a second identity; the rest would make the profile stop looking like the
# profile the user browses with. `new_cdp_session` is the one that matters most: a raw
# CDP session reaches Network.setUserAgentOverride, Emulation.setTimezoneOverride and
# Network.setCookie in one step, past every named method here.
CONTEXT_MUTATORS = frozenset(
    {
        "new_context",
        "new_cdp_session",
        "new_browser_cdp_session",
        "add_init_script",
        "add_script_tag",
        "add_cookies",
        "clear_cookies",
        "expose_function",
        "expose_binding",
        "grant_permissions",
        "route",
        "route_from_har",
        "route_web_socket",
        "unroute",
        "unroute_all",
        "set_extra_http_headers",
        "set_geolocation",
        "set_offline",
        "set_http_credentials",
        "emulate_media",
    }
)

# ADR 0006: netkeeper reads what the page loads and never touches a request. These are
# Playwright's ways to hold one and answer it -- a routed request's `continue_`,
# `fulfill`, and `abort` -- which only exist once something routes, and `route` is
# refused above; they are named here too so a regression is caught at the call that
# alters the request, not only at the one that intercepted it. (`fallback` is
# Playwright's fourth, left off because FallbackContactInfoSource has an attribute of
# that name; `route` and these three already cover every way to reach it.)
REQUEST_MUTATORS = frozenset({"continue_", "fulfill", "abort"})

# ADR 0006: no request of netkeeper's own through Playwright's API request context
# either -- `page.request`, `context.request`, `playwright.request` -- which would send
# with the profile's cookies from outside the page. The attribute `request` alone is
# not refused: an observed response's `.request` is how the observation reads the
# method and body the page sent. What is refused is sending through one.
API_REQUEST_SENDS = frozenset({"fetch", "get", "post", "put", "patch", "delete", "head"})

# ADR 0006: the modules that read the page's own answers only listen and scroll. Nothing
# in them evaluates script in the page, types, clicks, or otherwise drives it: the
# scroll is `BrowserRun.scroll`'s, and ADR 0006's one click, when enrichment adds it,
# is a narrow BrowserRun method of its own, not a call made from these.
OBSERVING_MODULES = (
    LINKEDIN / "observe.py",
    LINKEDIN / "page_connections.py",
    LINKEDIN / "flagship.py",
    LINKEDIN / "flight.py",
)
PAGE_DRIVERS = frozenset(
    {
        "evaluate",
        "evaluate_handle",
        "click",
        "dblclick",
        "tap",
        "fill",
        # `type` (Playwright's deprecated typing call) is left off: it is also the
        # builtin, which every module calls. `fill`, `press`, and `press_sequentially`
        # are the spellings that type into a page today.
        "press",
        "press_sequentially",
        "check",
        "uncheck",
        "select_option",
        "set_input_files",
        "dispatch_event",
        "hover",
        "focus",
        "keyboard",
        "locator",
        "query_selector",
        "query_selector_all",
        "wait_for_selector",
        "add_script_tag",
        "add_style_tag",
        "set_content",
        # Script run in the page by other names: each can call `fetch` as easily as
        # `evaluate` can.
        "wait_for_function",
        "eval_on_selector",
        "eval_on_selector_all",
        "evaluate_all",
        "fetch",
    }
)

# The attach point. Everything else goes through AttachBrowserProvider.
CONNECT_CALL = "connect_over_cdp"
CONNECTOR_MODULE = LINKEDIN / "browser.py"

# Spec 5 and 9.9: a request handler that awaits browser work deadlocks on the tab
# waiting for its own response, so routes enqueue work on the task runner instead.
BROWSER_MODULES = (
    "netkeeper.linkedin.browser",
    "netkeeper.linkedin.preflight",
    # A rehearsal drives a real tab through a whole pacing plan -- minutes of
    # it. Importing it from a route would await browser work inside a request
    # handler exactly as importing the provider would, so it is a browser
    # module here and not only a caller of one below.
    "netkeeper.linkedin.rehearse",
    # The in-page Voyager fetch (#150) awaits a real `page.evaluate` fetch --
    # exactly the deadlock risk the other three exist to keep out of a handler.
    "netkeeper.linkedin.fetch",
    # dom.py's contact-info overlay reader (P2-08, unwired since #187's review
    # removed its connections-list half): a page.evaluate read is exactly as
    # long-running as the in-page Voyager fetch above.
    "netkeeper.linkedin.dom",
    # The run worker (P2-10) attaches and runs a whole run. A route that imported
    # it could await it; routes submit runs to the task runner instead.
    "netkeeper.worker",
    # #187: the connections source that scrolls a real tab and waits on its answers.
    "netkeeper.linkedin.page_connections",
)

# The modules that may reach the provider at all, as paths from the repository root.
# A module that needs it adds itself here on purpose, in a diff someone reads. A
# module under web/ never belongs here; neither does a helper a web module imports,
# which is how a shim under services/ would smuggle the browser into a handler.
BROWSER_CALLERS = frozenset(
    {
        Path("netkeeper/cli.py"),  # `netkeeper preflight` and `rehearse`, on their own loops
        Path("netkeeper/linkedin/preflight.py"),  # the report, inside a run
        Path("netkeeper/linkedin/rehearse.py"),  # the rehearsal, inside a run
        Path("netkeeper/linkedin/fetch.py"),  # PageVoyagerFetch, inside a run (#150)
        Path("netkeeper/linkedin/dom.py"),  # DomContactInfoSource (P2-08)
        Path("netkeeper/linkedin/page_connections.py"),  # PageConnections, inside a run (#187)
        # The run worker (P2-10): takes the lock, attaches, runs a recorded run. Not
        # under web/ or services/, and nothing under either imports it: the app and
        # the runs API hold it only as services.runs.RunExecutor.
        Path("netkeeper/worker.py"),
    }
)

# Roots that must never import the browser directly. services/ is here because a
# helper is the obvious place to put a shim, and #144 is establishing
# services/linkedin_session.py as the core-side extractor helper.
CORE_ROOTS = [WEB, PACKAGE / "crm", PACKAGE / "services"]


@dataclass(frozen=True, slots=True)
class Finding:
    path: Path
    line: int
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.detail}"


def python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def package_of(path: Path) -> str:
    """The package a file's relative imports resolve against."""
    try:
        parts = path.resolve().parent.relative_to(REPO_ROOT).parts
    except (OSError, ValueError):
        parts = ()
    return ".".join(parts) if parts else PACKAGE.name


def called_names(source: str) -> Iterator[tuple[int, str]]:
    """Every called name in ``source``: ``a.b.c()`` yields ``c``, ``f()`` yields ``f``."""
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            yield node.lineno, node.func.attr
        elif isinstance(node.func, ast.Name):
            yield node.lineno, node.func.id


def reached_names(source: str, path: Path = MEMORY) -> Iterator[tuple[int, str]]:
    """Every way ``source`` can reach a name, called or not.

    A deny-list that only reads call sites is answered by one line of indirection,
    so this reads four:

    - a call, as :func:`called_names` sees it;
    - an attribute *reference*, called or not: ``start = p.chromium.launch`` holds
      the method to call later, and ``start()`` alone names nothing on the list;
    - the member of a ``from`` import, whatever it is renamed to:
      ``from os import system as sh`` makes ``sh()`` a process start;
    - a string literal handed to ``getattr`` or ``operator.attrgetter``.

    It cannot see a name built at run time, or one read out of a namespace
    (``vars(os)["system"]``), which is why every rule here is also a review rule,
    not only a test.

    Reading references rather than calls widens the net: an unrelated attribute
    that happens to be *named* ``route`` or ``system`` now fails the build too.
    That is the safe direction. There is deliberately no per-line escape hatch; a
    false positive is settled in review by renaming the attribute or by narrowing
    the list entry, in a diff someone reads.
    """
    yield from called_names(source)
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.Call):
            for arg in _attribute_name_arguments(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for part in arg.value.split("."):
                        yield node.lineno, part


def _attribute_name_arguments(node: ast.Call) -> list[ast.expr]:
    """The arguments that name an attribute, for the calls that take one by string.

    ``getattr(obj, "name", default)`` names it second and only second;
    ``operator.methodcaller("name", *args)`` names it first; ``attrgetter`` takes
    any number, each possibly dotted (``attrgetter("chromium.launch")``).
    """
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name == "getattr" and isinstance(func, ast.Name):
        return node.args[1:2]
    if name == "methodcaller":
        return node.args[:1]
    if name == "attrgetter":
        return list(node.args)
    return []


def imported_names(source: str, path: Path = MEMORY) -> Iterator[tuple[int, str]]:
    """Every imported dotted name: the module, and ``module.member`` for ``from`` imports.

    Relative imports are resolved against the file's own package, so
    ``from ..models import User`` reads as ``netkeeper.models``. Skipping that step
    would leave every rule here answerable with a dot.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(node, package_of(path))
            if base is None:
                continue
            yield node.lineno, base
            for alias in node.names:
                yield node.lineno, f"{base}.{alias.name}"


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """``from .. import x`` in ``netkeeper.linkedin`` is ``netkeeper``."""
    if node.level == 0:
        return node.module
    parts = package.split(".")
    climbed = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    prefix = ".".join(climbed)
    if node.module is None:
        return prefix or None
    return f"{prefix}.{node.module}" if prefix else node.module


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


def launch_calls(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in reached_names(source, path):
        if name in LAUNCH_CALLS:
            yield Finding(path, line, f"{name}() starts a browser (ADR 0002: attach only)")


def process_starts(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in imported_names(source, path):
        if name.split(".")[0] in PROCESS_MODULES:
            yield Finding(path, line, f"imports {name}, which can start a process")
    for line, name in reached_names(source, path):
        if name in PROCESS_CALLS:
            yield Finding(path, line, f"{name}() starts a process")


def context_mutations(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in reached_names(source, path):
        if name in CONTEXT_MUTATORS:
            yield Finding(path, line, f"{name}() changes the user's browser context (spec 9.1)")


def request_mutations(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in reached_names(source, path):
        if name in REQUEST_MUTATORS:
            yield Finding(path, line, f"{name}() answers or alters a request (ADR 0006)")


def api_request_sends(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    """A request of netkeeper's own through an API request context.

    Two readings, so holding the context in a variable is no way around it:

    - any ``.request`` attribute read on something other than a name ``response``
      (``page.request``, ``context.request``, ``playwright.request``). An observed
      response's ``.request`` is how the observation reads what the page sent; every
      other ``.request`` in Playwright is an API request context;
    - ``<anything>.request.<send>``, whatever it is read on.

    ``request.new_context`` is refused by the context rule (``new_context``).
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr == "request" and not (
            isinstance(node.value, ast.Name) and node.value.id == "response"
        ):
            yield Finding(path, node.lineno, "reads an API request context (ADR 0006)")
        if (
            node.attr in API_REQUEST_SENDS
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "request"
        ):
            yield Finding(path, node.lineno, f"request.{node.attr} sends a request (ADR 0006)")
    for line, name in reached_names(source, path):
        if name == "APIRequestContext":
            yield Finding(path, line, "an API request context sends requests (ADR 0006)")


def page_drivers(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in reached_names(source, path):
        if name in PAGE_DRIVERS:
            yield Finding(path, line, f"{name}() drives the page; this module only listens")


def database_imports(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in imported_names(source, path):
        if is_forbidden(name):
            yield Finding(path, line, f"imports {name}: linkedin/ has no database (spec 9.10)")


def browser_imports(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in imported_names(source, path):
        if name.startswith(BROWSER_MODULES):
            yield Finding(path, line, f"imports {name}; enqueue the work on the task runner")


def connect_calls(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in called_names(source):
        if name == CONNECT_CALL:
            yield Finding(path, line, f"{name}() attaches to a browser")


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


def test_no_code_path_alters_or_answers_a_request() -> None:
    """ADR 0006: the page's requests are read, never held, changed, or answered."""
    findings = scan([PACKAGE], request_mutations)
    assert not findings, complain(findings, "netkeeper reads the page's answers only:")


def test_no_code_path_sends_through_an_api_request_context() -> None:
    """ADR 0006: no request of netkeeper's own from outside the page either."""
    findings = scan([PACKAGE], api_request_sends)
    assert not findings, complain(findings, "netkeeper sends no requests of its own:")


def test_the_observing_modules_only_listen_and_scroll() -> None:
    """ADR 0006: the modules that read the page's answers never drive the page."""
    findings: list[Finding] = []
    for path in OBSERVING_MODULES:
        assert path.exists(), f"{path} moved; point OBSERVING_MODULES at its new home"
        findings.extend(page_drivers(path.read_text(encoding="utf-8"), path))
    assert not findings, complain(findings, "an observing module drives the page:")


def test_only_the_connector_opens_a_cdp_connection() -> None:
    """One attach point, so the activity lock cannot be sidestepped."""
    findings = scan([PACKAGE], connect_calls)
    inside = [finding for finding in findings if finding.path == CONNECTOR_MODULE]
    outside = [finding for finding in findings if finding.path != CONNECTOR_MODULE]
    assert inside, (
        f"nothing in {CONNECTOR_MODULE.name} attaches any more. If the connector moved,"
        " point CONNECTOR_MODULE at its new home; the rule is vacuous until you do."
    )
    assert not outside, complain(outside, f"attach through {CONNECTOR_MODULE.name}:")


def test_nothing_under_linkedin_touches_the_database() -> None:
    """Spec 9.10: job specs in, dataclasses out, no session anywhere in the extractor."""
    findings = scan([LINKEDIN], database_imports, at_least=5)
    assert not findings, complain(findings, "the extractor boundary is one-way:")


@pytest.mark.parametrize("module", boundary.extractor_modules())
def test_no_extractor_module_drags_the_database_in(module: str) -> None:
    """The other half of the boundary: what the imports pull in, not what they say.

    A helper that looks harmless and imports the ORM two levels down puts a session in
    the extractor's process just as surely as ``from netkeeper.models import Contact``.

    This is every module under ``linkedin/``, each in a subprocess of its own, and it
    replaced the per-module copies that ``test_linkedin_archive.py``,
    ``test_linkedin_pacing.py`` and ``test_classify.py`` used to carry.
    """
    loaded = boundary.imports_pulled_in_by(module)
    assert not loaded, f"importing {module} loads {', '.join(loaded)} (spec 9.10)"


def test_no_request_handler_can_await_browser_work() -> None:
    """Spec 9.9: a handler that awaits the browser deadlocks on its own response."""
    findings = scan(CORE_ROOTS, browser_imports, at_least=15)
    assert not findings, complain(findings, "routes enqueue browser work, never await it:")


def test_the_browser_modules_have_exactly_these_callers() -> None:
    """Who may reach the provider is a list someone edits, not a thing that drifts.

    A shim under ``services/`` that a route imports would satisfy every other rule
    here and still put browser work inside a request. It cannot satisfy this one.
    """
    callers = {
        finding.path.resolve().relative_to(REPO_ROOT)
        for finding in scan([PACKAGE], browser_imports)
    }
    assert callers == set(BROWSER_CALLERS), (
        "the browser's callers changed.\n"
        f"  now:      {sorted(str(path) for path in callers)}\n"
        f"  expected: {sorted(str(path) for path in BROWSER_CALLERS)}\n"
        "A module that must reach the provider adds itself to BROWSER_CALLERS on"
        " purpose. A request handler never does: it enqueues the work on the task"
        " runner, because awaiting browser work inside a handler deadlocks on the tab"
        " that is waiting for the response (spec 9.9)."
    )


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


def test_process_scanner_catches_the_asyncio_spelling() -> None:
    """The one a module that already imports asyncio would reach for."""
    assert list(
        process_starts(
            "import asyncio\n"
            "async def go():\n"
            "    await asyncio.create_subprocess_exec('open', '-na', 'Google Chrome')\n"
        )
    )
    assert list(process_starts("await asyncio.create_subprocess_shell('open -a Chrome')\n"))
    assert list(process_starts("await loop.subprocess_exec(protocol, 'chrome')\n"))
    assert not list(process_starts("import asyncio\nasyncio.sleep(0)\n"))


def test_the_scanners_see_a_renamed_import() -> None:
    """``as`` is not a way around the list: the import line itself is the finding."""
    spawn = (
        "from asyncio import create_subprocess_exec as spawn\n"
        "async def go():\n"
        "    await spawn('open', '-na', 'Google Chrome')\n"
    )
    assert list(process_starts(spawn))
    assert list(process_starts("from os import system as sh\nsh('open -a Chrome')\n"))
    assert list(launch_calls("from playwright.async_api import launch as go\n"))
    assert not list(process_starts("from asyncio import sleep as nap\n"))


def test_the_scanners_see_a_method_held_before_it_is_called() -> None:
    """Holding the bound method and calling it later names nothing at the call."""
    assert list(launch_calls("start = p.chromium.launch\nawait start()\n"))
    assert list(context_mutations("add = context.add_cookies\nawait add([])\n"))
    assert list(process_starts("run = os.posix_spawn\n"))
    assert not list(launch_calls("start = p.chromium.connect_over_cdp\n"))


def test_the_scanners_see_getattr_with_a_literal() -> None:
    """A literal handed to ``getattr`` or ``attrgetter`` is a reference by another name."""
    assert list(launch_calls("await getattr(p.chromium, 'launch')()\n"))
    assert list(context_mutations("getattr(context, 'new_cdp_session')(page)\n"))
    assert list(process_starts("operator.attrgetter('system')(os)('open -a Chrome')\n"))
    assert list(launch_calls("operator.methodcaller('launch')(p.chromium)\n"))
    assert list(launch_calls("operator.attrgetter('chromium.launch')(p)()\n"))
    assert list(process_starts("os.spawnlp(os.P_NOWAIT, 'open', 'open', '-a', 'Chrome')\n"))
    assert not list(launch_calls("getattr(page, 'url')\n"))
    # getattr names its attribute second; a default that happens to spell a listed
    # name is a value, not a reference.
    assert not list(launch_calls("getattr(obj, 'x', 'launch')\n"))


def test_context_scanner_catches_a_mutation() -> None:
    assert list(context_mutations("await browser.new_context()\n"))
    assert list(context_mutations("await context.add_init_script('x')\n"))
    assert not list(context_mutations("await context.new_page()\n"))


def test_context_scanner_catches_a_raw_cdp_session() -> None:
    """The escape hatch that reaches every override at once."""
    assert list(context_mutations("session = await context.new_cdp_session(page)\n"))
    assert list(context_mutations("session = await browser.new_browser_cdp_session()\n"))
    assert list(context_mutations("await context.route_from_har('x.har')\n"))
    assert list(context_mutations("await context.expose_function('f', f)\n"))


def test_the_request_scanner_catches_an_interception() -> None:
    """The ways a request is held or answered: each spelling a regression would use."""
    assert list(context_mutations("await page.route('**/*', handler)\n"))
    assert list(context_mutations("await context.route('**/pagination', handler)\n"))
    assert list(context_mutations("await page.set_extra_http_headers({'x': 'y'})\n"))
    assert list(context_mutations("await page.route_web_socket('wss://x', ws)\n"))
    assert list(context_mutations("await page.unroute_all()\n"))
    assert list(context_mutations("await ctx.set_http_credentials({'username': 'x'})\n"))
    assert list(request_mutations("await route.continue_(post_data='{}')\n"))
    assert list(request_mutations("await route.fulfill(body='x')\n"))
    assert list(request_mutations("await route.abort()\n"))
    assert list(request_mutations("go = route.continue_\nawait go()\n"))
    assert list(request_mutations("getattr(route, 'fulfill')(body='x')\n"))
    assert not list(request_mutations("method = response.request.method\n"))


def test_the_api_request_scanner_catches_a_send() -> None:
    assert list(api_request_sends("await page.request.post(url, data=body)\n"))
    assert list(api_request_sends("await context.request.fetch(url)\n"))
    assert list(api_request_sends("await self._page.request.get(url)\n"))
    assert list(api_request_sends("from playwright.async_api import APIRequestContext\n"))
    assert list(api_request_sends("api = page.context.request\nawait api.post(u)\n"))
    assert list(api_request_sends("api = self._run.context.request\n"))
    assert list(api_request_sends("rc = await playwright.request.new_context()\n"))
    assert list(context_mutations("rc = await playwright.request.new_context()\n"))
    assert not list(api_request_sends("body = response.request.post_data\n"))
    assert not list(api_request_sends("method = response.request.method\n"))


def test_the_page_driver_scanner_catches_a_click_and_an_evaluate() -> None:
    assert list(page_drivers("await page.click('text=Contact info')\n"))
    assert list(page_drivers("await page.evaluate('fetch(u)')\n"))
    assert list(page_drivers("await page.locator('a').click()\n"))
    assert list(page_drivers("await page.keyboard.press('End')\n"))
    assert list(page_drivers("await page.wait_for_function('fetch(u)')\n"))
    assert list(page_drivers("await page.eval_on_selector('a', 'e => e.click()')\n"))
    assert list(page_drivers("await page.eval_on_selector_all('a', 'es => 1')\n"))
    assert list(page_drivers("await locator.evaluate_all('es => fetch(u)')\n"))
    assert not list(page_drivers("await run.scroll(plan)\n"))
    assert not list(page_drivers("page.on('response', handler)\n"))


def test_the_forbidden_matcher_reads_dotted_segments() -> None:
    """Shared by both mechanisms now, so it gets its own case.

    Matching characters rather than segments would read a future ``netkeeper.dbg`` as
    ``netkeeper.db`` and fail a rule nobody broke.
    """
    assert is_forbidden("netkeeper.db")
    assert is_forbidden("netkeeper.models.contacts")
    assert is_forbidden("sqlalchemy.orm")
    assert not is_forbidden("netkeeper.dbg")
    assert not is_forbidden("netkeeper.linkedin.browser")


def test_database_scanner_catches_an_import() -> None:
    assert list(database_imports("from netkeeper.models import Contact\n"))
    assert list(database_imports("import sqlalchemy\n"))
    assert list(database_imports("from netkeeper.crm.tags import list_tags\n"))
    assert not list(database_imports("from netkeeper.linkedin.browser import BrowserRun\n"))


def test_the_scanners_resolve_a_relative_import() -> None:
    """A dot is not a way out of the rules."""
    assert list(database_imports("from ..models import User\n", LINKEDIN / "browser.py"))
    assert list(database_imports("from ..crm.tags import list_tags\n", LINKEDIN / "dom.py"))
    assert list(
        browser_imports("from ...linkedin.browser import Attach\n", WEB / "api" / "__init__.py")
    )
    assert not list(database_imports("from .browser import BrowserRun\n", LINKEDIN / "dom.py"))
    assert not list(database_imports("from . import archive\n", LINKEDIN / "conversations.py"))


def test_browser_import_scanner_catches_an_import() -> None:
    assert list(browser_imports("from netkeeper.linkedin.browser import AttachBrowserProvider\n"))
    assert not list(browser_imports("from netkeeper.services.tasks import TaskRunner\n"))


def test_every_browser_module_is_one_the_scanner_would_catch() -> None:
    """Each entry in BROWSER_MODULES, shown an import of itself.

    The rules above only bite when something actually imports one of these, so
    an entry that is spelled wrong -- or missing -- passes every test in this
    file while guarding nothing. A rehearsal is the case that made this worth
    writing: it drives a real tab through a whole pacing plan, minutes of it,
    so a future ``netkeeper/web/api/rehearse.py`` importing it would await
    browser work inside a request handler exactly as importing the provider
    would (spec 9.9).
    """
    for module in BROWSER_MODULES:
        assert list(browser_imports(f"from {module} import thing\n")), module
        assert list(browser_imports(f"import {module}\n")), module
    assert "netkeeper.linkedin.rehearse" in BROWSER_MODULES
    assert not list(browser_imports("from netkeeper.linkedin.pacing import human_delay\n"))


def test_connect_scanner_catches_an_attach() -> None:
    assert list(connect_calls("browser = await pw.chromium.connect_over_cdp(url)\n"))
    assert not list(connect_calls("browser = await pw.chromium.connect(url)\n"))
