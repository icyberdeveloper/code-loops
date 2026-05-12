"""Tests for SubtaskIteratorStage parsers and design-escalation logic.

The full per-subtask loop with worktree + LLM is too heavy for unit tests;
that's exercised by smoke tests. Here we cover:
- Verdict / router JSON parsing
- DesignEscalation contract
- Helpers (_render_spec, _build_failure_input)
"""

from __future__ import annotations

from code_loops.runner import RunnerFactory
from code_loops.stages.subtask_iterator import (
    VALID_FIX_TARGETS,
    DesignEscalation,
    SubtaskIteratorStage,
)


def _stage():
    return SubtaskIteratorStage(RunnerFactory())


# ---- _parse_reviewer_verdict ----


def test_reviewer_verdict_approved():
    text = '...prose...\n```json\n{"verdict": "approved", "concerns": []}\n```\n'
    v = _stage()._parse_reviewer_verdict(text)
    assert v["verdict"] == "approved"
    assert v["concerns"] == []


def test_reviewer_verdict_needs_fix_with_concerns():
    text = (
        '```json\n{"verdict": "needs_fix", "concerns": ['
        '{"severity": "blocker", "where": "app/x.py:5", "what": "broke"}'
        "]}\n```\n"
    )
    v = _stage()._parse_reviewer_verdict(text)
    assert v["verdict"] == "needs_fix"
    assert len(v["concerns"]) == 1
    assert v["concerns"][0]["severity"] == "blocker"


def test_reviewer_verdict_unknown_value_falls_back_to_needs_fix():
    text = '```json\n{"verdict": "maybe", "concerns": []}\n```\n'
    v = _stage()._parse_reviewer_verdict(text)
    assert v["verdict"] == "needs_fix"


def test_reviewer_verdict_no_json_falls_back():
    v = _stage()._parse_reviewer_verdict("just prose")
    assert v["verdict"] == "needs_fix"
    assert any("could not parse" in str(c) for c in v["concerns"])


# ---- _parse_router_verdict ----


def test_router_verdict_coder():
    text = (
        '```json\n{"target": "coder", "reason": "import error", '
        '"feedback_for_target": "fix the import in app/foo.py"}\n```\n'
    )
    v = _stage()._parse_router_verdict(text)
    assert v["target"] == "coder"
    assert "import" in v["reason"]
    assert "app/foo.py" in v["feedback_for_target"]


def test_router_verdict_test_writer():
    text = '```json\n{"target": "test_writer", "reason": "broken fixture", "feedback_for_target": "fix"}\n```\n'
    v = _stage()._parse_router_verdict(text)
    assert v["target"] == "test_writer"


def test_router_verdict_escalate_design():
    text = '```json\n{"target": "escalate_design", "reason": "spec impossible", "feedback_for_target": "rfc must change"}\n```\n'
    v = _stage()._parse_router_verdict(text)
    assert v["target"] == "escalate_design"


def test_router_verdict_unknown_target_falls_back_to_coder():
    text = '```json\n{"target": "nonexistent", "reason": "x", "feedback_for_target": "y"}\n```\n'
    v = _stage()._parse_router_verdict(text)
    assert v["target"] == "coder"


def test_router_verdict_no_json_falls_back_to_coder():
    v = _stage()._parse_router_verdict("blah blah")
    assert v["target"] == "coder"
    assert "could not parse" in v["reason"]


def test_valid_fix_targets_set():
    assert {"coder", "test_writer", "escalate_design"} == VALID_FIX_TARGETS


# ---- DesignEscalation ----


def test_design_escalation_carries_context():
    e = DesignEscalation("subtask_x", "spec impossible", "rfc must change")
    assert e.subtask_id == "subtask_x"
    assert e.reason == "spec impossible"
    assert e.feedback == "rfc must change"
    assert "subtask_x" in str(e)


# ---- helpers ----


def test_render_spec_includes_id_and_files():
    subtask = {
        "id": "extract_helper",
        "title": "Extract helper",
        "files": {"create": ["app/foo.py"]},
        "spec_md": "do the thing",
    }
    spec = _stage()._render_spec(subtask)
    assert "extract_helper" in spec
    assert "Extract helper" in spec
    assert "app/foo.py" in spec
    assert "do the thing" in spec


def test_build_failure_input_includes_pytest_and_reviewer():
    failure = _stage()._build_failure_input(
        validator_result={
            "passed": False,
            "pytest_rc": 1,
            "pytest_log": "FAILED test_x: assertion error",
            "ruff_rc": 0,
            "ruff_log": "",
        },
        review_verdict={"verdict": "needs_fix", "concerns": [{"what": "broke"}]},
        disagreement=None,
    )
    assert "PYTEST" in failure
    assert "FAILED test_x" in failure
    assert "REVIEWER concerns" in failure
    assert "broke" in failure


def test_build_failure_input_includes_disagreement_when_present():
    failure = _stage()._build_failure_input(
        validator_result={
            "passed": True,
            "pytest_rc": 0,
            "ruff_rc": 0,
            "pytest_log": "",
            "ruff_log": "",
        },
        review_verdict={"verdict": "approved", "concerns": []},
        disagreement="TEST_DISAGREEMENT: test asserts wrong value",
    )
    assert "CODER disagreement" in failure
    assert "test asserts wrong value" in failure
