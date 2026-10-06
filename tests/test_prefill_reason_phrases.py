"""Every reason phrase the prefill can record has words in the UI (#383, drift guard).

A prefill run records its reason (``Run.error``, and the message's ``error``) as a fixed
phrase or one of three codes. ``frontend/src/features/linkedin-steps/prefill-reasons.json``
lists them all, with ``{}`` for a value a phrase interpolates. The frontend test reads
that file and fails for a phrase with no words, so a new refusal can't reach a person raw.

**The rule.** A reason the prefill records must be a string literal, an f-string, or a
module-level ``NAME: Final = "..."`` constant, written where it is recorded (the arguments
of the calls in ``OUTCOME_CALLS``, and the returns of ``REFUSING_FUNCTIONS``). The scan
fails on any other expression: an unannotated constant, a phrase returned from a helper,
a local variable, an exception's text. A reason that only passes on a phrase the scan
already reads goes in ``PASS_THROUGH`` by its exact source, with the reason it is safe.
A new refusing function goes in ``REFUSING_FUNCTIONS``.

This test finds the phrases in the source (the arguments of the calls that build an
outcome, and the returns of the checks that refuse) and fails when the file and the
source disagree. To update the file after adding a refusal, run
``NETKEEPER_WRITE_PHRASES=1 pytest tests/test_prefill_reason_phrases.py``.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "frontend/src/features/linkedin-steps/prefill-reasons.json"
SOURCES = (
    "netkeeper/linkedin/browser.py",
    "netkeeper/linkedin/page_messaging.py",
    "netkeeper/linkedin/messaging.py",
    "netkeeper/services/message_send.py",
    "netkeeper/worker.py",
)
#: Calls whose arguments carry a reason.
OUTCOME_CALLS = frozenset(
    {
        "_not_typed",
        "ComposeRefusal",
        "_after_failure",
        "MessageClick",
        "TypingResult",
        "MessageOutcome",
        "_prefill_not_typed",
    }
)
#: Checks whose returns (and first assignments) are a refusal phrase.
REFUSING_FUNCTIONS = frozenset(
    {"_bubble_refusal", "_composer_refusal", "message_control_refusal", "spend", "_await_bubble"}
)
#: Not a refusal: what a prefill that typed records.
NOT_REASONS = frozenset({"typed"})


def _final_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME: Final = "phrase"`` constants. An unannotated constant isn't one:
    it can be rebound, so a phrase recorded through it is unresolved."""
    found: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and "Final" in ast.unparse(node.annotation)
        ):
            found[node.target.id] = node.value.value
    return found


class Unresolved(Exception):
    """A reason this scan can't turn into a phrase."""


def _strings(node: ast.AST, constants: dict[str, str]) -> Iterator[str]:
    """The phrases ``node`` can be. A literal, an f-string (``{}`` for each value), a
    ``Final`` constant, or a choice between them. Anything else raises :class:`Unresolved`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.Constant) and node.value is None:
        return
    elif isinstance(node, ast.JoinedStr):
        yield "".join(str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values)
    elif isinstance(node, ast.IfExp):
        yield from _strings(node.body, constants)
        yield from _strings(node.orelse, constants)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _strings(value, constants)
    elif isinstance(node, ast.Name) and node.id in constants:
        yield constants[node.id]
    else:
        raise Unresolved(ast.unparse(node))


def _reasons(relative: str, node: ast.AST, constants: dict[str, str]) -> tuple[set[str], set[str]]:
    """(phrases, unresolved sources) of one reason expression."""
    phrases: set[str] = set()
    try:
        phrases.update(_strings(node, constants))
    except Unresolved:
        # A choice that mixes a pass-through with a literal still yields its literals.
        for part in ast.walk(node):
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                phrases.add(part.value)
        return phrases, {ast.unparse(node)}
    return phrases, set()


def _owners(tree: ast.Module) -> dict[int, str]:
    """The innermost function each node sits in (``<module>`` for none)."""
    owner: dict[int, str] = {}
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            for inner in ast.walk(function):
                owner[id(inner)] = function.name  # walk is outer-first: the inner wins
    return owner


def _scan() -> tuple[set[str], set[tuple[str, str, str]]]:
    """(phrases, unresolved reasons as ``(file, function, source)``)."""
    found: set[str] = set()
    unresolved: set[tuple[str, str, str]] = set()
    for relative in SOURCES:
        tree = ast.parse((ROOT / relative).read_text())
        constants = _final_constants(tree)
        owner = _owners(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name in OUTCOME_CALLS:
                    for arg in [*node.args, *(k.value for k in node.keywords)]:
                        if not _is_reason_position(name, node, arg):
                            continue
                        phrases, bad = _reasons(relative, arg, constants)
                        found |= phrases
                        unresolved |= {(relative, owner[id(node)], src) for src in bad}
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in REFUSING_FUNCTIONS
            ):
                for inner in ast.walk(node):
                    value = inner.value if isinstance(inner, ast.Return | ast.AnnAssign) else None
                    if value is not None:
                        phrases, bad = _reasons(relative, value, constants)
                        found |= phrases
                        unresolved |= {(relative, owner[id(node)], src) for src in bad}
    return found - NOT_REASONS, unresolved


def _is_reason_position(call: str, node: ast.Call, arg: ast.expr) -> bool:
    """Whether ``arg`` is the reason of this outcome-building call."""
    if call == "MessageOutcome":
        return node.args.index(arg) == 1 if arg in node.args else True
    if call == "TypingResult":
        return arg in node.args and node.args.index(arg) == 1
    if call == "MessageClick":
        return arg in node.args and node.args.index(arg) == 2
    if call == "_after_failure":
        return arg in node.args and node.args.index(arg) == 1
    if call == "_prefill_not_typed":
        return arg in node.args and node.args.index(arg) == 2
    return True


#: Reasons that pass a phrase on from another scanned place, by ``(file, function, source)``.
#: Each is the output of a call or check this scan already reads, so no phrase hides in
#: it, and each is allowed only in the function named: a local variable of the same name
#: elsewhere still fails. A new entry needs that same justification.
_B = "netkeeper/linkedin/browser.py"
_P = "netkeeper/linkedin/page_messaging.py"
_M = "netkeeper/services/message_send.py"
PASS_THROUGH = frozenset(
    {
        # The refusing checks hand their phrase up: every check's returns are scanned.
        (_B, "_await_bubble", "refusal"),
        (_B, "_composer_refusal", "bubble"),
        (_B, "type_into_composer", "refusal"),
        # click_message passes message_control_refusal's phrase on (scanned).
        (_B, "click_message", "refusal"),
        # The one-line helpers take their caller's phrase; every call site is scanned.
        (_P, "_not_typed", "reason"),
        (_M, "_not_typed", "reason"),
        (_M, "_after_failure", "reason"),
        ("netkeeper/worker.py", "_prefill_not_typed", "reason"),
        # PagePrefill passes on a TypingResult's reason and the click's: both scanned.
        (_P, "_result", "typing.reason"),
        (_P, "prefill", "click.refusal or 'the Message control was not clicked'"),
        # The compose option's refusal: ComposeRefusal(...) calls are scanned.
        (_P, "prefill", "option.reason"),
        # spend() returns a phrase; run_prefill passes it on to _not_typed.
        (_M, "run_prefill", "refused"),
    }
)


def source_phrases() -> set[str]:
    found, unresolved = _scan()
    bad = sorted(u for u in unresolved if u not in PASS_THROUGH)
    assert not bad, (
        "a refusal reason that isn't a literal or a Final constant; make it one, or, if it"
        " passes on a phrase this scan already reads, add (file, function, source) to"
        " PASS_THROUGH:\n" + "\n".join(map(str, bad))
    )
    return found


def test_the_phrase_list_matches_the_source() -> None:
    found = sorted(source_phrases())
    if os.environ.get("NETKEEPER_WRITE_PHRASES") == "1":
        FIXTURE.write_text(json.dumps(found, indent=2, ensure_ascii=False) + "\n")
    listed = json.loads(FIXTURE.read_text())
    assert listed == found, (
        "prefill-reasons.json and the source disagree: add words for a new phrase in"
        " prefill-copy.ts, then regenerate with NETKEEPER_WRITE_PHRASES=1"
    )


def test_the_scan_finds_the_known_phrases() -> None:
    """The scan itself is pinned, so a refactor can't make it find nothing."""
    found = source_phrases()
    for phrase in (
        "another_compose",
        "recipient_name_mismatch",
        "recipient_name_unreadable",
        "the Message control could not be clicked",
        "the bubble was not drawn",
        "the browser was busy",
        "the page answered {}",
        "after typing: {}",
        "the composer is not empty",
    ):
        assert phrase in found
    assert len(found) > 60
