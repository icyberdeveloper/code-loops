"""Tests for FinalReviewStage parser + corrective_subtasks validation."""

from __future__ import annotations

from code_loops.runner import RunnerFactory
from code_loops.stages.final_review import FinalReviewStage, _parse_verdict


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
