"""The backlog seeder parses the real implementation guide; keep it honest as the guide evolves."""

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("seed_backlog", ROOT / "scripts" / "seed_backlog.py")
assert spec is not None and spec.loader is not None
seed = importlib.util.module_from_spec(spec)
sys.modules["seed_backlog"] = seed  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(seed)


def test_every_item_in_the_guide_is_parsed() -> None:
    items, checkpoints = seed.parse(seed.GUIDE)
    guide = seed.GUIDE.read_text()
    bold_ids = set(re.findall(r"^\*\*(P\d-\d\d) ", guide, flags=re.M))
    assert {it.id for it in items} == bold_ids
    assert len(items) == len(bold_ids), "duplicate item ids"
    cp_ids = set(re.findall(r"^### (CP\d+): ", guide, flags=re.M))
    assert {cp.id for cp in checkpoints} == cp_ids
    for it in items:
        assert it.goal and it.done, f"{it.id} is missing Goal or Done when"
        assert it.lane in {"core", "extractor", "campaigns", "frontend", "infra", "crm"}, it.id
        assert it.size in {"S", "M", "L"}, it.id
    for cp in checkpoints:
        assert cp.phase >= 0 and cp.closes, cp.id


def test_labels_and_areas_follow_the_guide_table() -> None:
    items, _ = seed.parse(seed.GUIDE)
    by_id = {it.id: it for it in items}
    assert by_id["P0-01"].labels() == ["phase:0", "area:core", "lane:core", "size:S"]
    assert by_id["P1-01"].area == "crm"
    assert "safety" in by_id["P0-09"].labels()
    assert by_id["P5-03"].lane_label == "core" and by_id["P5-03"].area == "crm"


def test_dependency_links_resolve_known_ids_and_leave_unknown_ones() -> None:
    numbers = {"P1-02": 11, "CP1": 28}
    out = seed.link_dependencies("P1-02, P1-06 merge; then CP1", numbers)
    assert out == "#11 (P1-02), P1-06 merge; then #28 (CP1)"


def test_anchor_matches_github_slug_rules() -> None:
    assert seed.anchor_for("Phase 1: import and CRM") == "phase-1-import-and-crm"
    assert seed.anchor_for("3. Human checkpoints") == "3-human-checkpoints"


def test_bodies_link_back_to_the_guide() -> None:
    items, checkpoints = seed.parse(seed.GUIDE)
    body = seed.item_body(items[0], {})
    assert f"#{items[0].anchor}" in body and "Depends on:** nothing" in body
    cp = next(c for c in checkpoints if c.id == "CP0")
    assert "3-human-checkpoints" in seed.cp_body(cp, {})
