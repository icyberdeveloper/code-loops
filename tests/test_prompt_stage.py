"""Tests for PromptStage with a mocked runner (no real claude calls)."""

from __future__ import annotations

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.prompt import PromptStage, StageContext
from tests.conftest import FakeFactory


class FakeRunner:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.calls: list[tuple[str, str]] = []

    def run(self, system_prompt: str, user_message: str) -> RunnerResult:
        self.calls.append((system_prompt, user_message))
        return RunnerResult(text=self.response_text, cost_usd=0.05, duration_s=2.0)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "fake_writer.md").write_text("FAKE SYSTEM PROMPT")
    return tmp_path


@pytest.fixture
def task_dir(tmp_path):
    td = tmp_path / "task"
    td.mkdir()
    (td / "task.md").write_text("Add /export-data command")
    return td


def test_prompt_stage_writes_artifact(repo, task_dir):
    runner = FakeRunner("# PRD: Test\n\n## Problem\nNothing\n")
    stage = PromptStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage_def = {
        "name": "prd",
        "type": "prompt",
        "prompt": "agents/fake_writer.md",
        "inputs": ["task.md"],
        "outputs": ["prd/prd.md"],
    }
    result = stage.run(stage_def, ctx)
    out_path = task_dir / "prd" / "prd.md"
    assert out_path.exists()
    assert out_path.read_text().startswith("# PRD: Test")
    assert result["cost_usd"] == 0.05
    assert "prd/prd.md" in result["outputs"]


def test_prompt_stage_passes_inputs_to_runner(repo, task_dir):
    runner = FakeRunner("ok")
    stage = PromptStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage_def = {
        "name": "prd",
        "type": "prompt",
        "prompt": "agents/fake_writer.md",
        "inputs": ["task.md"],
        "outputs": ["prd/prd.md"],
    }
    stage.run(stage_def, ctx)
    sys_prompt, user_msg = runner.calls[0]
    assert sys_prompt == "FAKE SYSTEM PROMPT"
    assert "=== task.md ===" in user_msg
    assert "Add /export-data command" in user_msg


def test_prompt_stage_revision_mode_includes_feedback(repo, task_dir):
    runner = FakeRunner("revised")
    stage = PromptStage(FakeFactory(runner))
    fb = task_dir / "feedback.md"
    fb.write_text("Make problem section shorter.")
    prev = task_dir / "previous_prd.md"
    prev.write_text("# PRD: Test\n## Problem\nLong text.")
    ctx = StageContext(
        task_dir=task_dir,
        prompts_dir=repo / "agents",
        repo_root=repo,
        revision_inputs=[prev, fb],
    )
    stage_def = {
        "name": "prd",
        "type": "prompt",
        "prompt": "agents/fake_writer.md",
        "inputs": ["task.md"],
        "outputs": ["prd/prd.md"],
    }
    stage.run(stage_def, ctx)
    _, user_msg = runner.calls[0]
    assert "REVISION MODE" in user_msg
    assert "Make problem section shorter." in user_msg
    assert "previous_prd.md" in user_msg


def test_prompt_stage_missing_input_raises(repo, task_dir):
    runner = FakeRunner("ok")
    stage = PromptStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage_def = {
        "name": "prd",
        "type": "prompt",
        "prompt": "agents/fake_writer.md",
        "inputs": ["does_not_exist.md"],
        "outputs": ["prd/prd.md"],
    }
    with pytest.raises(FileNotFoundError):
        stage.run(stage_def, ctx)
