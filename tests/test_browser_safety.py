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

# ADR 0006's amendment for #200: the one CDP session in the package. `new_cdp_session`
# stays on the list above -- a raw session can reach every mutator there is -- and is
# allowed at exactly one site, the body tap's, whose session may send exactly the
# read-only methods below and nothing else (`test_the_one_cdp_session_is_read_only`).
ALLOWED_CONTEXT_MUTATIONS = frozenset(
    {(LINKEDIN / "browser.py", "BrowserRun._open_body_tap", "new_cdp_session")}
)
#: What the body tap's session may send, each named as a literal at its one call,
#: with the only params keys it may pass (a dict literal):
#:
#: - ``Network.enable``: the session hears the tab's network events. It alters no
#:   request and nothing the page can see; Playwright's own session already sends it.
#: - ``Network.streamResourceContent``: Chrome forwards an answer's data to this
#:   session as it arrives. It alters, blocks, delays, and adds no request.
READ_ONLY_CDP_METHODS: dict[str, frozenset[str]] = {
    "Network.enable": frozenset({"maxTotalBufferSize", "maxResourceBufferSize"}),
    "Network.streamResourceContent": frozenset({"requestId"}),
}
#: The one function whose ``send`` calls reach a CDP session, and how many it makes.
CDP_SENDERS = {(LINKEDIN / "browser.py", "BrowserRun._open_body_tap"): 2}
#: The only observations that open the body tap, each once (ADR 0006's amendment):
#: the connections sync's (#200) and each enrichment visit's, whose tap streams only
#: its lazy cards and the Contact info overlay (#203).
TAP_OBSERVERS = frozenset(
    {
        (LINKEDIN / "page_connections.py", "PageConnections._land"),
        (LINKEDIN / "page_profiles.py", "PageProfiles.open_profile"),
    }
)
#: The one place the tap's opener is reached from: the observation that asked for it.
TAP_OPENER = (LINKEDIN / "browser.py", "BrowserRun.observe")

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
# in them evaluates script in the page, types, clicks, locates, or otherwise drives it:
# the scroll is `BrowserRun.scroll`'s, and ADR 0006's one click is
# `BrowserRun.click_contact_info`'s (#190), a narrow method of its own that these call by
# that name and nothing else.
OBSERVING_MODULES = (
    LINKEDIN / "observe.py",
    LINKEDIN / "body_tap.py",
    LINKEDIN / "page_connections.py",
    LINKEDIN / "flagship.py",
    LINKEDIN / "flight.py",
    LINKEDIN / "page_profiles.py",
    LINKEDIN / "flagship_profile.py",
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
        "get_by_role",
        "get_by_text",
        "get_by_label",
        "get_by_placeholder",
        "get_by_alt_text",
        "get_by_title",
        "get_by_test_id",
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

# ADR 0006's one exception to "navigation and the scroll are the only input": one click
# on Contact info per profile visit (#190). Every way Playwright gives a page input --
# a click, a key, a tap, a hover, typing, clearing a field, a blur, a checkbox, a select,
# a synthetic event, a drag, a programmatic scroll into view -- is refused anywhere in the
# package, except the single `click` call inside `BrowserRun.click_contact_info`. `type`
# counts only as an attribute (Playwright's `locator.type`); the builtin `type(x)` is a
# bare name and is not read.
#
# Like every rule here, this reads names, not meanings: deliberate obfuscation (a name
# built at run time, a method read out of a namespace) is a limit of any static scan, and
# is what review, the offline tests, and the smoke suite are for.
INPUT_CALLS = frozenset(
    {
        "click",
        "dblclick",
        "tap",
        "fill",
        "type",
        "press",
        "press_sequentially",
        "insert_text",
        "check",
        "uncheck",
        "set_checked",
        "select_option",
        "select_text",
        "set_input_files",
        "dispatch_event",
        "hover",
        "focus",
        "drag_to",
        "drag_and_drop",
        "keyboard",
        "touchscreen",
        "down",
        "up",
        "move",
        "clear",
        "blur",
        "scroll_into_view_if_needed",
    }
)
INPUT_ROOTS = [PACKAGE]
#: Two names the core also uses for its own things -- an event's and a column's `type`,
#: heat's `clear` -- read only where a page can be reached: the extractor and the worker.
#: Nothing outside those may import the browser (the browser-callers rule below), so a
#: page is never in reach there to type into or clear.
BROWSER_ONLY_INPUTS = frozenset({"type", "clear"})
#: ``cli.py`` is a browser caller (``preflight``, ``rehearse``, ``run``), so a page is in
#: reach there too (#196 item 2).
BROWSER_ROOTS = (LINKEDIN, PACKAGE / "worker.py", PACKAGE / "cli.py")
#: The only places a page input is allowed: (file, enclosing function, name). Each must
#: be reached exactly once.
#:
#: - ADR 0006's one click on Contact info (#190).
#: - #192's pointer rest: ``mouse.move`` to a bare point over the content before the
#:   first wheel replay on a tab, never a click or a hover resolved against an element.
#:   This one rule replaces #192's separate ``mouse_move_sites`` scanner, which checked
#:   the same thing (``move`` only inside that method) with a narrower reading; this
#:   one reads every ``.move`` reference in the package, not only one on a ``mouse``.
#:   The geometry read that method makes (``_content_box``'s ``bounding_box``) is not a
#:   page input and is not listed.
#: - Not a page input at all: ``cli.py`` reads a run event's ``type`` while it prints a
#:   run's progress (#196 item 2). Listed by function, so a ``type`` anywhere else in
#:   ``cli.py`` is still a finding.
ALLOWED_INPUTS = frozenset(
    {
        (LINKEDIN / "browser.py", "BrowserRun.click_contact_info", "click"),
        (LINKEDIN / "browser.py", "BrowserRun._rest_pointer_over_content", "move"),
        (PACKAGE / "cli.py", "_execute_printing.show", "type"),
    }
)

# Script in the page, by any of Playwright's names for it. Each can call `fetch` or
# `click()` as easily as a click can: refused anywhere in the package except preflight's
# one read of the blank tab's `navigator` fingerprint (spec 9.1), which navigates nowhere.
SCRIPT_CALLS = frozenset(
    {
        "evaluate",
        "evaluate_handle",
        "evaluate_all",
        "eval_on_selector",
        "eval_on_selector_all",
        "wait_for_function",
        "add_script_tag",
        "add_init_script",
        "expose_function",
        "expose_binding",
    }
)
ALLOWED_SCRIPTS = frozenset({(LINKEDIN / "preflight.py", "_read_fingerprint", "evaluate")})

#: Where ``super().request(...)`` may appear, and how many times: the Gmail
#: transport's overrides of ``httplib2``'s ``request`` methods, which call the
#: method they override (#267). No browser is in reach there. Any other
#: ``.request`` read, in this file or elsewhere, is still a finding.
SUPER_REQUEST = "calls the request it overrides"
ALLOWED_SUPER_REQUESTS = {PACKAGE / "campaigns" / "gmail.py": 3}

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
    # The run worker (P2-10) attaches and runs a whole run. A route that imported
    # it could await it; routes submit runs to the task runner instead.
    "netkeeper.worker",
    # #187: the connections source that scrolls a real tab and waits on its answers.
    "netkeeper.linkedin.page_connections",
    # #190: the profile source that scrolls, clicks Contact info, and waits on answers.
    "netkeeper.linkedin.page_profiles",
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
        Path("netkeeper/linkedin/page_connections.py"),  # PageConnections, inside a run (#187)
        Path("netkeeper/linkedin/page_profiles.py"),  # PageProfiles, inside a run (#190)
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
    if name in ("getattr", "getattr_static"):
        return node.args[1:2]
    if name == "methodcaller":
        return node.args[:1]
    if name in ("attrgetter", "__getattribute__"):
        # ``x.__getattribute__("name")`` names it first; ``object.__getattribute__(x,
        # "name")`` second: every argument is read, and only a string is a name.
        return list(node.args)
    return []


#: The callables that read an attribute named by a value (#196 item 11).
DYNAMIC_ATTRIBUTE_READERS = frozenset(
    {"getattr", "getattr_static", "__getattribute__", "methodcaller", "attrgetter"}
)


def dynamic_attribute_reads(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    """Every attribute read whose name the scanners cannot see (#207 review, #196 item 11).

    The scanners read a name handed to ``getattr``, ``__getattribute__``,
    ``methodcaller``, or ``attrgetter`` only as a string literal: ``getattr(x, "obs" +
    "erve")`` reaches ``observe`` and names nothing. So where a page is in reach, each
    of those is a finding when its name is not a literal (an expression, a ``*``
    argument, no name at all), and when the reader itself is held rather than called
    (``read = getattr``), since nothing on the later call says it is one.
    """
    tree = ast.parse(source)
    called: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in DYNAMIC_ATTRIBUTE_READERS:
            continue
        called.add(id(func))
        names = _attribute_name_arguments(node)
        literal = [a for a in names if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        starred = any(isinstance(a, ast.Starred) for a in node.args)
        if name == "__getattribute__":
            ok = len(literal) == 1 and not starred
        else:
            ok = bool(names) and len(literal) == len(names) and not starred
        if not ok:
            yield Finding(path, node.lineno, f"{name}() with a name that is not a literal")
    for node in ast.walk(tree):
        if id(node) in called:
            continue
        if isinstance(node, ast.Name) and node.id in DYNAMIC_ATTRIBUTE_READERS:
            yield Finding(path, node.lineno, f"{node.id} held, not called")
        elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_ATTRIBUTE_READERS:
            yield Finding(path, node.lineno, f"{node.attr} held, not called")


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
        if node.attr == "request" and _is_super_call(node.value):
            yield Finding(path, node.lineno, SUPER_REQUEST)
        elif node.attr == "request" and not (
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


def _is_super_call(node: ast.expr) -> bool:
    """``super()``, with no arguments."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "super"
        and not node.args
        and not node.keywords
    )


def page_drivers(source: str, path: Path = MEMORY) -> Iterator[Finding]:
    for line, name in reached_names(source, path):
        if name in PAGE_DRIVERS:
            yield Finding(path, line, f"{name}() drives the page; this module only listens")


@dataclass(frozen=True, slots=True)
class Input:
    """A page input a scanner found: where, inside which function, and which."""

    path: Path
    line: int
    function: str
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.name}() in {self.function or '<module>'}"


def page_inputs(
    source: str, path: Path = MEMORY, names: frozenset[str] = INPUT_CALLS
) -> Iterator[Input]:
    """Every reach of a page input: an attribute, an imported member, a getattr literal.

    Each carries its enclosing ``Class.method`` (or function) name, so the one allowed
    click can be told apart from the same call anywhere else, including elsewhere in
    ``browser.py``. A bare name (``type(x)``, ``check()``) is not a page input.
    """
    tree = ast.parse(source)
    scopes: dict[ast.AST, str] = {}

    def enclose(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{name}.{child.name}" if name else child.name
                scopes[child] = inner
                enclose(child, inner)
            else:
                scopes[child] = name
                enclose(child, name)

    enclose(tree, "")
    for node in ast.walk(tree):
        where = scopes.get(node, "")
        if isinstance(node, ast.Attribute) and node.attr in names:
            yield Input(path, node.lineno, where, node.attr)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in names:
                    yield Input(path, node.lineno, where, alias.name)
        elif isinstance(node, ast.Call):
            for arg in _attribute_name_arguments(node):
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    for part in arg.value.split("."):
                        if part in names:
                            yield Input(path, node.lineno, where, part)


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
    """Spec 9.1: reuse contexts[0] as it is; no second context, no cookies, no UA.

    The one exception is the body tap's CDP session (#200), opened at exactly one
    site; what that session may send is pinned by the next test."""
    findings = scan([PACKAGE], context_mutations)
    assert findings, "the scanner no longer sees the body tap's session; is it gone?"
    found = package_inputs(CONTEXT_MUTATORS)
    others = [i for i in found if (i.path, i.function, i.name) not in ALLOWED_CONTEXT_MUTATIONS]
    for entry in sorted(ALLOWED_CONTEXT_MUTATIONS):
        hits = [i for i in found if (i.path, i.function, i.name) == entry]
        assert len(hits) == 1, (
            f"{entry[1]} no longer makes exactly one {entry[2]}() call ({len(hits)} found);"
            " if it moved, point ALLOWED_CONTEXT_MUTATIONS at its new home"
        )
    assert not others, "the attached context is the user's:\n" + "\n".join(str(i) for i in others)


@dataclass(frozen=True, slots=True)
class CdpSend:
    """One reach of ``send``: where, the method it names, and the keys of its params.

    ``method`` is the call's first argument when that is a string literal, else
    ``None``. ``params`` is the set of keys of a dict literal second argument,
    ``frozenset()`` for none, and ``None`` when it is not a dict literal with string
    keys. A reach nobody can read off the line -- ``send`` held without a call, a
    method in a variable, ``getattr(session, "send")``, ``operator.methodcaller``
    or ``attrgetter`` with ``"send"`` -- has ``method`` ``None`` and is refused.
    """

    where: Input
    method: str | None
    params: frozenset[str] | None


def cdp_sends(source: str, path: Path = MEMORY) -> Iterator[CdpSend]:
    """Every reach of ``send`` in ``source``, the way :func:`reached_names` reads a name:
    an attribute, called or not, and a literal handed to ``getattr``, ``attrgetter``,
    or ``methodcaller``."""
    tree = ast.parse(source)
    calls: dict[int, ast.Call] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls[id(node.func)] = node
    scopes: dict[int, str] = {}

    def enclose(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = name
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{name}.{child.name}" if name else child.name
            scopes[id(child)] = inner
            enclose(child, inner)

    enclose(tree, "")
    for node in ast.walk(tree):
        where = Input(path, getattr(node, "lineno", 0), scopes.get(id(node), ""), "send")
        if isinstance(node, ast.Attribute) and node.attr == "send":
            call = calls.get(id(node))
            if call is None:
                yield CdpSend(where, None, None)
                continue
            first = call.args[0] if call.args else None
            method = (
                first.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str)
                else None
            )
            yield CdpSend(where, method, _param_keys(call.args[1:2]))
        elif isinstance(node, ast.Call):
            for arg in _attribute_name_arguments(node):
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and "send" in arg.value.split(".")
                ):
                    yield CdpSend(where, None, None)


def _param_keys(args: list[ast.expr]) -> frozenset[str] | None:
    if not args:
        return frozenset()
    params = args[0]
    if not isinstance(params, ast.Dict):
        return None
    keys = [key.value for key in params.keys if isinstance(key, ast.Constant)]
    if len(keys) != len(params.keys) or not all(isinstance(key, str) for key in keys):
        return None
    return frozenset(str(key) for key in keys)


def test_the_one_cdp_session_is_read_only() -> None:
    """ADR 0006's amendment for #200: in the extractor and the worker, ``send`` is
    reached only inside the body tap's one function, only to send a read-only method
    named as a literal, and only with the params that method is allowed
    (``READ_ONLY_CDP_METHODS``). ``Network.enable`` may set its two buffer sizes and
    nothing else -- none of its other options (post data, direct socket traffic,
    durable messages) -- and ``Network.streamResourceContent`` names a request and
    nothing else. The buffer *values* are module constants, pinned by
    ``tests/test_body_tap.py``: a scanner reads names, and the numbers live one
    import away."""
    found = [
        item
        for root in BROWSER_ROOTS
        for path in ([root] if root.is_file() else python_files(root))
        for item in cdp_sends(path.read_text(encoding="utf-8"), path)
    ]
    outside = [i.where for i in found if (i.where.path, i.where.function) not in CDP_SENDERS]
    assert not outside, "a CDP send outside the body tap:\n" + "\n".join(str(i) for i in outside)
    for item in found:
        assert item.method in READ_ONLY_CDP_METHODS, f"{item.where}: sends {item.method!r}"
        assert item.params == READ_ONLY_CDP_METHODS[item.method], (
            f"{item.where}: {item.method} with params {item.params}"
        )
    assert sorted(str(i.method) for i in found) == sorted(READ_ONLY_CDP_METHODS)
    for site, count in CDP_SENDERS.items():
        hits = [i for i in found if (i.where.path, i.where.function) == site]
        assert len(hits) == count, (
            f"{site[1]} makes {len(hits)} send calls, not {count}; if it moved, point"
            " CDP_SENDERS at its new home"
        )


def _scoped(tree: ast.AST) -> dict[int, str]:
    """Each node's enclosing ``Class.method`` (or function) name, by node id."""
    scopes: dict[int, str] = {}

    def enclose(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                inner = f"{name}.{child.name}" if name else child.name
                scopes[id(child)] = inner
                enclose(child, inner)
            else:
                scopes[id(child)] = name
                enclose(child, name)

    enclose(tree, "")
    return scopes


def name_reaches(source: str, name: str, path: Path = MEMORY) -> Iterator[tuple[Input, bool]]:
    """Every reach of the attribute ``name`` in ``source``, the way :func:`reached_names`
    reads one -- an attribute, called or held; a ``from`` import; a literal handed to
    ``getattr``, ``attrgetter``, or ``methodcaller`` -- each with whether it is a direct
    call that passes no ``tap`` and no ``**`` keywords (the one harmless way to reach
    ``observe``)."""
    tree = ast.parse(source)
    scopes = _scoped(tree)
    calls = {
        id(node.func): node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for node in ast.walk(tree):
        where = scopes.get(id(node), "")
        if isinstance(node, ast.Attribute) and node.attr == name:
            call = calls.get(id(node))
            plain = call is not None and all(
                keyword.arg not in ("tap", None) for keyword in call.keywords
            )
            yield Input(path, node.lineno, where, name), plain
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == name:
                    yield Input(path, node.lineno, where, name), False
        elif isinstance(node, ast.Call):
            for arg in _attribute_name_arguments(node):
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and name in arg.value.split(".")
                ):
                    yield Input(path, node.lineno, where, name), False


def tap_observations(source: str, path: Path = MEMORY) -> Iterator[Input]:
    """Every reach of ``observe`` that could open a body tap: a call that passes ``tap``
    or ``**`` keywords, and any reach that is not a direct call at all -- a held method,
    ``functools.partial``, ``getattr`` or ``methodcaller`` -- whose keywords nobody can
    read off the line (#207 review)."""
    for item, plain in name_reaches(source, "observe", path):
        if not plain:
            yield item


def test_only_the_named_observations_open_the_body_tap() -> None:
    """ADR 0006's amendment: the body tap opens for the connections sync (#200) and for
    each enrichment visit (#203), at one call each, and nowhere else; and the tap's
    opener is reached only from ``BrowserRun.observe``, once."""
    files = [
        path
        for root in BROWSER_ROOTS
        for path in ([root] if root.is_file() else python_files(root))
    ]
    found = [
        item for path in files for item in tap_observations(path.read_text(encoding="utf-8"), path)
    ]
    sites = sorted((item.path, item.function) for item in found)
    assert sites == sorted(TAP_OBSERVERS), "a body tap opened somewhere new:\n" + "\n".join(
        str(item) for item in found
    )
    openers = [
        item
        for path in files
        for item, _ in name_reaches(path.read_text(encoding="utf-8"), "_open_body_tap", path)
    ]
    assert [(i.path, i.function) for i in openers] == [TAP_OPENER], "\n".join(
        str(i) for i in openers
    )


def test_the_tap_observation_scanner_sees_a_tap_and_a_hidden_one() -> None:
    source = (
        "import functools, operator\n"
        "class PageProfiles:\n"
        "    async def open_profile(self, run, match, extra):\n"
        "        await run.observe(match, tap=True)\n"
        "        await run.observe(match, **extra)\n"
        "        await run.observe(match, limits=None)\n"
        "        held = run.observe\n"
        "        await getattr(run, 'observe')(match, tap=True)\n"
        "        await functools.partial(run.observe, tap=True)(match)\n"
        "        await operator.methodcaller('observe', match, tap=True)(run)\n"
    )
    found = sorted((i.line, i.function) for i in tap_observations(source))
    assert found == [(line, "PageProfiles.open_profile") for line in (4, 5, 7, 8, 9, 10)]


def test_the_tap_opener_scanner_sees_every_way_to_reach_it() -> None:
    source = (
        "import functools\n"
        "class PageProfiles:\n"
        "    async def open_profile(self, run, page, match):\n"
        "        await run._open_body_tap(page, match, 1)\n"
        "        opener = run._open_body_tap\n"
        "        await getattr(run, '_open_body_tap')(page, match, 1)\n"
        "        await functools.partial(run._open_body_tap, page)(match, 1)\n"
        "        from netkeeper.linkedin.browser import _open_body_tap\n"
    )
    found = sorted(i.line for i, _ in name_reaches(source, "_open_body_tap"))
    assert found == [4, 5, 6, 7, 8]


def test_a_held_observe_is_a_reach_even_where_no_tap_is_named() -> None:
    """A held ``observe`` is refused on its own: nothing on the line says ``tap``."""
    source = "def later(run):\n    return run.observe\n"
    assert [(i.line, i.function) for i in tap_observations(source)] == [(2, "later")]


def test_the_cdp_send_scanner_catches_a_mutating_method_or_a_hidden_one() -> None:
    source = (
        "import operator\n"
        "class BrowserRun:\n"
        "    async def _open_body_tap(self, session, name):\n"
        "        await session.send('Network.setUserAgentOverride', {})\n"
        "        await session.send(name)\n"
        "        send = session.send\n"
        "        await getattr(session, 'send')('Network.setCookie', {})\n"
        "        await operator.methodcaller('send', 'Network.setCookie')(session)\n"
        "        await operator.attrgetter('send')(session)('Network.setCookie')\n"
        "        await session.send('Network.enable', {'maxPostDataSize': 1})\n"
        "        await session.send('Network.enable', options)\n"
    )
    found = list(cdp_sends(source))
    assert sorted(str(i.method) for i in found) == sorted(
        [
            "Network.setUserAgentOverride",
            "None",
            "None",
            "None",
            "None",
            "None",
            "Network.enable",
            "Network.enable",
        ]
    )
    assert {i.where.function for i in found} == {"BrowserRun._open_body_tap"}
    enables = [i.params for i in found if i.method == "Network.enable"]
    assert frozenset({"maxPostDataSize"}) in enables and None in enables
    for params in enables:
        assert params != READ_ONLY_CDP_METHODS["Network.enable"]


def test_no_code_path_alters_or_answers_a_request() -> None:
    """ADR 0006: the page's requests are read, never held, changed, or answered."""
    findings = scan([PACKAGE], request_mutations)
    assert not findings, complain(findings, "netkeeper reads the page's answers only:")


def test_no_code_path_sends_through_an_api_request_context() -> None:
    """ADR 0006: no request of netkeeper's own from outside the page either."""
    findings = scan([PACKAGE], api_request_sends)
    for path, count in ALLOWED_SUPER_REQUESTS.items():
        hits = [f for f in findings if f.path == path and f.detail == SUPER_REQUEST]
        assert len(hits) == count, (
            f"{path.name} no longer makes exactly {count} super().request calls ({len(hits)});"
            " if they moved, update ALLOWED_SUPER_REQUESTS"
        )
    others = [
        f for f in findings if not (f.path in ALLOWED_SUPER_REQUESTS and f.detail == SUPER_REQUEST)
    ]
    assert not others, complain(others, "netkeeper sends no requests of its own:")


def test_the_observing_modules_only_listen_and_scroll() -> None:
    """ADR 0006: the modules that read the page's answers never drive the page."""
    findings: list[Finding] = []
    for path in OBSERVING_MODULES:
        assert path.exists(), f"{path} moved; point OBSERVING_MODULES at its new home"
        findings.extend(page_drivers(path.read_text(encoding="utf-8"), path))
    assert not findings, complain(findings, "an observing module drives the page:")


def _in_browser_roots(path: Path) -> bool:
    return any(path == root or root in path.parents for root in BROWSER_ROOTS)


def package_inputs(
    names: frozenset[str] = INPUT_CALLS, roots: list[Path] = INPUT_ROOTS
) -> list[Input]:
    """Every page input (or script call, with ``names``) under ``roots``, with the
    browser-only names read only in the extractor and the worker."""
    found: list[Input] = []
    files = [path for root in roots for path in python_files(root)]
    assert len(files) >= 20, "the scan found too few files; is the path right?"
    for path in files:
        for item in page_inputs(path.read_text(encoding="utf-8"), path, names):
            if item.name in BROWSER_ONLY_INPUTS and not _in_browser_roots(path):
                continue
            found.append(item)
    return found


def test_the_one_page_input_is_the_contact_info_click() -> None:
    """ADR 0006, #190: no click, key, tap, hover, typing, or synthetic event anywhere in the
    package but the one ``click`` inside ``BrowserRun.click_contact_info``."""
    found = package_inputs()
    others = [i for i in found if (i.path, i.function, i.name) not in ALLOWED_INPUTS]
    for entry in sorted(ALLOWED_INPUTS):
        hits = [i for i in found if (i.path, i.function, i.name) == entry]
        assert len(hits) == 1, (
            f"{entry[1]} no longer makes exactly one {entry[2]}() call ({len(hits)} found);"
            " if it moved, point ALLOWED_INPUTS at its new home"
        )
    assert not others, "a page input outside ADR 0006's one click:\n" + "\n".join(
        str(i) for i in others
    )


def test_script_runs_in_a_page_only_for_preflights_fingerprint() -> None:
    """ADR 0006, #190 review: no ``evaluate`` (or any other way to run script in a page)
    anywhere in the package -- ``browser.py`` and ``rehearse.py`` included -- but
    preflight's one read of the blank tab's ``navigator`` properties."""
    found = package_inputs(SCRIPT_CALLS)
    allowed = [i for i in found if (i.path, i.function, i.name) in ALLOWED_SCRIPTS]
    others = [i for i in found if (i.path, i.function, i.name) not in ALLOWED_SCRIPTS]
    assert len(allowed) == 1, (
        f"preflight no longer makes exactly one evaluate call ({len(allowed)} found);"
        " if it moved, point ALLOWED_SCRIPTS at its new home"
    )
    assert not others, "script run in a page outside preflight's fingerprint:\n" + "\n".join(
        str(i) for i in others
    )


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


def test_the_scanners_see_getattribute_with_a_literal() -> None:
    """#196 item 11: ``__getattribute__`` names an attribute as plainly as ``getattr``."""
    assert list(context_mutations("context.__getattribute__('new_cdp_session')(page)\n"))
    assert list(launch_calls("object.__getattribute__(p.chromium, 'launch')()\n"))
    assert [i.line for i, _ in name_reaches("run.__getattribute__('observe')(m)\n", "observe")]
    assert list(tap_observations("run.__getattribute__('observe')(m, tap=True)\n"))


def test_the_scanners_see_a_qualified_getattr_with_a_literal() -> None:
    """#309 review, M10: ``builtins.getattr`` and ``inspect.getattr_static`` name an
    attribute as plainly as a bare ``getattr``, for the launch, tap, and CDP scanners."""
    assert list(launch_calls("await builtins.getattr(p.chromium, 'launch')()\n"))
    assert list(launch_calls("await inspect.getattr_static(p.chromium, 'launch')()\n"))
    assert list(tap_observations("builtins.getattr(run, 'observe')(m, tap=True)\n"))
    assert list(tap_observations("inspect.getattr_static(run, 'observe')(m, tap=True)\n"))
    source = (
        "class BrowserRun:\n"
        "    async def _open_body_tap(self, session):\n"
        "        await builtins.getattr(session, 'send')('Network.setCookie', {})\n"
        "        await inspect.getattr_static(session, 'send')('Network.setCookie', {})\n"
    )
    assert len(list(cdp_sends(source))) == 2
    assert list(context_mutations("builtins.getattr(context, 'new_cdp_session')(page)\n"))


def test_no_attribute_is_read_by_a_name_the_scanners_cannot_see() -> None:
    """#207 review, #196 item 11: where a page is in reach -- the extractor, the worker,
    the CLI -- every ``getattr``, ``__getattribute__``, ``methodcaller``, and
    ``attrgetter`` names its attribute with a literal, so the tap, send, CDP session,
    and launch scanners can read it."""
    files = [
        path
        for root in BROWSER_ROOTS
        for path in ([root] if root.is_file() else python_files(root))
    ]
    assert PACKAGE / "cli.py" in files and PACKAGE / "worker.py" in files
    assert len(files) >= 20, "the scan found too few files; is the path right?"
    findings = [
        finding
        for path in files
        for finding in dynamic_attribute_reads(path.read_text(encoding="utf-8"), path)
    ]
    assert not findings, complain(findings, "an attribute named by a value, not a literal:")


@pytest.mark.parametrize(
    "snippet",
    [
        "getattr(x, 'obs' + 'erve')(m, tap=True)\n",
        "getattr(x, name)\n",
        "getattr(x, f'{a}')\n",
        "getattr(*args)\n",
        "builtins.getattr(x, name)\n",
        "x.__getattribute__('obs' + 'erve')\n",
        "x.__getattribute__(name)\n",
        "object.__getattribute__(x, name)\n",
        "getattr(x, '_open_body' + '_tap')\n",
        "operator.methodcaller(name, m)\n",
        "operator.attrgetter('send', name)\n",
        "read = getattr\n",
        "read = x.__getattribute__\n",
        "fn = operator.attrgetter\n",
        "inspect.getattr_static(x, name)\n",
        "read = inspect.getattr_static\n",
    ],
)
def test_the_dynamic_attribute_scanner_catches_a_name_built_at_run_time(snippet: str) -> None:
    assert list(dynamic_attribute_reads(snippet))


@pytest.mark.parametrize(
    "snippet",
    [
        "getattr(response, 'from_service_worker', None)\n",
        "x.__getattribute__('url')\n",
        "object.__getattribute__(x, 'url')\n",
        "operator.methodcaller('url', 1)\n",
        "operator.attrgetter('a', 'b.c')\n",
        "inspect.getattr_static(x, 'url')\n",
    ],
)
def test_the_dynamic_attribute_scanner_passes_a_literal(snippet: str) -> None:
    assert not list(dynamic_attribute_reads(snippet))


def test_cli_is_scanned_for_typing_and_clearing() -> None:
    """#196 item 2: ``cli.py`` reaches the browser, so a ``type`` or ``clear`` there is
    a page input like anywhere in the extractor; only the one ``event.type`` read is
    allowed, by its function."""
    cli = PACKAGE / "cli.py"
    assert _in_browser_roots(cli)
    source = "async def run(page):\n    await page.type('#q', 'x')\n    await locator.clear()\n"
    found = [
        item
        for item in page_inputs(source, cli)
        if (item.path, item.function, item.name) not in ALLOWED_INPUTS
    ]
    assert sorted(i.name for i in found) == ["clear", "type"]


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
    assert list(api_request_sends("super().request(url)\n"))  # allowed by path only
    assert list(api_request_sends("super(Page, page).request.get(url)\n"))
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
    assert list(page_drivers("await page.get_by_role('link', name='Contact info').click()\n"))
    assert list(page_drivers("control = page.get_by_text('Contact info')\n"))
    assert not list(page_drivers("await run.scroll(plan)\n"))
    assert not list(page_drivers("await run.click_contact_info(path, pause_s=1.0)\n"))
    assert not list(page_drivers("page.on('response', handler)\n"))


def test_the_input_scanner_catches_every_other_input() -> None:
    """Each spelling a regression would use, and the one place a click is allowed."""
    allowed = (
        "class BrowserRun:\n"
        "    async def click_contact_info(self, path):\n"
        "        await control.click(delay=90)\n"
    )
    (one,) = page_inputs(allowed)
    assert (one.function, one.name) == ("BrowserRun.click_contact_info", "click")
    elsewhere = (
        "class BrowserRun:\n"
        "    async def scroll(self, plan):\n"
        "        await page.mouse.click(1, 2)\n"
        "async def click_contact_info():\n"  # the right name, the wrong owner
        "    await page.click('a')\n"
    )
    assert sorted((i.function, i.name) for i in page_inputs(elsewhere)) == [
        ("BrowserRun.scroll", "click"),
        ("click_contact_info", "click"),
    ]
    for snippet in (
        "await page.keyboard.press('Escape')\n",
        "await locator.fill('x')\n",
        "await locator.type('x')\n",
        "await locator.hover()\n",
        "await locator.tap()\n",
        "await locator.dblclick()\n",
        "await locator.check()\n",
        "await locator.set_checked(True)\n",
        "await locator.select_option('a')\n",
        "await locator.dispatch_event('click')\n",
        "await locator.focus()\n",
        "await locator.drag_to(other)\n",
        "await page.mouse.down()\n",
        "await page.mouse.move(1, 2)\n",
        "await page.touchscreen.tap(1, 2)\n",
        "go = locator.click\n",
        "getattr(locator, 'click')()\n",
        "operator.methodcaller('press', 'Enter')(locator)\n",
        "from somewhere import click as go\n",
    ):
        assert list(page_inputs(snippet)), snippet
        assert not {(i.function, i.name) for i in page_inputs(snippet)} & {
            ("BrowserRun.click_contact_info", "click")
        }
    for snippet in (
        "await locator.clear()\n",
        "await locator.blur()\n",
        "await locator.scroll_into_view_if_needed()\n",
    ):
        assert list(page_inputs(snippet)), snippet
    # #192's pointer rest, as its own scanner used to check it: the allowed method,
    # a sibling method, and module level read differently.
    rest = (
        "class BrowserRun:\n"
        "    async def _rest_pointer_over_content(self, page):\n"
        "        await page.mouse.move(1, 2)\n"
    )
    assert [(i.function, i.name) for i in page_inputs(rest)] == [
        ("BrowserRun._rest_pointer_over_content", "move")
    ]
    sibling = (
        "class BrowserRun:\n"
        "    async def scroll(self, page):\n"
        "        await page.mouse.move(1, 2)\n"
    )
    assert [(i.function, i.name) for i in page_inputs(sibling)] == [("BrowserRun.scroll", "move")]
    assert [(i.function, i.name) for i in page_inputs("await page.mouse.move(1, 2)\n")] == [
        ("", "move")
    ]
    assert list(page_inputs("mouse = page.mouse\nawait mouse.move(x, y)\n"))
    assert not list(page_inputs("box = await locator.bounding_box()\n"))
    assert not list(page_inputs("kind = type(exc).__name__\n"))
    assert not list(page_inputs("await run.click_contact_info(path, pause_s=1.0)\n"))
    assert not list(page_inputs("await page.mouse.wheel(0, 300)\n"))


def test_the_browser_only_names_are_read_where_a_page_can_be_reached() -> None:
    """``type`` and ``clear`` are page inputs in the extractor and the worker, and the
    core's own attributes elsewhere; every other input name counts everywhere."""
    assert _in_browser_roots(LINKEDIN / "page_profiles.py")
    assert _in_browser_roots(PACKAGE / "worker.py")
    assert not _in_browser_roots(PACKAGE / "services" / "heat.py")
    assert not _in_browser_roots(WEB / "api" / "events.py")
    assert {"type", "clear"} == BROWSER_ONLY_INPUTS
    assert [PACKAGE] == INPUT_ROOTS
    # The core's own `type` and `clear` exist (so the narrowing is load-bearing), and
    # nothing else the scan reads is exempt.
    raw = [
        i
        for path in python_files(PACKAGE / "services")
        for i in page_inputs(path.read_text(encoding="utf-8"), path)
    ]
    assert any(i.name == "clear" for i in raw)
    assert all(i.name in BROWSER_ONLY_INPUTS for i in raw)


def test_the_script_scanner_catches_every_spelling() -> None:
    for snippet in (
        "await page.evaluate('1')\n",
        "await page.evaluate_handle('document')\n",
        "await locator.evaluate_all('es => 1')\n",
        "await page.eval_on_selector('a', 'e => e.click()')\n",
        "await page.wait_for_function('fetch(u)')\n",
        "await page.add_script_tag(content='x')\n",
        "getattr(page, 'evaluate')('1')\n",
    ):
        assert list(page_inputs(snippet, names=SCRIPT_CALLS)), snippet
    fingerprint = "async def _read_fingerprint(page):\n    return await page.evaluate(JS)\n"
    (one,) = list(page_inputs(fingerprint, LINKEDIN / "preflight.py", SCRIPT_CALLS))
    assert (one.path, one.function, one.name) in ALLOWED_SCRIPTS
    elsewhere = "class BrowserRun:\n    async def peek(self):\n        await p.evaluate('1')\n"
    (other,) = list(page_inputs(elsewhere, LINKEDIN / "browser.py", SCRIPT_CALLS))
    assert (other.path, other.function, other.name) not in ALLOWED_SCRIPTS
    assert not list(page_inputs("await run.scroll(plan)\n", names=SCRIPT_CALLS))


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
    assert list(
        database_imports("from ..crm.tags import list_tags\n", LINKEDIN / "page_profiles.py")
    )
    assert list(
        browser_imports("from ...linkedin.browser import Attach\n", WEB / "api" / "__init__.py")
    )
    assert not list(
        database_imports("from .browser import BrowserRun\n", LINKEDIN / "page_profiles.py")
    )
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
