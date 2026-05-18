"""Tests for engine resume behavior — `--from-stage` reset semantics.

Pre Step-9.40 Phase 3, this file also covered `_archive_design_artifacts()`
which moved per-pass design artifacts into pass_<N>/ subdirs. That helper
is now redundant — debate_writer + debate_critique scope their writes
under pass_<N>/ directly via ArtifactWriter (Phase 2c). Archive tests
removed; reset_from_stage tests preserved.

Phase 4 (kill meta.yaml): switched from MetaStore to Manifest as the
single store. Tests updated to match.
"""

from __future__ import annotations

import pytest


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
    from code_loops.manifest import Manifest

    manifest = Manifest(task_dir / "manifest.json")
    manifest.init_task("0001_t", "feature")
    for s in ["prd", "research", "design", "impl_plan"]:
        manifest.stage_started(s)
        manifest.stage_completed(s, cost_usd=0.1, duration_s=1.0)

    from code_loops.engine import Engine

    Engine(task_dir, from_stage="design")
    after = Manifest(task_dir / "manifest.json").data["stages"]
    assert after["prd"]["status"] == "done"
    assert after["research"]["status"] == "done"
    # reset_stage wipes status (only preserves attempts_count counter)
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
    from code_loops.manifest import Manifest

    Manifest(task_dir / "manifest.json").init_task("0001_t", "feature")

    from code_loops.engine import Engine

    with pytest.raises(EngineError, match="not in pipeline"):
        Engine(task_dir, from_stage="nonexistent_stage")
