"""Test engine resumes from done stages and chains sequentially."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from code_loops.engine import Engine
from code_loops.runner import RunnerResult
from tests.conftest import FakeFactory


def _make_pipeline(tmp_path: Path, stages: list[dict]) -> Path:
    """Create a fake repo root with pipeline.yaml + prompts."""
    (tmp_path / "agents").mkdir()
    for st in stages:
        prompt_rel = st.get("prompt")
        if prompt_rel:
            (tmp_path / prompt_rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / prompt_rel).write_text(f"FAKE PROMPT for {st['name']}")
    (tmp_path / "pipeline.yaml").write_text(yaml.safe_dump({"stages": stages}))
    return tmp_path


def _make_task(repo_root: Path, task_id: str, task_text: str) -> Path:
    task_dir = repo_root / "tasks" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(task_text)
    return task_dir


def test_engine_runs_two_stages_in_order(tmp_path, monkeypatch):
    repo = _make_pipeline(
        tmp_path,
        [
            {
                "name": "prd",
                "type": "prompt",
                "prompt": "agents/strategy/business-analyst.md",
                "inputs": ["task.md"],
                "outputs": ["prd/prd.md"],
            },
            {
                "name": "plan",
                "type": "prompt",
                "prompt": "agents/research/research-lead.md",
                "inputs": ["prd/prd.md"],
                "outputs": ["research_plan/plan.md"],
            },
        ],
    )
    task_dir = _make_task(repo, "0001_test", "Add /export-data command")

    # Patch PACKAGE_DIR in engine + initialize task meta
    from code_loops.manifest import Manifest

    Manifest(task_dir / "manifest.json").init_task("0001_test", "feature")

    responses = iter(
        [
            RunnerResult(text="# PRD: X\n## Problem\np", cost_usd=0.01, duration_s=1.0),
            RunnerResult(text="# Plan: X\n## Scope\ns", cost_usd=0.01, duration_s=1.0),
        ]
    )

    class _R:
        def run(self, *a, **k):
            return next(responses)

    with patch("code_loops.engine.PACKAGE_DIR", repo):
        engine = Engine(task_dir)
        engine.factory = FakeFactory(_R())
        # Re-wire handlers because they captured the original factory in __init__
        from code_loops.stages.parallel import ParallelStage
        from code_loops.stages.prompt import PromptStage

        engine.handlers["prompt"] = PromptStage(engine.factory)
        engine.handlers["parallel"] = ParallelStage(engine.factory)
        engine.run()

    assert (task_dir / "prd" / "prd.md").read_text().startswith("# PRD")
    assert (task_dir / "research_plan" / "plan.md").read_text().startswith("# Plan")
    meta = Manifest(task_dir / "manifest.json").data
    assert meta["stages"]["prd"]["status"] == "done"
    assert meta["stages"]["plan"]["status"] == "done"
    assert meta["status"] == "completed"


def test_engine_skips_already_done_stage(tmp_path):
    repo = _make_pipeline(
        tmp_path,
        [
            {
                "name": "prd",
                "type": "prompt",
                "prompt": "agents/strategy/business-analyst.md",
                "inputs": ["task.md"],
                "outputs": ["prd/prd.md"],
            },
            {
                "name": "plan",
                "type": "prompt",
                "prompt": "agents/research/research-lead.md",
                "inputs": ["prd/prd.md"],
                "outputs": ["research_plan/plan.md"],
            },
        ],
    )
    task_dir = _make_task(repo, "0001_test", "task")

    from code_loops.manifest import Manifest

    meta = Manifest(task_dir / "manifest.json")
    meta.init_task("0001_test", "feature")
    meta.stage_started("prd")
    meta.stage_completed("prd", cost_usd=0.05, duration_s=1.0)
    (task_dir / "prd").mkdir()
    (task_dir / "prd" / "prd.md").write_text("# PRD\n")

    plan_response = RunnerResult(text="# Plan: X", cost_usd=0.02, duration_s=1.0)
    runner_calls: list = []

    class _R:
        def run(self, *args, **kwargs):
            runner_calls.append(args)
            return plan_response

    with patch("code_loops.engine.PACKAGE_DIR", repo):
        engine = Engine(task_dir)
        engine.factory = FakeFactory(_R())
        from code_loops.stages.parallel import ParallelStage
        from code_loops.stages.prompt import PromptStage

        engine.handlers["prompt"] = PromptStage(engine.factory)
        engine.handlers["parallel"] = ParallelStage(engine.factory)
        engine.run()

    # Runner should be called exactly once (only for plan, not prd)
    assert len(runner_calls) == 1
    # plan stage should now be done
    meta_after = Manifest(task_dir / "manifest.json").data
    assert meta_after["stages"]["plan"]["status"] == "done"
