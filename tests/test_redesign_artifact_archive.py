"""Tests for redesign-loop artifact preservation + abort behavior.

Covers the engine helpers added to fix Step 10 findings: when the
`debate_critique` stage emits `redesign_needed`, just-completed
design + design_review artifacts should be moved into `pass_<N>/`
subdirs (not overwritten by the next pass), and when redesign loops
exhaust, the engine should abort instead of falling through with a
bad RFC.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_loops.engine import _archive_design_artifacts


def _make_design_pass(task_dir: Path) -> None:
    """Populate design/ + design_review/ with files like a finished pass."""
    design = task_dir / "design"
    design.mkdir(parents=True)
    (design / "draft_v1.md").write_text("draft 1")
    (design / "draft_v2.md").write_text("draft 2")
    (design / "debate.md").write_text("debate log")
    (design / "final.md").write_text("final rfc")
    (design / "redesign_signal.md").write_text("signal — input for next pass")
    (design / "previous_rfc.md").write_text("prev rfc — input for next pass")

    review = task_dir / "design_review"
    review.mkdir(parents=True)
    (review / "ai.md").write_text("ai critic")
    (review / "ai_v1.md").write_text("ai critic round 1")
    (review / "elegance.md").write_text("elegance critic")
    (review / "elegance_v1.md").write_text("elegance round 1")
    (review / "verdict.md").write_text("redesign_needed")
    (review / "debate.md").write_text("review debate log")


def test_archive_moves_per_pass_files(tmp_path: Path):
    _make_design_pass(tmp_path)
    _archive_design_artifacts(tmp_path, prev_pass=1)

    # Archive subdirs created
    assert (tmp_path / "design" / "pass_1").is_dir()
    assert (tmp_path / "design_review" / "pass_1").is_dir()

    # Per-pass files moved into pass_1/
    for fname in ["draft_v1.md", "draft_v2.md", "debate.md", "final.md"]:
        archived = tmp_path / "design" / "pass_1" / fname
        flat = tmp_path / "design" / fname
        assert archived.exists(), f"{fname} should be moved to pass_1/"
        assert not flat.exists(), f"{fname} should be gone from design/ root"

    for fname in ["ai.md", "ai_v1.md", "elegance.md", "elegance_v1.md", "verdict.md", "debate.md"]:
        archived = tmp_path / "design_review" / "pass_1" / fname
        flat = tmp_path / "design_review" / fname
        assert archived.exists(), f"{fname} should be moved to pass_1/"
        assert not flat.exists(), f"{fname} should be gone from design_review/ root"


def test_archive_keeps_redesign_inputs_at_top_level(tmp_path: Path):
    """`redesign_signal.md` and `previous_rfc.md` are inputs for the NEXT pass —
    must remain at design/ root, not get archived."""
    _make_design_pass(tmp_path)
    _archive_design_artifacts(tmp_path, prev_pass=1)

    assert (tmp_path / "design" / "redesign_signal.md").exists()
    assert (tmp_path / "design" / "previous_rfc.md").exists()
    # Not in archive
    assert not (tmp_path / "design" / "pass_1" / "redesign_signal.md").exists()
    assert not (tmp_path / "design" / "pass_1" / "previous_rfc.md").exists()


def test_archive_handles_missing_dirs(tmp_path: Path):
    """If a stage dir doesn't exist (e.g. design_review never ran for some
    reason), archive should be a no-op for that one without raising."""
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "draft_v1.md").write_text("only design exists")
    # design_review/ missing entirely

    _archive_design_artifacts(tmp_path, prev_pass=1)

    assert (tmp_path / "design" / "pass_1" / "draft_v1.md").exists()
    assert not (tmp_path / "design_review").exists()


def test_archive_does_not_recurse_into_existing_pass_dirs(tmp_path: Path):
    """Calling archive twice (e.g. loops 1 → 2 → 3) should not move an existing
    pass_1/ contents into pass_2/."""
    _make_design_pass(tmp_path)
    _archive_design_artifacts(tmp_path, prev_pass=1)
    # Now simulate pass 2 finishing with new top-level files
    (tmp_path / "design" / "draft_v1.md").write_text("pass 2 draft 1")
    (tmp_path / "design" / "final.md").write_text("pass 2 final")

    _archive_design_artifacts(tmp_path, prev_pass=2)

    # pass_1 untouched
    assert (tmp_path / "design" / "pass_1" / "draft_v1.md").read_text() == "draft 1"
    # pass_2 has new content
    assert (tmp_path / "design" / "pass_2" / "draft_v1.md").read_text() == "pass 2 draft 1"
    assert (tmp_path / "design" / "pass_2" / "final.md").read_text() == "pass 2 final"


# ---- _reset_from_stage ----


def test_reset_from_stage_clears_target_and_downstream(tmp_path, monkeypatch):
    monkeypatch.setattr("code_loops.engine.PACKAGE_DIR", tmp_path)
    (tmp_path / "pipeline.yaml").write_text(
        "defaults:\n  model: claude-opus-4-7\nstages:\n"
        "  - {name: prd, type: prompt, prompt: x.md, inputs: [], outputs: []}\n"
        "  - {name: research, type: prompt, prompt: x.md, inputs: [], outputs: []}\n"
        "  - {name: design, type: prompt, prompt: x.md, inputs: [], outputs: []}\n"
        "  - {name: impl_plan, type: prompt, prompt: x.md, inputs: [], outputs: []}\n"
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    from code_loops.meta import MetaStore

    meta = MetaStore(task_dir / "meta.yaml")
    meta.init_task("0001_t", "feature")
    for s in ["prd", "research", "design", "impl_plan"]:
        meta.stage_started(s)
        meta.stage_completed(s, cost_usd=0.1, duration_s=1.0)

    from code_loops.engine import Engine

    Engine(task_dir, from_stage="design")
    after = MetaStore(task_dir / "meta.yaml").data["stages"]
    assert after["prd"]["status"] == "done"
    assert after["research"]["status"] == "done"
    # reset_stage wipes status (only preserves attempts counter)
    assert "status" not in after["design"]
    assert "status" not in after["impl_plan"]


def test_reset_from_stage_unknown_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("code_loops.engine.PACKAGE_DIR", tmp_path)
    (tmp_path / "pipeline.yaml").write_text(
        "defaults:\n  model: claude-opus-4-7\nstages:\n"
        "  - {name: prd, type: prompt, prompt: x.md, inputs: [], outputs: []}\n"
    )
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    from code_loops.engine import EngineError
    from code_loops.meta import MetaStore

    MetaStore(task_dir / "meta.yaml").init_task("0001_t", "feature")

    from code_loops.engine import Engine

    with pytest.raises(EngineError, match="not in pipeline"):
        Engine(task_dir, from_stage="nonexistent_stage")
