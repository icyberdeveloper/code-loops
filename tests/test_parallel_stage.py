"""Tests for ParallelStage with mocked runner."""

from __future__ import annotations

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.parallel import ParallelStage
from code_loops.stages.prompt import StageContext
from tests.conftest import FakeFactory

PLAN_MD = """\
# Plan: X

## Research questions

1. **[codebase]** Where is X?
2. **[prompts]** Which prompts touch X?
3. **[problems_lessons]** Any prior incidents?
"""


class CountingRunner:
    """Records every call; returns deterministic responses keyed by spec."""

    def __init__(self, response_by_spec: dict[str, str]):
        self.response_by_spec = response_by_spec
        self.calls: list[tuple[str, str]] = []

    def run(self, system_prompt: str, user_message: str) -> RunnerResult:
        self.calls.append((system_prompt, user_message))
        # Identify which branch by looking at the user message tag
        for spec, text in self.response_by_spec.items():
            if f"[{spec}] only" in user_message:
                return RunnerResult(text=text, cost_usd=0.05, duration_s=2.0)
        return RunnerResult(text="(unknown branch)", cost_usd=0.0, duration_s=0.1)


def _make_repo_and_task(tmp_path):
    (tmp_path / "agents" / "research").mkdir(parents=True)
    (tmp_path / "agents" / "research" / "researcher-codebase.md").write_text("CODEBASE PROMPT")
    (tmp_path / "agents" / "research" / "researcher-prompts.md").write_text("PROMPTS PROMPT")
    (tmp_path / "agents" / "research" / "researcher-incidents.md").write_text("PL PROMPT")
    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text("Original task text")
    (task_dir / "research_plan").mkdir()
    (task_dir / "research_plan" / "plan.md").write_text(PLAN_MD)
    return tmp_path, task_dir


def test_parallel_runs_all_branches_and_writes_artifacts(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    runner = CountingRunner(
        {
            "codebase": "# Research: codebase\nfindings about code",
            "prompts": "# Research: prompts\nfindings about prompts",
            "problems_lessons": "# Research: problems & lessons\nno incidents",
        }
    )
    stage = ParallelStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage_def = {
        "name": "research",
        "type": "parallel",
        "inputs": ["task.md", "research_plan/plan.md"],
        "branches": [
            {
                "name": "codebase",
                "prompt": "agents/research/researcher-codebase.md",
                "outputs": ["research/codebase.md"],
            },
            {
                "name": "prompts",
                "prompt": "agents/research/researcher-prompts.md",
                "outputs": ["research/prompts.md"],
            },
            {
                "name": "problems_lessons",
                "prompt": "agents/research/researcher-incidents.md",
                "outputs": ["research/problems_lessons.md"],
            },
        ],
    }
    result = stage.run(stage_def, ctx)

    assert (task_dir / "research" / "codebase.md").read_text().startswith("# Research: codebase")
    assert (task_dir / "research" / "prompts.md").read_text().startswith("# Research: prompts")
    assert (
        (task_dir / "research" / "problems_lessons.md")
        .read_text()
        .startswith("# Research: problems & lessons")
    )
    assert len(runner.calls) == 3
    # Cost is summed; duration is max
    assert result["cost_usd"] == pytest.approx(0.15)
    assert result["duration_s"] == 2.0
    assert set(result["outputs"]) == {
        "research/codebase.md",
        "research/prompts.md",
        "research/problems_lessons.md",
    }


def test_parallel_isolates_inputs_per_branch(tmp_path):
    """Each branch sees only its own questions (hard isolation)."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    runner = CountingRunner({"codebase": "ok", "prompts": "ok", "problems_lessons": "ok"})
    stage = ParallelStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage_def = {
        "name": "research",
        "type": "parallel",
        "inputs": ["task.md", "research_plan/plan.md"],
        "branches": [
            {
                "name": "codebase",
                "prompt": "agents/research/researcher-codebase.md",
                "outputs": ["research/codebase.md"],
            },
            {
                "name": "prompts",
                "prompt": "agents/research/researcher-prompts.md",
                "outputs": ["research/prompts.md"],
            },
            {
                "name": "problems_lessons",
                "prompt": "agents/research/researcher-incidents.md",
                "outputs": ["research/problems_lessons.md"],
            },
        ],
    }
    stage.run(stage_def, ctx)

    by_spec: dict[str, str] = {}
    for sys_prompt, user_msg in runner.calls:
        if "CODEBASE PROMPT" in sys_prompt:
            by_spec["codebase"] = user_msg
        elif "PROMPTS PROMPT" in sys_prompt:
            by_spec["prompts"] = user_msg
        elif "PL PROMPT" in sys_prompt:
            by_spec["problems_lessons"] = user_msg

    # Codebase branch sees its own question
    assert "Where is X?" in by_spec["codebase"]
    # Codebase branch does NOT see agents/problems_lessons questions
    assert "Which prompts touch X?" not in by_spec["codebase"]
    assert "Any prior incidents?" not in by_spec["codebase"]
    # Prompts branch sees its own question only
    assert "Which prompts touch X?" in by_spec["prompts"]
    assert "Where is X?" not in by_spec["prompts"]
    # Problems & lessons sees its own only
    assert "Any prior incidents?" in by_spec["problems_lessons"]
    assert "Where is X?" not in by_spec["problems_lessons"]


def test_parallel_does_not_set_manifest_latest(tmp_path):
    """Bug A fix: parallel branches don't update manifest.latest.

    Each branch produces a distinct artifact; treating any one as "the
    latest" is arbitrary iteration-order semantics. The manifest's
    `latest` pointer for the parallel stage should remain unset.
    """
    from code_loops.artifact_writer import ArtifactWriter
    from code_loops.manifest import Manifest

    repo, task_dir = _make_repo_and_task(tmp_path)
    runner = CountingRunner(
        {
            "codebase": "# Codebase research",
            "prompts": "# Prompts research",
            "problems_lessons": "# Problems research",
        }
    )
    stage = ParallelStage(FakeFactory(runner))
    manifest = Manifest(task_dir / "manifest.json")
    manifest.init_task("0001_x", mode="feature")
    aw = ArtifactWriter(task_dir, manifest)
    ctx = StageContext(
        task_dir=task_dir,
        prompts_dir=repo / "agents",
        repo_root=repo,
        artifact_writer=aw,
    )
    stage_def = {
        "name": "research",
        "type": "parallel",
        "inputs": ["task.md", "research_plan/plan.md"],
        "branches": [
            {
                "name": "codebase",
                "prompt": "agents/research/researcher-codebase.md",
                "outputs": ["research/codebase.md"],
            },
            {
                "name": "prompts",
                "prompt": "agents/research/researcher-prompts.md",
                "outputs": ["research/prompts.md"],
            },
            {
                "name": "problems_lessons",
                "prompt": "agents/research/researcher-incidents.md",
                "outputs": ["research/problems_lessons.md"],
            },
        ],
    }
    stage.run(stage_def, ctx)

    # All 3 artifacts written
    assert (task_dir / "research" / "codebase.md").exists()
    assert (task_dir / "research" / "prompts.md").exists()
    assert (task_dir / "research" / "problems_lessons.md").exists()
    # But manifest.latest is NOT set (parallel stages don't have a single canonical "latest")
    research_entry = manifest.data["stages"].get("research", {})
    assert "latest" not in research_entry, (
        f"Parallel stage should not set manifest.latest; got: {research_entry.get('latest')!r}"
    )
