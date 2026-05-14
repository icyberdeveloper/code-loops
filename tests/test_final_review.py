"""Tests for FinalReviewStage parser + corrective_subtasks validation."""

from __future__ import annotations

from code_loops.runner import RunnerFactory
from code_loops.stages.final_review import FinalReviewStage, _parse_verdict
from code_loops.stages.prompt import StageContext


def _stage():
    return FinalReviewStage(RunnerFactory())


# ---- _parse_verdict ----


def test_verdict_approved():
    text = '...analysis...\n```json\n{"verdict": "approved", "reason": "all done"}\n```\n'
    v = _parse_verdict(text)
    assert v["verdict"] == "approved"
    assert v["reason"] == "all done"
    assert v["corrective_subtasks"] == []


def test_verdict_needs_more_work_with_valid_corrective():
    text = """\
prose

```json
{
  "verdict": "needs_more_work",
  "reason": "missing dispatcher registration",
  "corrective_subtasks": [
    {
      "id": "register_action",
      "title": "Register export_week in ActionDispatcher",
      "files": {"modify": ["src/feature/dispatcher.py"]},
      "spec_md": "Add @register('export_week') decorator on the new handler."
    }
  ]
}
```
"""
    v = _parse_verdict(text)
    assert v["verdict"] == "needs_more_work"
    assert len(v["corrective_subtasks"]) == 1
    assert v["corrective_subtasks"][0]["id"] == "register_action"


def test_verdict_needs_more_work_invalid_corrective_drops_it():
    """Schema-invalid corrective subtask: id has CamelCase. Should be dropped, reason annotated."""
    text = """\
```json
{
  "verdict": "needs_more_work",
  "reason": "missing thing",
  "corrective_subtasks": [
    {
      "id": "BadCamelId",
      "title": "x",
      "files": {"modify": ["a.py"]},
      "spec_md": "y"
    }
  ]
}
```
"""
    v = _parse_verdict(text)
    assert v["verdict"] == "needs_more_work"
    assert v["corrective_subtasks"] == []
    assert "schema" in v["reason"].lower()


def test_verdict_no_json_falls_back_to_needs_more_work():
    v = _parse_verdict("just prose, no json block")
    assert v["verdict"] == "needs_more_work"
    assert "could not parse" in v["reason"]


def test_verdict_unknown_value_falls_back_to_needs_more_work():
    text = '```json\n{"verdict": "maybe", "reason": "x"}\n```\n'
    v = _parse_verdict(text)
    assert v["verdict"] == "needs_more_work"


def test_verdict_invalid_json_falls_back():
    text = '```json\n{"verdict": "approved", broken json\n```\n'
    v = _parse_verdict(text)
    assert v["verdict"] == "needs_more_work"
    assert "JSON parse error" in v["reason"]


def test_stage_instantiates():
    """Smoke: the stage class can be constructed with a factory."""
    s = _stage()
    assert s.factory is not None


def test_final_review_per_attempt_scoping_and_manifest_record(tmp_path):
    """Two consecutive runs (simulating needs_more_work loop) must write
    attempt_1/ and attempt_2/ separately, copy latest to flat path,
    record both in manifest with verdict outcomes."""
    from code_loops.artifact_writer import ArtifactWriter
    from code_loops.manifest import Manifest
    from code_loops.runner import RunnerResult
    from code_loops.stages.final_review import FinalReviewStage

    class _R:
        def __init__(self, responses):
            self.responses = list(responses)

        def make(self, _spec):
            return self

        def run(self, _sys, _user):
            return self.responses.pop(0)

    # Set up task_dir + minimal inputs
    repo = tmp_path / "repo"
    (repo / "agents/release").mkdir(parents=True)
    (repo / "agents/release/release-manager.md").write_text("RELEASE MANAGER")
    task_dir = tmp_path / "task"
    for sub in ["design", "prd", "implementation", "validation"]:
        (task_dir / sub).mkdir(parents=True)
    (task_dir / "design" / "final.md").write_text("# RFC")
    (task_dir / "prd" / "prd.md").write_text("# PRD")
    (task_dir / "implementation" / "_full_diff.patch").write_text("diff")
    (task_dir / "validation" / "result.yaml").write_text("passed: true")
    (task_dir / "validation" / "coverage.md").write_text("ok")

    needs_more = (
        '```json\n{"verdict": "needs_more_work", "reason": "missing X", '
        '"corrective_subtasks": [{"id": "fix_x", "title": "fix x", '
        '"files": {"create": ["a.py"]}, "spec_md": "do x"}]}\n```\n'
    )
    approved = '```json\n{"verdict": "approved", "reason": "ok"}\n```\n'

    factory = _R(
        [
            RunnerResult(text=needs_more, cost_usd=0.5, duration_s=10.0),
            RunnerResult(text=approved, cost_usd=0.4, duration_s=8.0),
        ]
    )
    stage = FinalReviewStage(factory)
    manifest = Manifest(task_dir / "manifest.json")
    manifest.init_task("0001_t", mode="feature")
    aw = ArtifactWriter(task_dir, manifest)
    ctx = StageContext(
        task_dir=task_dir,
        prompts_dir=repo / "agents",
        repo_root=repo,
        artifact_writer=aw,
    )
    stage_def = {
        "name": "release_review",
        "type": "final_review",
        "prompt": "agents/release/release-manager.md",
    }

    # Attempt 1: needs_more_work
    r1 = stage.run(stage_def, ctx)
    assert r1["verdict"] == "needs_more_work"
    assert (task_dir / "release_review" / "attempts" / "attempt_1" / "verdict.md").exists()
    assert (
        task_dir / "release_review" / "attempts" / "attempt_1" / "corrective_subtasks.yaml"
    ).exists()

    # Attempt 2: approved (engine would reset stage in between, but our
    # manifest entry still shows attempt 1 — handler bumps to attempt 2
    # based on existing attempts list).
    r2 = stage.run(stage_def, ctx)
    assert r2["verdict"] == "approved"
    assert (task_dir / "release_review" / "attempts" / "attempt_2" / "verdict.md").exists()
    # Latest copy = attempt 2
    assert "approved" in (task_dir / "release_review" / "verdict.md").read_text()

    # Manifest records both
    attempts = manifest.data["stages"]["release_review"]["attempts"]
    assert len(attempts) == 2
    assert attempts[0]["outcome"] == "needs_more_work"
    assert attempts[0]["corrective_subtasks_count"] == 1
    assert attempts[1]["outcome"] == "approved"
