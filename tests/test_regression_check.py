"""Tests for Stage 9 regression_check action handler.

Covers skip-paths, baseline capture (first run), pass (within threshold),
fail (regression > threshold), and error paths (bench fails, bad JSON,
missing output). The subprocess call is mocked — tests never invoke a
real bench.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from code_loops.stages.prompt import StageContext


def _ctx(task_dir: Path, project_config: dict | None = None) -> StageContext:
    return StageContext(
        task_dir=task_dir,
        prompts_dir=task_dir,
        repo_root=task_dir,
        project_config=project_config,
    )


def _make_workspace(tmp_path: Path, monkeypatch) -> Path:
    """Set CODE_LOOPS_WORKSPACE so PROJECTS_DIR resolves under tmp_path; reload."""
    monkeypatch.setenv("CODE_LOOPS_WORKSPACE", str(tmp_path))
    import importlib

    import code_loops.project_loader
    import code_loops.stages.regression_check

    importlib.reload(code_loops.project_loader)
    importlib.reload(code_loops.stages.regression_check)
    return tmp_path


# ---- skip paths ----


def test_skips_when_disabled(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    cfg = {"project": {"name": "p", "base_repo": str(tmp_path)}, "regression": {"enabled": False}}
    out = run_regression_check({}, _ctx(tmp_path / "task", project_config=cfg))
    assert out["skipped"] is True
    assert "enabled=false" in out["reason"]


def test_skips_when_no_command(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    cfg = {
        "project": {"name": "p", "base_repo": str(tmp_path)},
        "regression": {"enabled": True, "output_path": "out.json"},
    }
    out = run_regression_check({}, _ctx(tmp_path / "task", project_config=cfg))
    assert out["skipped"] is True
    assert "command" in out["reason"]


def test_skips_when_no_output_path(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    cfg = {
        "project": {"name": "p", "base_repo": str(tmp_path)},
        "regression": {"enabled": True, "command": "echo hi"},
    }
    out = run_regression_check({}, _ctx(tmp_path / "task", project_config=cfg))
    assert out["skipped"] is True
    assert "output_path" in out["reason"]


def test_skips_when_no_worktree(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = tmp_path / "task"
    task.mkdir()
    cfg = {
        "project": {"name": "p", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "echo hi",
            "output_path": "out.json",
        },
    }
    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["skipped"] is True
    assert "worktree absent" in out["reason"]


# ---- happy paths ----


def _make_worktree(tmp_path: Path) -> Path:
    task = tmp_path / "task"
    wt = task / "worktree" / "wt"
    wt.mkdir(parents=True)
    return task


def _patch_subprocess(monkeypatch, returncode: int, output_path_rel: str, output_content: str):
    """Stub subprocess.run to fake the bench, optionally writing output_path."""

    def fake_run(cmd, cwd, capture_output, text, timeout):  # noqa: ARG001
        if returncode == 0 and output_content is not None:
            (Path(cwd) / output_path_rel).write_text(output_content)
        result = MagicMock()
        result.returncode = returncode
        result.stdout = ""
        result.stderr = "bench failed (stderr tail)" if returncode != 0 else ""
        return result

    monkeypatch.setattr("code_loops.stages.regression_check.subprocess.run", fake_run)


def test_captures_baseline_on_first_run(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "uv run pytest -m eval",
            "output_path": "eval.json",
            "threshold_pct": 5,
        },
    }
    _patch_subprocess(
        monkeypatch,
        returncode=0,
        output_path_rel="eval.json",
        output_content=json.dumps({"recall_at_10": 0.90, "mrr": 0.72}),
    )

    out = run_regression_check({}, _ctx(task, project_config=cfg))

    assert out["success"] is True
    assert out["baseline_captured"] is True
    assert out["regressed_metrics"] == []
    baseline_path = tmp_path / "projects" / "demo" / "baselines" / "eval.json"
    assert baseline_path.exists()
    saved = json.loads(baseline_path.read_text())
    assert saved["metrics"] == {"recall_at_10": 0.90, "mrr": 0.72}
    assert (task / "validation" / "regression.md").exists()


def test_passes_within_threshold(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    baseline_dir = tmp_path / "projects" / "demo" / "baselines"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "eval.json").write_text(
        json.dumps({"metrics": {"recall_at_10": 0.90, "mrr": 0.72}})
    )

    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "uv run pytest -m eval",
            "output_path": "eval.json",
            "threshold_pct": 5,
        },
    }
    # current within 5% of baseline (0.88 / 0.90 = 97.7% — passes)
    _patch_subprocess(
        monkeypatch,
        returncode=0,
        output_path_rel="eval.json",
        output_content=json.dumps({"recall_at_10": 0.88, "mrr": 0.71}),
    )

    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["success"] is True
    assert out["baseline_captured"] is False
    assert out["regressed_metrics"] == []


def test_fails_on_regression(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    baseline_dir = tmp_path / "projects" / "demo" / "baselines"
    baseline_dir.mkdir(parents=True)
    (baseline_dir / "eval.json").write_text(
        json.dumps({"metrics": {"recall_at_10": 0.90, "mrr": 0.72}})
    )

    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "uv run pytest -m eval",
            "output_path": "eval.json",
            "threshold_pct": 5,
        },
    }
    # recall_at_10 dropped 22% — way past threshold
    _patch_subprocess(
        monkeypatch,
        returncode=0,
        output_path_rel="eval.json",
        output_content=json.dumps({"recall_at_10": 0.70, "mrr": 0.72}),
    )

    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["success"] is False
    assert len(out["regressed_metrics"]) == 1
    r = out["regressed_metrics"][0]
    assert r["metric"] == "recall_at_10"
    assert r["baseline"] == 0.90
    assert r["current"] == 0.70
    assert r["delta_pct"] < -20  # ~-22%
    report = (task / "validation" / "regression.md").read_text()
    assert "regression" in report.lower()
    assert "recall_at_10" in report


# ---- error paths ----


def test_fails_when_bench_command_errors(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "false",
            "output_path": "eval.json",
        },
    }
    _patch_subprocess(monkeypatch, returncode=1, output_path_rel="eval.json", output_content=None)
    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["success"] is False
    assert "bench command failed" in out["reason"]
    assert "bench failed (stderr tail)" in (task / "validation" / "regression.md").read_text()


def test_fails_when_output_missing(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "echo ok",
            "output_path": "missing.json",
        },
    }
    _patch_subprocess(monkeypatch, returncode=0, output_path_rel="other.json", output_content=None)
    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["success"] is False
    assert "bench output not found" in out["reason"]


def test_fails_on_invalid_json(tmp_path: Path, monkeypatch):
    _make_workspace(tmp_path, monkeypatch)
    from code_loops.stages.regression_check import run_regression_check

    task = _make_worktree(tmp_path)
    cfg = {
        "project": {"name": "demo", "base_repo": str(tmp_path)},
        "regression": {
            "enabled": True,
            "command": "echo ok",
            "output_path": "eval.json",
        },
    }
    _patch_subprocess(
        monkeypatch,
        returncode=0,
        output_path_rel="eval.json",
        output_content="not valid json {{{",
    )
    out = run_regression_check({}, _ctx(task, project_config=cfg))
    assert out["success"] is False
    assert "not valid JSON" in out["reason"]


# ---- project_loader.get_regression_config ----


def test_get_regression_config_defaults():
    from code_loops.project_loader import get_regression_config

    out = get_regression_config(None)
    assert out == {"enabled": False, "command": None, "output_path": None, "threshold_pct": 5}


def test_get_regression_config_validates_threshold():
    import pytest

    from code_loops.project_loader import get_regression_config

    with pytest.raises(ValueError, match="threshold_pct"):
        get_regression_config({"regression": {"threshold_pct": -1}})
    with pytest.raises(ValueError, match="threshold_pct"):
        get_regression_config({"regression": {"threshold_pct": 101}})


def test_get_regression_config_validates_types():
    import pytest

    from code_loops.project_loader import get_regression_config

    with pytest.raises(ValueError, match="command"):
        get_regression_config({"regression": {"command": 42}})
    with pytest.raises(ValueError, match="output_path"):
        get_regression_config({"regression": {"output_path": ["x"]}})
