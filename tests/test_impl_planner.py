"""Tests for ImplPlannerStage parser, validator, and run flow."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from code_loops.runner import RunnerResult
from code_loops.stages.impl_planner import (
    ImplPlannerError,
    ImplPlannerStage,
    _build_summary,
    _split_plan_and_subtasks,
    _validate_subtasks,
)
from code_loops.stages.prompt import StageContext
from tests.conftest import FakeFactory

# ---- _split_plan_and_subtasks ----


VALID_OUTPUT = """\
# Implementation Plan: Foo

## Strategy
Land helper first, then wire it into the caller.

## Subtasks

```yaml
subtasks:
  - id: extract_helper
    title: Extract pure relevance helper
    files:
      create: [src/feature/foo.py]
    spec_md: |
      Build pure function `score(entry)`.
      Tests:
      - test_score_returns_zero_for_empty
  - id: wire_helper
    title: Wire helper into context builder
    files:
      modify: [src/feature/bar.py]
    depends_on: [extract_helper]
    spec_md: |
      Call score(...) inside build_context.
```
"""


def test_split_extracts_yaml_and_strips_from_plan():
    plan, data = _split_plan_and_subtasks(VALID_OUTPUT)
    assert "Land helper first" in plan
    assert "subtasks:" not in plan  # yaml block was stripped from plan text
    assert isinstance(data, dict)
    assert len(data["subtasks"]) == 2
    assert data["subtasks"][0]["id"] == "extract_helper"


def test_split_no_yaml_block_raises():
    with pytest.raises(ImplPlannerError, match="No `subtasks:` YAML"):
        _split_plan_and_subtasks("# Plan\n\nno yaml here\n")


def test_split_invalid_yaml_raises():
    bad = "# Plan\n\n```yaml\nsubtasks:\n  - id: foo\n  bad indent\n```\n"
    with pytest.raises(ImplPlannerError, match="YAML parse error"):
        _split_plan_and_subtasks(bad)


# ---- _validate_subtasks ----


def _data(*subtasks):
    return {"subtasks": list(subtasks)}


def _st(id="extract_helper", **overrides):
    base = {
        "id": id,
        "title": "Extract helper",
        "files": {"create": ["app/foo.py"]},
        "spec_md": "do the thing",
    }
    base.update(overrides)
    return base


def test_validate_passes_minimal():
    _validate_subtasks(_data(_st()))


def test_validate_rejects_missing_subtasks_key():
    with pytest.raises(ImplPlannerError, match="`subtasks` is required"):
        _validate_subtasks({})


def test_validate_rejects_empty_subtasks():
    with pytest.raises(ImplPlannerError, match="non-empty list"):
        _validate_subtasks({"subtasks": []})


def test_validate_rejects_non_snake_case_id():
    bad = _st(id="ExtractHelper")
    with pytest.raises(ImplPlannerError, match="snake_case"):
        _validate_subtasks(_data(bad))


def test_validate_rejects_id_starting_with_digit():
    bad = _st(id="1_helper")
    with pytest.raises(ImplPlannerError, match="snake_case"):
        _validate_subtasks(_data(bad))


def test_validate_rejects_duplicate_ids():
    a = _st(id="foo")
    b = _st(id="foo")
    with pytest.raises(ImplPlannerError, match="duplicated"):
        _validate_subtasks(_data(a, b))


def test_validate_rejects_missing_files_key():
    bad = _st()
    del bad["files"]
    with pytest.raises(ImplPlannerError, match="missing required key `files`"):
        _validate_subtasks(_data(bad))


def test_validate_rejects_unknown_files_subkey():
    bad = _st(files={"add": ["a.py"]})
    with pytest.raises(ImplPlannerError, match="unknown keys"):
        _validate_subtasks(_data(bad))


def test_validate_rejects_empty_files_list():
    bad = _st(files={"create": []})
    with pytest.raises(ImplPlannerError, match="omit the key entirely"):
        _validate_subtasks(_data(bad))


def test_validate_rejects_forward_dependency():
    a = _st(id="needs_b", depends_on=["b"])
    b = _st(id="b")
    with pytest.raises(ImplPlannerError, match="forward references not allowed"):
        _validate_subtasks(_data(a, b))


def test_validate_accepts_backward_dependency():
    a = _st(id="a")
    b = _st(id="b", depends_on=["a"])
    _validate_subtasks(_data(a, b))


# ---- wave validation ----


def test_wave_field_accepts_non_negative_int():
    _validate_subtasks(
        _data(
            _st(id="a", files={"create": ["app/foo.py"]}, wave=0),
            _st(id="b", files={"create": ["app/bar.py"]}, wave=1),
        )
    )


def test_wave_field_rejects_negative():
    with pytest.raises(ImplPlannerError, match="non-negative integer"):
        _validate_subtasks(_data(_st(id="a", wave=-1)))


def test_wave_field_rejects_non_integer():
    with pytest.raises(ImplPlannerError, match="non-negative integer"):
        _validate_subtasks(_data(_st(id="a", wave="zero")))


def test_wave_field_rejects_bool():
    # bool is technically int in Python; we reject explicitly
    with pytest.raises(ImplPlannerError, match="non-negative integer"):
        _validate_subtasks(_data(_st(id="a", wave=True)))


def test_wave_mates_cannot_share_files():
    a = _st(id="a", files={"modify": ["app/foo.py"]}, wave=0)
    b = _st(id="b", files={"modify": ["app/foo.py"]}, wave=0)
    with pytest.raises(ImplPlannerError, match="MUST share no files"):
        _validate_subtasks(_data(a, b))


def test_wave_mates_can_touch_disjoint_files():
    a = _st(id="a", files={"modify": ["app/foo.py"]}, wave=0)
    b = _st(id="b", files={"modify": ["app/bar.py"]}, wave=0)
    _validate_subtasks(_data(a, b))


def test_wave_mates_cannot_have_inter_dep():
    a = _st(id="a", files={"create": ["app/x.py"]}, wave=0)
    b = _st(id="b", files={"create": ["app/y.py"]}, wave=0, depends_on=["a"])
    with pytest.raises(ImplPlannerError, match="MUST be independent"):
        _validate_subtasks(_data(a, b))


def test_cross_wave_dependency_allowed():
    a = _st(id="a", files={"create": ["app/x.py"]}, wave=0)
    b = _st(id="b", files={"create": ["app/y.py"]}, wave=1, depends_on=["a"])
    _validate_subtasks(_data(a, b))


def test_needs_field_accepts_list_of_role_names():
    _validate_subtasks(
        _data(
            _st(id="a", needs=["prompt_engineer"]),
            _st(id="b", needs=["prompt_engineer", "eval_engineer"]),
        )
    )


def test_needs_field_rejects_non_list():
    with pytest.raises(ImplPlannerError, match="non-empty role-name strings"):
        _validate_subtasks(_data(_st(id="a", needs="prompt_engineer")))


def test_needs_field_rejects_non_string_entries():
    with pytest.raises(ImplPlannerError, match="non-empty role-name strings"):
        _validate_subtasks(_data(_st(id="a", needs=["prompt_engineer", 42])))


def test_needs_field_rejects_empty_string_entries():
    with pytest.raises(ImplPlannerError, match="non-empty role-name strings"):
        _validate_subtasks(_data(_st(id="a", needs=["prompt_engineer", ""])))


def test_subtasks_without_explicit_wave_skip_wave_checks():
    # Two subtasks touching the same file, no explicit wave — should pass
    # (wave grouping is opt-in).
    a = _st(id="a", files={"modify": ["app/shared.py"]})
    b = _st(id="b", files={"modify": ["app/shared.py"]})
    _validate_subtasks(_data(a, b))


# ---- _build_summary ----


def test_summary_renders_subtask_table():
    data = _data(
        _st(id="a", title="Alpha"),
        _st(id="b", title="Beta", files={"create": ["x.py"], "modify": ["y.py"]}, depends_on=["a"]),
    )
    summary = _build_summary(data)
    assert "Subtasks (2)" in summary
    assert "a: Alpha" in summary
    assert "b: Beta" in summary
    assert "[+1]" in summary  # alpha: 1 create
    assert "[+1 ~1]" in summary  # beta: 1 create, 1 modify
    assert "← a" in summary  # dependency arrow


# ---- ScriptedRunner + full stage run ----


class ScriptedRunner:
    def __init__(self, responses: list[RunnerResult]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def run(self, system_prompt: str, user_message: str) -> RunnerResult:
        self.calls.append((system_prompt, user_message))
        if not self.responses:
            raise RuntimeError("ScriptedRunner ran out of responses")
        return self.responses.pop(0)


def _make_repo_and_task(tmp_path: Path) -> tuple[Path, Path]:
    (tmp_path / "agents" / "strategy").mkdir(parents=True)
    (tmp_path / "agents" / "strategy" / "tech-lead.md").write_text("PLANNER")
    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "design").mkdir()
    (task_dir / "design" / "final.md").write_text("# RFC\n\n## File-level changes\n- foo")
    return tmp_path, task_dir


def _stage_def() -> dict:
    return {
        "name": "impl_plan",
        "type": "impl_planner",
        "prompt": "agents/strategy/tech-lead.md",
        "inputs": ["design/final.md"],
        "outputs": ["impl_plan/plan.md", "impl_plan/subtasks.yaml"],
    }


def test_impl_planner_writes_both_artifacts(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    runner = ScriptedRunner([RunnerResult(text=VALID_OUTPUT, cost_usd=0.50, duration_s=10.0)])
    stage = ImplPlannerStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    plan_path = task_dir / "impl_plan" / "plan.md"
    subtasks_path = task_dir / "impl_plan" / "subtasks.yaml"
    assert plan_path.exists()
    assert "Land helper first" in plan_path.read_text()
    assert "subtasks:" not in plan_path.read_text()
    assert subtasks_path.exists()
    parsed = yaml.safe_load(subtasks_path.read_text())
    assert len(parsed["subtasks"]) == 2
    assert result["subtask_count"] == 2
    assert "Subtasks (2)" in result["summary"]


def test_impl_planner_retries_once_on_validation_error(tmp_path):
    """Attempt 1 returns invalid yaml; engine retries with feedback; attempt 2 passes."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    bad_output = "# Plan\n\n```yaml\nsubtasks:\n  - id: BadId\n    title: x\n    files:\n      create: [a.py]\n    spec_md: y\n```\n"
    runner = ScriptedRunner(
        [
            RunnerResult(text=bad_output, cost_usd=0.30, duration_s=5.0),
            RunnerResult(text=VALID_OUTPUT, cost_usd=0.30, duration_s=5.0),
        ]
    )
    stage = ImplPlannerStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    assert len(runner.calls) == 2
    # Second call must include the schema error feedback
    _, second_msg = runner.calls[1]
    assert "schema_error.md" in second_msg
    assert "snake_case" in second_msg
    assert result["subtask_count"] == 2


def test_impl_planner_raises_after_exhausting_retries(tmp_path):
    repo, task_dir = _make_repo_and_task(tmp_path)
    bad = "# Plan\n\nno yaml block\n"
    runner = ScriptedRunner(
        [
            RunnerResult(text=bad, cost_usd=0.30, duration_s=5.0),
            RunnerResult(text=bad, cost_usd=0.30, duration_s=5.0),
        ]
    )
    stage = ImplPlannerStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    with pytest.raises(ImplPlannerError, match="failed validation after"):
        stage.run(_stage_def(), ctx)
    assert len(runner.calls) == 2
