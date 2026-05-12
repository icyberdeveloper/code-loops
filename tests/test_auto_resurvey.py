"""Tests for Stage 11 auto_resurvey action handler.

Covers the skip-paths (no notes / no command / no project / no base_repo
/ no surveyor prompt) without invoking an LLM. The happy-path is verified
via monkeypatching RunnerFactory + a faked brief.md write — no real
`claude` subprocess.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from code_loops.runner import RunnerResult
from code_loops.stages.auto_resurvey import run_auto_resurvey
from code_loops.stages.prompt import StageContext


def _ctx(task_dir: Path, project_config: dict | None = None) -> StageContext:
    return StageContext(
        task_dir=task_dir,
        prompts_dir=task_dir,
        repo_root=task_dir,
        project_config=project_config,
    )


# ---- skip paths ----


def test_skips_when_no_maintenance_notes(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    out = run_auto_resurvey({}, _ctx(task))
    assert out["skipped"] is True
    assert "no maintenance_notes" in out["reason"]
    assert out["cost_usd"] == 0.0


def test_skips_when_notes_lack_resurvey_command(tmp_path: Path):
    task = tmp_path / "task"
    (task / "docs").mkdir(parents=True)
    (task / "docs" / "maintenance_notes.md").write_text(
        "# Maintenance notes\n\nnothing important — no resurvey command here.\n"
    )
    out = run_auto_resurvey({}, _ctx(task))
    assert out["skipped"] is True
    assert "no resurvey command" in out["reason"]
    assert out["cost_usd"] == 0.0


def test_skips_when_no_project_config(tmp_path: Path):
    task = tmp_path / "task"
    (task / "docs").mkdir(parents=True)
    (task / "docs" / "maintenance_notes.md").write_text(
        "## Maintenance notes\n\n[ ] Run `code-loops resurvey foo` because:\n- X\n"
    )
    out = run_auto_resurvey({}, _ctx(task, project_config=None))
    assert out["skipped"] is True
    assert "no project config" in out["reason"]


def test_skips_when_base_repo_missing(tmp_path: Path):
    task = tmp_path / "task"
    (task / "docs").mkdir(parents=True)
    (task / "docs" / "maintenance_notes.md").write_text(
        "## Maintenance notes\n\n[ ] Run `code-loops resurvey foo` because:\n- X\n"
    )
    cfg = {"project": {"name": "foo", "base_repo": str(tmp_path / "does_not_exist")}}
    out = run_auto_resurvey({}, _ctx(task, project_config=cfg))
    assert out["skipped"] is True
    assert "base_repo missing" in out["reason"]


# ---- happy path ----


def test_invokes_surveyor_and_writes_brief(tmp_path: Path, monkeypatch):
    # Set up workspace so PROJECTS_DIR resolves to tmp_path/projects
    monkeypatch.setenv("CODE_LOOPS_WORKSPACE", str(tmp_path))
    # Re-import to pick up the new env var (PROJECTS_DIR is module-level)
    import importlib

    import code_loops.project_loader
    import code_loops.stages.auto_resurvey

    importlib.reload(code_loops.project_loader)
    importlib.reload(code_loops.stages.auto_resurvey)
    from code_loops.stages.auto_resurvey import run_auto_resurvey as run_fresh

    base_repo = tmp_path / "user_project"
    base_repo.mkdir()
    target_dir = tmp_path / "projects" / "demo"
    target_dir.mkdir(parents=True)

    task = tmp_path / "task"
    (task / "docs").mkdir(parents=True)
    (task / "docs" / "maintenance_notes.md").write_text(
        "## Maintenance notes\n\n[ ] Run `code-loops resurvey demo` because:\n"
        "- new src/feature/foo.py module\n"
    )

    # Fake RunnerFactory: returns a runner whose .run() writes brief.md
    # (simulates surveyor's Write tool effect) and returns a RunnerResult
    fake_runner = MagicMock()

    def fake_run(sys_prompt: str, user_msg: str, cwd: str | None = None):
        (target_dir / "brief.md").write_text("# Project Brief: demo\n\nfresh content\n")
        return RunnerResult(text="brief written", cost_usd=0.42, duration_s=12.0)

    fake_runner.run = fake_run

    fake_factory = MagicMock()
    fake_factory.make = MagicMock(return_value=fake_runner)

    monkeypatch.setattr(
        "code_loops.stages.auto_resurvey.RunnerFactory",
        lambda: fake_factory,
    )

    cfg = {"project": {"name": "demo", "base_repo": str(base_repo)}}
    out = run_fresh({}, _ctx(task, project_config=cfg))

    assert out["skipped"] is False
    assert out["success"] is True
    assert out["cost_usd"] == 0.42
    assert (target_dir / "brief.md").exists()
    assert "fresh content" in (target_dir / "brief.md").read_text()
    # Surveyor was invoked with Opus max
    fake_factory.make.assert_called_once()
    spec = fake_factory.make.call_args[0][0]
    assert spec["model"] == "claude-opus-4-7"
    assert spec["effort"] == "max"


def test_reports_failure_when_surveyor_doesnt_write_brief(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODE_LOOPS_WORKSPACE", str(tmp_path))
    import importlib

    import code_loops.project_loader
    import code_loops.stages.auto_resurvey

    importlib.reload(code_loops.project_loader)
    importlib.reload(code_loops.stages.auto_resurvey)
    from code_loops.stages.auto_resurvey import run_auto_resurvey as run_fresh

    base_repo = tmp_path / "user_project"
    base_repo.mkdir()
    (tmp_path / "projects" / "demo").mkdir(parents=True)

    task = tmp_path / "task"
    (task / "docs").mkdir(parents=True)
    (task / "docs" / "maintenance_notes.md").write_text(
        "## Maintenance notes\n\n[ ] Run `code-loops resurvey demo` because:\n- X\n"
    )

    # Fake runner that "succeeds" but fails to write brief.md (e.g. wrote to wrong path)
    fake_runner = MagicMock()
    fake_runner.run = MagicMock(
        return_value=RunnerResult(text="hmm", cost_usd=0.10, duration_s=5.0)
    )
    fake_factory = MagicMock()
    fake_factory.make = MagicMock(return_value=fake_runner)
    monkeypatch.setattr(
        "code_loops.stages.auto_resurvey.RunnerFactory",
        lambda: fake_factory,
    )

    cfg = {"project": {"name": "demo", "base_repo": str(base_repo)}}
    out = run_fresh({}, _ctx(task, project_config=cfg))

    assert out["skipped"] is False
    assert out["success"] is False
    assert out["reason"] == "brief.md not written"
    assert out["cost_usd"] == 0.10
