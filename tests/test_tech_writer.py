"""Tests for TechWriterStage extractors + happy-path run."""

from __future__ import annotations

from pathlib import Path

from code_loops.runner import RunnerResult
from code_loops.stages.prompt import StageContext
from code_loops.stages.tech_writer import (
    TechWriterStage,
    _extract_adr,
    _extract_changelog_bullet,
    _extract_maintenance_notes,
)
from tests.conftest import FakeFactory

# ---- _extract_changelog_bullet ----


def test_changelog_bullet_extracted_from_section():
    text = """\
brief intro

### 1. changelog_entry.md

- Reports now omit irrelevant filtered items (RFC: Plan D)

### 2. ADR
no adr needed
"""
    assert "Reports now omit" in _extract_changelog_bullet(text)


def test_changelog_bullet_first_match_when_no_section():
    text = "- The bullet (RFC: Foo)\n- another\n"
    assert _extract_changelog_bullet(text).startswith("- The bullet")


def test_changelog_bullet_fallback_when_missing():
    out = _extract_changelog_bullet("no bullets here at all")
    assert "no changelog bullet emitted" in out


# ---- _extract_adr ----


def test_adr_skipped_when_explicitly_marked():
    text = "## adr\n# ADR: not needed\n\nbecause of trivial change\n"
    fname, body = _extract_adr(text)
    assert fname == "0000-not-needed.md"
    assert "not needed" in body.lower()


def test_adr_extracted_with_number_and_slug():
    text = """\
intro

# ADR 0042: New Storage Layer For Embeddings

**Status:** Accepted
**Date:** 2026-05-09

## Context
We need...
"""
    fname, body = _extract_adr(text)
    assert fname == "0042-new-storage-layer-for-embeddings.md"
    assert body.startswith("# ADR 0042:")
    assert "Status:" in body


def test_adr_fallback_when_block_absent():
    text = "no adr block whatsoever, just changelog stuff"
    fname, body = _extract_adr(text)
    assert fname == "0000-not-needed.md"


# ---- full stage run ----


class _ScriptedRunner:
    def __init__(self, response_text: str):
        self.response_text = response_text

    def run(self, sys_prompt: str, user_msg: str) -> RunnerResult:
        return RunnerResult(text=self.response_text, cost_usd=0.10, duration_s=2.0)


def _make_repo_and_task(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "agents" / "release").mkdir(parents=True)
    (tmp_path / "agents" / "release" / "tech-writer.md").write_text("TECH_WRITER")
    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "prd").mkdir()
    (task_dir / "prd" / "prd.md").write_text("# PRD: foo")
    (task_dir / "design").mkdir()
    (task_dir / "design" / "final.md").write_text("# RFC: Foo solution")
    (task_dir / "implementation").mkdir()
    (task_dir / "implementation" / "_full_diff.patch").write_text("diff content")
    (task_dir / "implementation" / "_files_changed.txt").write_text("app/foo.py\n")
    (task_dir / "manifest.json").write_text(
        '{"task_id": "0001_x", "mode": "feature", "total_cost_usd": 0.0}'
    )
    return tmp_path, task_dir


def _stage_def() -> dict:
    return {
        "name": "release_docs",
        "type": "tech_writer",
        "prompt": "agents/release/tech-writer.md",
        "inputs": ["prd/prd.md", "design/final.md", "implementation/_full_diff.patch"],
        "outputs": ["docs/changelog_entry.md", "docs/adr/"],
    }


def test_tech_writer_writes_changelog_and_adr(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    response = """\
### 1. changelog_entry.md

- New /export-data command: weekly meetings as Markdown document (RFC: Foo solution)

### 2. ADR

# ADR 0001: New Export Module

**Status:** Accepted
**Date:** 2026-05-09
**Task:** `0001_x`

## Context
Need a place for export logic.

## Decision
Create src/feature/export.py.

## Consequences
- Positive: clear boundary
- Negative: 1 more file
"""
    runner = _ScriptedRunner(response)
    stage = TechWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    cl = (task_dir / "docs" / "changelog_entry.md").read_text()
    assert "/export-data" in cl
    adrs = list((task_dir / "docs" / "adr").glob("*.md"))
    assert len(adrs) == 1
    assert "0001-new-export-module" in adrs[0].name
    assert "# ADR 0001:" in adrs[0].read_text()
    assert result["adr_skipped"] is False
    assert result["cost_usd"] == 0.10


def test_tech_writer_emits_skipped_adr_for_trivial_change(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    response = """\
### 1. changelog_entry.md

- Fixed typo in briefing prompt (RFC: typo fix)

### 2. ADR

# ADR: not needed

Trivial change, no architectural decision.
"""
    runner = _ScriptedRunner(response)
    stage = TechWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    assert result["adr_skipped"] is True
    adr_file = task_dir / "docs" / "adr" / "0000-not-needed.md"
    assert adr_file.exists()
    assert "not needed" in adr_file.read_text().lower()


# ---- _extract_maintenance_notes ----


def test_maintenance_notes_returns_none_when_section_absent():
    text = "## changelog\n- foo\n\n## ADR\n# ADR: not needed\n"
    assert _extract_maintenance_notes(text) is None


def test_maintenance_notes_returns_none_when_section_empty():
    text = "## Maintenance notes\n\n## next section\n"
    assert _extract_maintenance_notes(text) is None


def test_maintenance_notes_returns_none_when_only_no_signal_markers():
    text = (
        "## Maintenance notes\n\n"
        "### Brief refresh\n"
        "✓ Brief stays accurate — no resurvey needed.\n\n"
        "### CLAUDE.md updates suggested\n"
        "✓ No new conventions to document.\n"
    )
    assert _extract_maintenance_notes(text) is None


def test_maintenance_notes_extracts_when_resurvey_suggested():
    text = (
        "blah\n\n"
        "## Maintenance notes\n\n"
        "### Brief refresh\n"
        "[ ] Run `code-loops resurvey demo` because:\n"
        "- new app/foo/ subpackage created\n\n"
        "### CLAUDE.md updates suggested\n"
        "✓ No new conventions to document.\n\n"
        "## something else\n"
    )
    out = _extract_maintenance_notes(text)
    assert out is not None
    assert out.startswith("# Maintenance notes\n\n")
    assert "code-loops resurvey demo" in out
    assert "new app/foo/" in out
    # Slice stops at next ## header
    assert "something else" not in out


def test_maintenance_notes_no_signal_without_resurvey_command():
    """Section present but missing the actual `code-loops resurvey` invocation."""
    text = "## Maintenance notes\n\nJust a vague mention of resurvey but no command invocation.\n"
    assert _extract_maintenance_notes(text) is None


def test_full_run_writes_maintenance_notes_when_present(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    response = """\
### 1. changelog_entry.md

- New /foo command added (RFC: foo)

### 2. ADR

# ADR: not needed

trivial.

## Maintenance notes

### Brief refresh

[ ] Run `code-loops resurvey smoke` because:
- new app/foo/ package created

### CLAUDE.md updates suggested

✓ No new conventions to document.
"""
    runner = _ScriptedRunner(response)
    stage = TechWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    notes_path = task_dir / "docs" / "maintenance_notes.md"
    assert notes_path.exists()
    body = notes_path.read_text()
    assert body.startswith("# Maintenance notes\n\n")
    assert "code-loops resurvey smoke" in body
    assert result["maintenance_present"] is True
    assert "docs/maintenance_notes.md" in result["outputs"]


def test_full_run_skips_maintenance_file_when_no_signal(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    # Response has NO ## Maintenance notes section at all
    response = """\
### 1. changelog_entry.md

- Fixed typo (RFC: typo fix)

### 2. ADR

# ADR: not needed

Trivial change.
"""
    runner = _ScriptedRunner(response)
    stage = TechWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    notes_path = task_dir / "docs" / "maintenance_notes.md"
    assert not notes_path.exists()
    assert result["maintenance_present"] is False
    assert "docs/maintenance_notes.md" not in result["outputs"]
