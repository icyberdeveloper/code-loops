"""Тесты для EvidenceExtractorStage — Phase 1 архитектор-цепочки."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.evidence_extractor import (
    EVIDENCE_ALLOWED_TOOLS,
    EVIDENCE_SCHEMA,
    EvidenceExtractorStage,
)
from code_loops.stages.prompt import StageContext
from tests.conftest import FakeFactory


class ScriptedRunner:
    """Mock runner — записывает kwargs и возвращает заранее заданный result."""

    def __init__(self, result: RunnerResult):
        self.result = result
        self.last_call: dict = {}

    def run(self, system_prompt: str, user_message: str, **kwargs) -> RunnerResult:
        self.last_call = {
            "system_prompt": system_prompt,
            "user_message": user_message,
            "output_schema": kwargs.get("output_schema"),
            "allowed_tools": kwargs.get("allowed_tools"),
        }
        return self.result


def _make_repo_and_task(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "agents" / "architects").mkdir(parents=True)
    (tmp_path / "agents" / "architects" / "software-architect-evidence.md").write_text(
        "EVIDENCE_AGENT_PROMPT"
    )
    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text("Fix bug X")
    (task_dir / "prd").mkdir()
    (task_dir / "prd" / "prd.md").write_text("# PRD\n...")
    (task_dir / "research_plan").mkdir()
    (task_dir / "research_plan" / "plan.md").write_text("# Plan\n...")
    (task_dir / "research").mkdir()
    (task_dir / "research" / "codebase.md").write_text("# Research codebase\n...")
    return tmp_path, task_dir


def _stage_def() -> dict:
    return {
        "name": "evidence",
        "type": "evidence_extractor",
        "prompt": "agents/architects/software-architect-evidence.md",
    }


def _evidence_json_payload() -> dict:
    return {
        "verified_files": ["app/foo.py", "app/bar.py"],
        "verified_symbols": [
            {"name": "do_x", "file": "app/foo.py", "line": 42, "kind": "function"}
        ],
        "file_quotes": [],
        "summary": "Bug lives in app/foo.py at do_x.",
    }


def test_evidence_extractor_saves_evidence_json(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    result = RunnerResult(
        text=json.dumps(_evidence_json_payload()),
        parsed_json=_evidence_json_payload(),
        cost_usd=0.5,
        duration_s=10.0,
        tool_events=[{"name": "Bash", "input": {"command": "grep -n do_x app/"}, "output": "..."}],
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    out = stage.run(_stage_def(), ctx)

    evidence_path = task_dir / "design" / "evidence.json"
    assert evidence_path.exists()
    saved = json.loads(evidence_path.read_text())
    assert saved == _evidence_json_payload()
    assert out["cost_usd"] == 0.5
    assert out["evidence"]["verified_files"] == ["app/foo.py", "app/bar.py"]


def test_evidence_extractor_passes_output_schema_and_allowed_tools(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    result = RunnerResult(
        text=json.dumps(_evidence_json_payload()),
        parsed_json=_evidence_json_payload(),
        cost_usd=0.5,
        duration_s=10.0,
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)

    assert runner.last_call["output_schema"] == EVIDENCE_SCHEMA
    assert runner.last_call["allowed_tools"] == EVIDENCE_ALLOWED_TOOLS


def test_evidence_extractor_saves_tool_transcript_when_tool_events_present(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    result = RunnerResult(
        text=json.dumps(_evidence_json_payload()),
        parsed_json=_evidence_json_payload(),
        cost_usd=0.5,
        duration_s=10.0,
        tool_events=[
            {"name": "Bash", "input": {"command": "grep foo"}, "output": "found"},
            {"name": "Read", "input": {"path": "app/foo.py"}, "output": "def foo"},
        ],
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)

    transcript_path = task_dir / "design" / "evidence_tool_transcript.md"
    assert transcript_path.exists()
    body = transcript_path.read_text()
    assert "Tool call #1: Bash" in body
    assert "Tool call #2: Read" in body
    assert "grep foo" in body
    assert "def foo" in body


def test_evidence_extractor_no_tool_transcript_when_no_events(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    result = RunnerResult(
        text=json.dumps(_evidence_json_payload()),
        parsed_json=_evidence_json_payload(),
        cost_usd=0.5,
        duration_s=10.0,
        tool_events=[],
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)
    # Transcript НЕ должен создаваться когда tool_events пуст
    assert not (task_dir / "design" / "evidence_tool_transcript.md").exists()


def test_evidence_extractor_raises_when_parsed_json_none(tmp_path):
    """Constrained decoding должен гарантировать parsed_json; если всё-таки
    пришёл None — raise с saved raw text для отладки."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    result = RunnerResult(
        text="malformed not json",
        parsed_json=None,
        cost_usd=0.5,
        duration_s=10.0,
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    with pytest.raises(RuntimeError, match="non-JSON"):
        stage.run(_stage_def(), ctx)
    # Сырой text должен быть сохранён для отладки
    assert (task_dir / "design" / "evidence_raw.txt").read_text() == "malformed not json"


def test_evidence_extractor_includes_redesign_signal_when_present(tmp_path):
    """В redesign-mode prompt включает redesign_signal."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    (task_dir / "design").mkdir(exist_ok=True)
    (task_dir / "design" / "redesign_signal.md").write_text("# Redesign signal\nRecurring theme: X")
    result = RunnerResult(
        text=json.dumps(_evidence_json_payload()),
        parsed_json=_evidence_json_payload(),
        cost_usd=0.5,
        duration_s=10.0,
    )
    runner = ScriptedRunner(result)
    stage = EvidenceExtractorStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)

    user_msg = runner.last_call["user_message"]
    assert "redesign_signal.md" in user_msg
    assert "Recurring theme: X" in user_msg
