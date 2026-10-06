"""Every reason phrase the prefill can record has words in the UI (#383, drift guard).

A prefill run records its reason (``Run.error``, and the message's ``error``) as a fixed
phrase or one of three codes. ``frontend/src/features/linkedin-steps/prefill-reasons.json``
lists them all, with ``{}`` for a value a phrase interpolates. The frontend test reads
that file and fails for a phrase with no words, so a new refusal can't reach a person raw.

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


def _strings(node: ast.AST, constants: dict[str, str]) -> Iterator[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
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


def source_phrases() -> set[str]:
    found: set[str] = set()
    for relative in SOURCES:
        tree = ast.parse((ROOT / relative).read_text())
        constants = {
            node.target.id: node.value.value
            for node in tree.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
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
                    for arg in node.args:
                        found.update(_strings(arg, constants))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                node.name in REFUSING_FUNCTIONS
            ):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        found.update(_strings(inner.value, constants))
                    if isinstance(inner, ast.AnnAssign) and inner.value is not None:
                        found.update(_strings(inner.value, constants))
    return found - NOT_REASONS


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
