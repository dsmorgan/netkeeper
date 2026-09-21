#!/usr/bin/env python3
"""Seed GitHub milestones, labels, and issues from docs/implementation-guide.md.

Idempotent: existing milestones, labels, and issues (matched by title) are
left alone and reused for dependency links. Dry-run by default.

    python3 scripts/seed_backlog.py --phases 0,1 --checkpoints --apply
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "implementation-guide.md"

LABELS: dict[str, tuple[str, str]] = {
    "checkpoint": ("b60205", "Human review gate; blocks its milestone"),
    "safety": ("d93f0b", "Touches budgets, pacing, browser identity, send caps, or secrets"),
    "size:S": ("c2e0c6", "Up to half a day"),
    "size:M": ("fef2c0", "One to two days"),
    "size:L": ("f9d0c4", "Three to five days"),
}
for n in range(7):
    LABELS[f"phase:{n}"] = ("0e8a16", f"Phase {n} work item")
for area in ("core", "extractor", "crm", "campaigns", "llm", "frontend", "infra", "docs"):
    LABELS[f"area:{area}"] = ("1d76db", f"Area: {area}")
for lane in ("core", "extractor", "campaigns", "frontend", "infra"):
    LABELS[f"lane:{lane}"] = ("5319e7", f"Parallel lane: {lane}")

PHASE_TITLES = {
    0: "scaffold",
    1: "import and CRM",
    2: "LinkedIn extractor",
    3: "email campaigns",
    4: "LinkedIn messaging",
    5: "LLM module",
    6: "polish and reach",
}


@dataclass
class Item:
    id: str
    title: str
    phase: int
    lane: str
    size: str
    safety: bool
    goal: str = ""
    depends: str = ""
    done: str = ""
    anchor: str = ""

    @property
    def issue_title(self) -> str:
        return f"[{self.id}] {self.title}"

    @property
    def area(self) -> str:
        if self.lane in ("frontend", "infra", "extractor"):
            return self.lane
        if self.lane == "campaigns":
            return "llm" if self.phase == 5 else "campaigns"
        if self.lane == "crm":
            return "crm"
        return "crm" if self.phase == 1 else "core"

    @property
    def lane_label(self) -> str:
        return "core" if self.lane == "crm" else self.lane

    def labels(self) -> list[str]:
        labels = [
            f"phase:{self.phase}",
            f"area:{self.area}",
            f"lane:{self.lane_label}",
            f"size:{self.size}",
        ]
        if self.safety:
            labels.append("safety")
        return labels


@dataclass
class Checkpoint:
    id: str
    title: str
    phase: int
    closes: str
    body_lines: list[str] = field(default_factory=list)

    @property
    def issue_title(self) -> str:
        return f"[{self.id}] {self.title}"

    def labels(self) -> list[str]:
        return ["checkpoint", f"phase:{self.phase}"]


ITEM_RE = re.compile(r"^\*\*(P\d-\d\d) (.+?)\*\* · lane (\w+) · (S|M|L)(?: · `safety`)?$")
CP_MARK_RE = re.compile(r"^\*\*(CP\d+(?:\.\d+)?)\*\* · checkpoint · (.+?)\.?$")
PHASE_RE = re.compile(r"^### Phase (\d): (.+)$")
CP_HEAD_RE = re.compile(r"^### (CP\d+(?:\.\d+)?): (.+)$")


def anchor_for(heading: str) -> str:
    text = heading.lower()
    text = re.sub(r"[^a-z0-9 -]", "", text)
    return text.replace(" ", "-")


def parse(guide: Path) -> tuple[list[Item], list[Checkpoint]]:
    lines = guide.read_text().splitlines()
    items: list[Item] = []
    checkpoints: dict[str, Checkpoint] = {}
    phase = -1
    phase_anchor = ""
    i = 0
    while i < len(lines):
        m = CP_HEAD_RE.match(lines[i])
        if m:
            cp = Checkpoint(id=m.group(1), title=m.group(2), phase=-1, closes="")
            i += 1
            while i < len(lines) and not lines[i].startswith(("### ", "## ")):
                cp.body_lines.append(lines[i])
                i += 1
            checkpoints[cp.id] = cp
            continue
        i += 1
    i = 0
    while i < len(lines):
        line = lines[i]
        m = PHASE_RE.match(line)
        if m:
            phase = int(m.group(1))
            phase_anchor = anchor_for(f"Phase {phase}: {m.group(2)}")
            i += 1
            continue
        m = ITEM_RE.match(line)
        if m and phase >= 0:
            item = Item(
                id=m.group(1),
                title=m.group(2),
                phase=phase,
                lane=m.group(3),
                size=m.group(4),
                safety="`safety`" in line,
                anchor=phase_anchor,
            )
            i += 1
            while i < len(lines) and lines[i].strip():
                body = lines[i]
                if body.startswith("Goal:"):
                    item.goal = body[len("Goal:") :].strip()
                elif body.startswith("Depends on:"):
                    item.depends = body[len("Depends on:") :].strip()
                elif body.startswith("Done when:"):
                    item.done = body[len("Done when:") :].strip()
                i += 1
            items.append(item)
            continue
        m = CP_MARK_RE.match(line)
        if m and phase >= 0 and m.group(1) in checkpoints:
            checkpoints[m.group(1)].phase = phase
            checkpoints[m.group(1)].closes = m.group(2)
        i += 1
    cps = [c for c in checkpoints.values() if c.phase >= 0]
    return items, cps


def gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def existing_milestones() -> dict[str, int]:
    out = gh("api", "repos/{owner}/{repo}/milestones?state=all&per_page=100")
    return {m["title"]: m["number"] for m in json.loads(out)}


def existing_issues() -> dict[str, int]:
    out = gh("issue", "list", "--state", "all", "--limit", "500", "--json", "number,title")
    return {issue["title"]: issue["number"] for issue in json.loads(out)}


def link_dependencies(text: str, numbers: dict[str, int]) -> str:
    def repl(m: re.Match[str]) -> str:
        key = m.group(0)
        return f"#{numbers[key]} ({key})" if key in numbers else key

    return re.sub(r"\b(?:P\d-\d\d|CP\d+(?:\.\d+)?)\b", repl, text)


def item_body(item: Item, numbers: dict[str, int]) -> str:
    depends = link_dependencies(item.depends, numbers) if item.depends else "nothing"
    return (
        f"**Goal:** {item.goal}\n\n"
        f"**Depends on:** {depends}\n\n"
        f"**Lane:** `{item.lane_label}` · **Size:** {item.size}"
        + (" · **Safety-relevant**" if item.safety else "")
        + "\n\n"
        f"**Done when:** {item.done}\n\n"
        f"---\n"
        f"Item `{item.id}` in [docs/implementation-guide.md]"
        f"(../blob/main/docs/implementation-guide.md#{item.anchor}). "
        "The guide is the source of truth; if this issue and the guide disagree, "
        "the guide wins and this issue gets edited. "
        "Design: [docs/architecture.md](../blob/main/docs/architecture.md)."
    )


def cp_body(cp: Checkpoint, numbers: dict[str, int]) -> str:
    body = "\n".join(cp.body_lines).strip()
    return (
        f"Human checkpoint. Opens when: {link_dependencies(cp.closes, numbers)}. "
        f"Closes when the maintainer signs off; the milestone does not close before it.\n\n"
        f"{link_dependencies(body, numbers)}\n\n"
        f"---\n"
        f"Checkpoint `{cp.id}` in [docs/implementation-guide.md]"
        "(../blob/main/docs/implementation-guide.md#3-human-checkpoints). "
        "Watch for the drift signals listed there."
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="", help="comma-separated phase numbers to create")
    ap.add_argument("--checkpoints", action="store_true", help="create all checkpoint issues")
    ap.add_argument("--apply", action="store_true", help="actually call gh (default: dry run)")
    args = ap.parse_args()
    phases = {int(p) for p in args.phases.split(",") if p.strip()}

    items, cps = parse(GUIDE)
    wanted_items = [it for it in items if it.phase in phases]
    wanted_cps = cps if args.checkpoints else []
    print(
        f"parsed {len(items)} items and {len(cps)} checkpoints; "
        f"creating {len(wanted_items)} items, {len(wanted_cps)} checkpoints"
    )

    if not args.apply:
        for it in wanted_items:
            print(f"  {it.issue_title}  [{', '.join(it.labels())}]  milestone=Phase {it.phase}")
        for cp in wanted_cps:
            print(f"  {cp.issue_title}  [{', '.join(cp.labels())}]  milestone=Phase {cp.phase}")
        print("dry run; pass --apply to create")
        return 0

    milestones = existing_milestones()
    for n, title in PHASE_TITLES.items():
        name = f"Phase {n}"
        if name not in milestones:
            out = gh(
                "api",
                "-X",
                "POST",
                "repos/{owner}/{repo}/milestones",
                "-f",
                f"title={name}",
                "-f",
                f"description=Phase {n}: {title}. See docs/implementation-guide.md.",
            )
            milestones[name] = json.loads(out)["number"]
            print(f"milestone created: {name}")

    for name, (color, desc) in LABELS.items():
        gh("label", "create", name, "--color", color, "--description", desc, "--force")
    print(f"labels ensured: {len(LABELS)}")

    numbers: dict[str, int] = {}
    for title, number in existing_issues().items():
        m = re.match(r"^\[(P\d-\d\d|CP\d+)\] ", title)
        if m:
            numbers[m.group(1)] = number

    created: list[Item | Checkpoint] = []
    things: list[Item | Checkpoint] = [*wanted_items, *wanted_cps]
    for thing in things:
        if thing.id in numbers:
            print(f"exists: {thing.issue_title} (#{numbers[thing.id]})")
            continue
        body = item_body(thing, numbers) if isinstance(thing, Item) else cp_body(thing, numbers)
        url = gh(
            "issue",
            "create",
            "--title",
            thing.issue_title,
            "--body",
            body,
            "--milestone",
            f"Phase {thing.phase}",
            "--label",
            ",".join(thing.labels()),
        )
        numbers[thing.id] = int(url.rstrip("/").rsplit("/", 1)[1])
        created.append(thing)
        print(f"created: {thing.issue_title} -> #{numbers[thing.id]}")

    for thing in created:
        body = item_body(thing, numbers) if isinstance(thing, Item) else cp_body(thing, numbers)
        gh("issue", "edit", str(numbers[thing.id]), "--body", body)
    print(f"linked dependencies on {len(created)} issues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
