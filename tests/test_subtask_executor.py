"""Tests для SubtaskExecutorStage — new Aider-style executor.

Covers control flow via mock runner (no real subprocess для editor/replanner).
Heavy integration test для full pipeline kept out — Phase 4 validates that с
real CLI on real subtask.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.subtask_executor import (
    DesignEscalation,
    SubtaskExecutorError,
    SubtaskExecutorStage,
    _scope_test_files,
    _subtask_files,
)
from tests.conftest import FakeFactory, MockClaudeRunner

# ---- subtask file helpers ----


def test_subtask_files_merges_create_and_modify():
    st = {"files": {"create": ["a.py", "b.json"], "modify": ["c.py"]}}
    assert set(_subtask_files(st)) == {"a.py", "b.json", "c.py"}


def test_subtask_files_handles_empty():
    assert _subtask_files({}) == []
    assert _subtask_files({"files": {}}) == []


def test_scope_test_files_filters_to_test_py():
    st = {"files": {"create": ["app/foo.py", "tests/test_foo.py", "data.json"]}}
    assert _scope_test_files(st) == ["tests/test_foo.py"]


# ---- DesignEscalation ----


def test_design_escalation_carries_context():
    exc = DesignEscalation("subtask_x", "exhausted", "feedback here")
    assert exc.subtask_id == "subtask_x"
    assert exc.reason == "exhausted"
    assert exc.feedback == "feedback here"


# ---- _build_editor_input / _render_spec ----


def _stage():
    # FakeFactory with no runner — only used for helper methods
    return SubtaskExecutorStage(FakeFactory(MockClaudeRunner()))


def test_render_spec_includes_mode_and_acceptance():
    st = {
        "id": "test_x",
        "title": "Do thing",
        "mode": "baseline",
        "files": {"create": ["x.py"]},
        "acceptance": [{"type": "ruff_clean", "file": "x.py"}],
        "spec_md": "build it",
    }
    text = _stage()._render_spec(st)
    assert "test_x" in text
    assert "baseline" in text
    assert "ruff_clean" in text
    assert "build it" in text


def test_render_spec_default_mode_tdd():
    st = {"id": "x", "title": "T", "files": {"create": ["a.py"]}, "spec_md": "x"}
    text = _stage()._render_spec(st)
    assert "tdd" in text


def test_build_editor_input_includes_feedback_when_present():
    st = {"id": "x", "title": "T", "files": {"create": ["a.py"]}, "spec_md": "x"}
    wt = type("WT", (), {"path": Path("/tmp/wt")})
    inp = _stage()._build_editor_input(st, wt, feedback="prior fail message")
    assert "feedback_from_previous_attempt" in inp
    assert "prior fail message" in inp


def test_build_editor_input_skips_feedback_when_absent():
    st = {"id": "x", "title": "T", "files": {"create": ["a.py"]}, "spec_md": "x"}
    wt = type("WT", (), {"path": Path("/tmp/wt")})
    inp = _stage()._build_editor_input(st, wt, feedback=None)
    assert "feedback_from_previous_attempt" not in inp


# ---- _build_feedback ----


def test_build_feedback_includes_pytest_when_failed_non_baseline():
    verdict = {
        "passed": False,
        "mode": "tdd",
        "pytest_rc": 1,
        "pytest_log": "FAILED test_x.py::test_foo - AssertionError\nE   assert False\n",
        "ruff_rc": 0,
        "ruff_log": "",
        "acceptance_violations": [],
    }
    text = _stage()._build_feedback(verdict)
    assert "PYTEST FAILURE" in text
    assert "test_foo" in text


def test_build_feedback_skips_pytest_in_baseline_mode():
    """Baseline: failing tests expected, не блокирующее."""
    verdict = {
        "passed": False,
        "mode": "baseline",
        "pytest_rc": 1,
        "pytest_log": "16 xfailed, 1 passed",
        "ruff_rc": 0,
        "ruff_log": "",
        "acceptance_violations": [],
    }
    text = _stage()._build_feedback(verdict)
    assert "PYTEST" not in text  # baseline допускает pytest failures


def test_build_feedback_includes_ruff():
    verdict = {
        "passed": False,
        "mode": "tdd",
        "pytest_rc": 0,
        "pytest_log": "",
        "ruff_rc": 1,
        "ruff_log": "E501 line too long",
        "acceptance_violations": [],
    }
    text = _stage()._build_feedback(verdict)
    assert "RUFF" in text
    assert "E501" in text


# ---- _run_editor STUCK detection ----


def test_editor_stuck_signal_detected():
    """`STUCK` on its own line → status STUCK."""
    runner = MockClaudeRunner(
        responses={"editor": [RunnerResult(text="STUCK\n\nReason: spec impossible", cost_usd=1.0)]}
    )
    stage = SubtaskExecutorStage(FakeFactory(runner))
    # Mock minimal wt + ctx
    wt = type("WT", (), {"path": Path("/tmp/wt_test")})
    sub_dir = Path("/tmp/sub_test_stuck")
    sub_dir.mkdir(parents=True, exist_ok=True)
    ctx = type(
        "Ctx", (),
        {
            "repo_root": Path("/home/neondelph/dev/code-loops/src/code_loops"),
            "project_config": None,
            "artifact_writer": None,
        },
    )
    roles = {"editor": {"prompt": "agents/engineering/editor.md"}}
    subtask = {"id": "x", "title": "T", "files": {"create": ["a.py"]}, "spec_md": "x"}
    cost, status, response = stage._run_editor(
        roles, subtask, wt, sub_dir, ctx, None, 1
    )
    assert status == "STUCK"
    assert "spec impossible" in response


def test_editor_done_status_default():
    runner = MockClaudeRunner(
        responses={"editor": [RunnerResult(text="DONE\n\nChanged files:\n- a.py", cost_usd=0.5)]}
    )
    stage = SubtaskExecutorStage(FakeFactory(runner))
    wt = type("WT", (), {"path": Path("/tmp/wt_test")})
    sub_dir = Path("/tmp/sub_test_done")
    sub_dir.mkdir(parents=True, exist_ok=True)
    ctx = type(
        "Ctx", (),
        {
            "repo_root": Path("/home/neondelph/dev/code-loops/src/code_loops"),
            "project_config": None,
            "artifact_writer": None,
        },
    )
    roles = {"editor": {"prompt": "agents/engineering/editor.md"}}
    subtask = {"id": "x", "title": "T", "files": {"create": ["a.py"]}, "spec_md": "x"}
    cost, status, response = stage._run_editor(
        roles, subtask, wt, sub_dir, ctx, None, 1
    )
    assert status == "DONE"


def test_pre_check_skips_editor_when_acceptance_already_satisfied(monkeypatch, tmp_path):
    """If validator green BEFORE editor invocation (worktree state already meets
    acceptance), executor short-circuits — no editor call, no cost. Solves the
    rewind-duplicate problem where corrective_subtasks duplicate shipped work."""
    runner = MockClaudeRunner(default_responses=["should NOT be called"])
    stage = SubtaskExecutorStage(FakeFactory(runner))

    # Stub _run_validator to return passed=True (acceptance already met)
    def fake_validator(self, wt, sub_dir, subtask, test_infra):
        return {
            "passed": True, "mode": "tdd",
            "pytest_rc": 0, "pytest_log": "",
            "ruff_rc": 0, "ruff_log": "",
            "acceptance_violations": [],
        }
    monkeypatch.setattr(SubtaskExecutorStage, "_run_validator", fake_validator)

    # Stub project loader
    from code_loops import project_loader
    monkeypatch.setattr(
        project_loader, "get_test_infrastructure",
        lambda cfg: {"enabled": True, "test_paths": ["tests"], "lock_strategy": "none"},
    )

    wt = type("WT", (), {"path": tmp_path})
    sub_dir = tmp_path / "sub"
    sub_dir.mkdir(parents=True, exist_ok=True)
    ctx = type(
        "Ctx", (),
        {
            "repo_root": Path("/tmp"),
            "project_config": None,
            "artifact_writer": None,
            "task_dir": tmp_path,
        },
    )
    roles = {"editor": {"prompt": "agents/engineering/editor.md"}}
    subtask = {
        "id": "already_done", "title": "T",
        "files": {"create": ["a.py"]},
        "spec_md": "do thing",
        "acceptance": [{"type": "ruff_clean", "file": "a.py"}],
    }
    cost = stage._execute_subtask(subtask, ctx, wt, roles)
    assert cost == 0.0
    # Editor never invoked
    assert len(runner.calls) == 0


def test_editor_role_missing_raises():
    stage = SubtaskExecutorStage(FakeFactory(MockClaudeRunner()))
    wt = type("WT", (), {"path": Path("/tmp/wt_test")})
    sub_dir = Path("/tmp/sub_test_missing")
    sub_dir.mkdir(parents=True, exist_ok=True)
    ctx = type(
        "Ctx", (),
        {
            "repo_root": Path("/tmp"),
            "project_config": None,
            "artifact_writer": None,
        },
    )
    with pytest.raises(SubtaskExecutorError, match="editor.*not configured"):
        stage._run_editor({}, {"id": "x", "files": {}, "spec_md": "x"}, wt, sub_dir, ctx, None, 1)
