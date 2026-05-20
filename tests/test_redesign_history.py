"""Тесты для redesign_history.md — cross-pass summary для architect.

Architect должен видеть pattern across passes, не only last redesign_signal.
Это решает limitation Reflexion (Meta-Policy Reflexion, arxiv 2509.03990):
"reflections produce ephemeral task-specific traces not reused across tasks"
→ architect repeats class of mistake между passes.
"""

from __future__ import annotations

import json
from pathlib import Path

from code_loops.stages.debate_critique import (
    _extract_chosen_shape_from_pass,
    _write_redesign_history,
)


def _write_pass_json(task_dir: Path, pass_n: int, version: int, chosen_shape: str):
    """Helper: пишет draft_vN.json в pass_<N> с заданным chosen shape."""
    pass_dir = task_dir / "design" / f"pass_{pass_n}"
    pass_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "title": "test",
        "shapes_considered": {"chosen": chosen_shape},
    }
    (pass_dir / f"draft_v{version}.json").write_text(json.dumps(data))


def _write_pass_md(task_dir: Path, pass_n: int, chosen_shape: str):
    """Helper: пишет final.md в pass_<N> с chosen shape (для markdown fallback)."""
    pass_dir = task_dir / "design" / f"pass_{pass_n}"
    pass_dir.mkdir(parents=True, exist_ok=True)
    (pass_dir / "final.md").write_text(
        f"## Shapes considered\n\n**Cross-axis chosen shape: {chosen_shape}.**\n\n# RFC: test\n"
    )


# ---- _extract_chosen_shape_from_pass ----


def test_extract_shape_from_json_prefers_latest_version(tmp_path):
    _write_pass_json(tmp_path, 1, version=1, chosen_shape="C × F")
    _write_pass_json(tmp_path, 1, version=2, chosen_shape="B × F")
    _write_pass_json(tmp_path, 1, version=3, chosen_shape="D × F")  # latest
    assert _extract_chosen_shape_from_pass(tmp_path, 1) == "D × F"


def test_extract_shape_falls_back_to_markdown_when_no_json(tmp_path):
    _write_pass_md(tmp_path, 1, "E × F")
    assert _extract_chosen_shape_from_pass(tmp_path, 1) == "E × F"


def test_extract_shape_returns_none_when_pass_missing(tmp_path):
    assert _extract_chosen_shape_from_pass(tmp_path, 5) is None


def test_extract_shape_handles_malformed_json(tmp_path):
    pass_dir = tmp_path / "design" / "pass_1"
    pass_dir.mkdir(parents=True)
    (pass_dir / "draft_v1.json").write_text("not valid json {")
    # malformed JSON — silently skip, fallback на markdown (тут тоже нет) → None
    assert _extract_chosen_shape_from_pass(tmp_path, 1) is None


def test_extract_shape_json_without_chosen_field_falls_back(tmp_path):
    """JSON есть но без shapes_considered.chosen → fallback на markdown."""
    pass_dir = tmp_path / "design" / "pass_1"
    pass_dir.mkdir(parents=True)
    (pass_dir / "draft_v1.json").write_text(json.dumps({"title": "x"}))
    _write_pass_md(tmp_path, 1, "C × G")
    assert _extract_chosen_shape_from_pass(tmp_path, 1) == "C × G"


# ---- _write_redesign_history ----


def test_write_redesign_history_skips_when_no_priors(tmp_path):
    """pass_1 first redesign — нет prior passes, файл не создаётся."""
    (tmp_path / "design").mkdir()
    _write_redesign_history(tmp_path, current_pass_n=1, prior_verdicts=[])
    assert not (tmp_path / "design" / "redesign_history.md").exists()


def test_write_redesign_history_creates_file_with_pass_summaries(tmp_path):
    (tmp_path / "design").mkdir()
    _write_pass_json(tmp_path, 1, 1, "C × F")
    _write_pass_json(tmp_path, 2, 1, "B × F")
    priors = [
        {"pass_n": 1, "recurring_theme": "alpha_theme", "verdict_status": "redesign_needed"},
        {"pass_n": 2, "recurring_theme": "beta_theme", "verdict_status": "redesign_needed"},
    ]
    _write_redesign_history(tmp_path, current_pass_n=3, prior_verdicts=priors)
    history = (tmp_path / "design" / "redesign_history.md").read_text()
    assert "## Pass 1" in history
    assert "## Pass 2" in history
    assert "C × F" in history
    assert "B × F" in history
    assert "alpha_theme" in history
    assert "beta_theme" in history


def test_write_redesign_history_marks_recurring_themes(tmp_path):
    """Если та же theme в нескольких passes — second instance помечается
    `(RECURRENCE)` чтобы architect мгновенно видел repeat."""
    (tmp_path / "design").mkdir()
    _write_pass_json(tmp_path, 1, 1, "C × F")
    _write_pass_json(tmp_path, 2, 1, "B × F")
    priors = [
        {
            "pass_n": 1,
            "recurring_theme": "partition_completeness",
            "verdict_status": "redesign_needed",
        },
        {
            "pass_n": 2,
            "recurring_theme": "partition_completeness",
            "verdict_status": "redesign_needed",
        },
    ]
    _write_redesign_history(tmp_path, current_pass_n=3, prior_verdicts=priors)
    history = (tmp_path / "design" / "redesign_history.md").read_text()
    # First mention — без marker, second — с (RECURRENCE)
    assert "partition_completeness`\n" in history  # First, plain
    assert "(RECURRENCE)" in history


def test_write_redesign_history_includes_pattern_summary_when_recurring(tmp_path):
    """Если есть recurring theme (≥2 instances) — pattern summary в конце с
    explicit guidance reformulate vs layer-shift."""
    (tmp_path / "design").mkdir()
    _write_pass_json(tmp_path, 1, 1, "C × F")
    _write_pass_json(tmp_path, 2, 1, "B × F")
    priors = [
        {"pass_n": 1, "recurring_theme": "x_theme", "verdict_status": "redesign_needed"},
        {"pass_n": 2, "recurring_theme": "x_theme", "verdict_status": "redesign_needed"},
    ]
    _write_redesign_history(tmp_path, current_pass_n=3, prior_verdicts=priors)
    history = (tmp_path / "design" / "redesign_history.md").read_text()
    assert "## Pattern detected" in history
    assert "x_theme` recurred 2 times" in history
    assert "framing, not in layer choice" in history
    assert "Axis-3" in history


def test_write_redesign_history_no_pattern_summary_when_unique_themes(tmp_path):
    """Different theme'ы каждый pass — pattern summary не emit'ится (нет
    recurrence к escalate'у)."""
    (tmp_path / "design").mkdir()
    _write_pass_json(tmp_path, 1, 1, "C × F")
    priors = [
        {"pass_n": 1, "recurring_theme": "alpha", "verdict_status": "redesign_needed"},
    ]
    _write_redesign_history(tmp_path, current_pass_n=2, prior_verdicts=priors)
    history = (tmp_path / "design" / "redesign_history.md").read_text()
    assert "## Pattern detected" not in history


def test_write_redesign_history_handles_missing_chosen_shape(tmp_path):
    """Если pass без JSON и без final.md — chosen marked as `?`, не raise."""
    (tmp_path / "design").mkdir()
    # Никаких pass_1 артефактов
    priors = [
        {"pass_n": 1, "recurring_theme": "theme_x", "verdict_status": "redesign_needed"},
    ]
    _write_redesign_history(tmp_path, current_pass_n=2, prior_verdicts=priors)
    history = (tmp_path / "design" / "redesign_history.md").read_text()
    assert "Chosen shape**: `?`" in history
