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


def _where(relative: str, node: ast.AST) -> str:
    return f"{relative}:{getattr(node, 'lineno', '?')}: {ast.unparse(node)}"


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
        return phrases, {_where(relative, node)}
    return phrases, set()


def _scan() -> tuple[set[str], set[str]]:
    found: set[str] = set()
    unresolved: set[str] = set()
    for relative in SOURCES:
        tree = ast.parse((ROOT / relative).read_text())
        constants = _final_constants(tree)
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
                        unresolved |= bad
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in REFUSING_FUNCTIONS
            ):
                for inner in ast.walk(node):
                    value = inner.value if isinstance(inner, ast.Return | ast.AnnAssign) else None
                    if value is not None:
                        phrases, bad = _reasons(relative, value, constants)
                        found |= phrases
                        unresolved |= bad
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


#: Reasons that pass a phrase on from another scanned place, by their exact source. Each
#: one is the output of a call or check this scan already reads, so no phrase hides in
#: it. A new entry needs that same justification.
PASS_THROUGH = frozenset(
    {
        # page_messaging: the click's refusal; the same literal text is scanned beside it.
        "click.refusal or 'the Message control was not clicked'",
        # message_send.spend() returns a phrase; run_prefill passes it on to _not_typed.
        "refused",
        # _not_typed(option.reason): the ComposeRefusal constructors are scanned.
        "option.reason",
        # _not_typed(click.refusal or ...): MessageClick(...) is scanned.
        "click.refusal",
        # PrefillResult(MessageOutcome(kind, typing.reason, ...)): TypingResult is scanned.
        "typing.reason",
        # type_into_composer: the refusal of _await_bubble / _composer_refusal, both scanned.
        "refusal",
        "bubble",
        "final",
        "f'after typing: {final}'",
        # the reason of a TypingResult built from another TypingResult's check.
        "reason",
        # plan_refusal() builds its own MessageOutcome from literals, scanned there.
        "outcome.reason",
        # the checks call each other: _composer_refusal returns _bubble_refusal's phrase.
        "await self._bubble_refusal(tab, composer, recipient)",
        (
            "await self._composer_refusal(tab, composer, recipient, '', focus=False,"
            " another_compose=another_compose)"
        ),
        "await self._composer_refusal(tab, composer, recipient, '',"
        " another_compose=another_compose)",
    }
)


def source_phrases() -> set[str]:
    found, unresolved = _scan()
    bad = sorted(u for u in unresolved if u.split(": ", 1)[1] not in PASS_THROUGH)
    assert not bad, (
        "a refusal reason that isn't a literal or a Final constant; make it one, or, if it"
        " passes on a phrase this scan already reads, add its source to PASS_THROUGH:\n"
        + "\n".join(bad)
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
