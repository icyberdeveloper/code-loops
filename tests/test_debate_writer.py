"""Tests for DebateWriterStage with mocked runner.

Covers parsing helpers and the loop control flow at the unit level — the
real LLM integration is exercised via smoke runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_loops.isolation import parse_perspectives
from code_loops.runner import RunnerResult
from code_loops.stages.debate_writer import (
    DebateWriterStage,
    _new_concerns_budget,
    _parse_facilitator_verdict,
)
from code_loops.stages.prompt import StageContext
from tests.conftest import FakeFactory

# ---- parse_perspectives ----


def test_parse_perspectives_extracts_block_and_injects_mandatory():
    """structural_skeptic is auto-injected at position 0 if planner omitted it
    (defense-in-depth — see isolation.MANDATORY_PERSPECTIVE rationale)."""
    plan = """\
# Plan

## Perspectives for RFC

```
perspectives_for_rfc:
  - simplicity
  - data_integrity
  - operational
```
"""
    assert parse_perspectives(plan) == [
        "structural_skeptic",
        "simplicity",
        "data_integrity",
        "operational",
    ]


def test_parse_perspectives_preserves_order_when_mandatory_already_present():
    plan = """\
perspectives_for_rfc:
  - structural_skeptic
  - simplicity
  - operational
"""
    assert parse_perspectives(plan) == ["structural_skeptic", "simplicity", "operational"]


def test_parse_perspectives_returns_default_when_block_missing():
    plan = "# Plan\n\nno block here\n"
    assert parse_perspectives(plan) == ["structural_skeptic", "simplicity", "correctness"]


def test_parse_perspectives_strips_whitespace_and_injects_mandatory():
    plan = "perspectives_for_rfc:\n   -   foo\n   -   bar\n"
    assert parse_perspectives(plan) == ["structural_skeptic", "foo", "bar"]


# ---- _new_concerns_budget ----


def test_new_concerns_budget_narrows_each_round():
    # max_rounds=5: R1=5, R2=4, R3=3, R4=2, R5=1
    assert _new_concerns_budget(1, 5) == 5
    assert _new_concerns_budget(2, 5) == 4
    assert _new_concerns_budget(3, 5) == 3
    assert _new_concerns_budget(4, 5) == 2
    assert _new_concerns_budget(5, 5) == 1


def test_new_concerns_budget_floor_is_one():
    # Beyond max_rounds, budget stays at 1 (we only call this with valid round_n)
    assert _new_concerns_budget(10, 5) == 1


def test_new_concerns_budget_max_rounds_3():
    assert _new_concerns_budget(1, 3) == 3
    assert _new_concerns_budget(2, 3) == 2
    assert _new_concerns_budget(3, 3) == 1


# ---- _parse_facilitator_verdict ----


def test_facilitator_verdict_converged_true():
    text = """\
Some analysis here.

```json
{"converged": true, "reason": "All concerns addressed."}
```
"""
    v = _parse_facilitator_verdict(text)
    assert v["converged"] is True
    assert v["reason"] == "All concerns addressed."


def test_facilitator_verdict_converged_false():
    text = """\
Analysis paragraph.

```json
{"converged": false, "reason": "Cost concern unresolved."}
```
"""
    v = _parse_facilitator_verdict(text)
    assert v["converged"] is False


def test_facilitator_verdict_no_json_defaults_to_not_converged():
    text = "just prose, no json block"
    v = _parse_facilitator_verdict(text)
    assert v["converged"] is False
    assert "could not parse" in v["reason"]


def test_facilitator_verdict_inline_json_text_fallback():
    text = 'no proper block but says "converged": true somewhere'
    v = _parse_facilitator_verdict(text)
    assert v["converged"] is True


# ---- ScriptedRunner ----


class ScriptedRunner:
    """Returns a queued response per call. Caller must script enough responses."""

    def __init__(self, responses: list[RunnerResult]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def run(self, system_prompt: str, user_message: str, **kwargs) -> RunnerResult:
        self.calls.append((system_prompt, user_message))
        if not self.responses:
            raise RuntimeError("ScriptedRunner ran out of responses")
        return self.responses.pop(0)


def _make_repo_and_task(tmp_path: Path, perspectives: list[str]) -> tuple[Path, Path]:
    (tmp_path / "agents" / "architects").mkdir(parents=True)
    (tmp_path / "agents" / "architects" / "software-architect.md").write_text("WRITER")
    (tmp_path / "agents" / "architects" / "architect-perspective.md").write_text(
        "PERSPECTIVE for {perspective_name}"
    )
    (tmp_path / "agents" / "architects" / "architect-debate-arbiter.md").write_text("FACILITATOR")

    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text("Some task description\nLine 2\nLine 3\n")
    (task_dir / "research_plan").mkdir()
    persp_block = "perspectives_for_rfc:\n" + "\n".join(f"  - {p}" for p in perspectives) + "\n"
    (task_dir / "research_plan" / "plan.md").write_text(f"# Plan\n\n{persp_block}\n")
    (task_dir / "research").mkdir()
    (task_dir / "research" / "codebase.md").write_text("# Research codebase\nstuff")
    return tmp_path, task_dir


def _verdict_block(converged: bool, reason: str = "ok") -> str:
    return f'analysis\n\n```json\n{{"converged": {str(converged).lower()}, "reason": "{reason}"}}\n```\n'


def _stage_def(max_rounds: int = 10) -> dict:
    return {
        "name": "design",
        "type": "debate_writer",
        "max_rounds": max_rounds,
        "roles": {
            "writer": {"prompt": "agents/architects/software-architect.md"},
            "perspective": {"prompt": "agents/architects/architect-perspective.md"},
            "facilitator": {"prompt": "agents/architects/architect-debate-arbiter.md"},
        },
    }


def test_debate_converges_first_round(tmp_path):
    """Writer drafts → 3 perspectives (structural_skeptic auto-injected + 2
    declared) critique → facilitator says converged. Final.md = draft_v1.
    """
    repo, task_dir = _make_repo_and_task(tmp_path, ["simplicity", "operational"])
    responses = [
        RunnerResult(text="# RFC: X\n\n## Context\nctx", cost_usd=0.10, duration_s=5.0),
        RunnerResult(text="# Perspective: structural_skeptic\nok", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="# Perspective: simplicity\nok", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="# Perspective: operational\nok", cost_usd=0.05, duration_s=3.5),
        RunnerResult(text=_verdict_block(True, "All addressed"), cost_usd=0.03, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    assert (task_dir / "design" / "draft_v1.md").exists()
    assert not (task_dir / "design" / "draft_v2.md").exists()
    assert (task_dir / "design" / "final.md").read_text().startswith("# RFC: X")
    assert (task_dir / "design" / "debate.md").read_text().count(
        "Round 1"
    ) >= 2  # perspectives + facilitator
    assert result["converged"] is True
    # Bug E fix: converge at round N → rounds == N (not N-1). One round of
    # critique happened, even though only draft_v1 exists (no revision needed).
    assert result["rounds"] == 1
    assert result["cost_usd"] == pytest.approx(0.28)
    assert len(runner.calls) == 5


def test_debate_two_rounds_then_converges(tmp_path):
    """Round 1: not converged → writer revises → Round 2: converged.
    Final.md = draft_v2. 3 perspectives per round (structural_skeptic auto-injected).
    """
    repo, task_dir = _make_repo_and_task(tmp_path, ["simplicity", "data_integrity"])
    responses = [
        # Initial draft
        RunnerResult(text="DRAFT V1", cost_usd=0.10, duration_s=5.0),
        # Round 1 perspectives (3: structural_skeptic + simplicity + data_integrity)
        RunnerResult(text="P0 round1", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round1", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P2 round1", cost_usd=0.05, duration_s=3.0),
        # Round 1 facilitator: not converged
        RunnerResult(text=_verdict_block(False, "still issues"), cost_usd=0.03, duration_s=2.0),
        # Writer revision → draft_v2
        RunnerResult(text="DRAFT V2", cost_usd=0.10, duration_s=5.0),
        # Round 2 perspectives (3)
        RunnerResult(text="P0 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P2 round2", cost_usd=0.05, duration_s=3.0),
        # Round 2 facilitator: converged
        RunnerResult(text=_verdict_block(True, "all addressed"), cost_usd=0.03, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    assert (task_dir / "design" / "draft_v1.md").read_text() == "DRAFT V1"
    assert (task_dir / "design" / "draft_v2.md").read_text() == "DRAFT V2"
    assert (task_dir / "design" / "final.md").read_text() == "DRAFT V2"
    assert result["converged"] is True
    # Bug E fix: 2 rounds executed, converge at R2 → rounds == 2 (not 1).
    assert result["rounds"] == 2
    assert len(runner.calls) == 10


def test_debate_max_rounds_falls_back_to_last_draft(tmp_path):
    """Facilitator never converges; max_rounds=2 hit; final = latest draft."""
    # 2 perspectives per round (structural_skeptic auto-injected + 1 declared).
    repo, task_dir = _make_repo_and_task(tmp_path, ["simplicity"])
    stage_def = _stage_def(max_rounds=2)
    responses = [
        # Initial
        RunnerResult(text="DRAFT V1", cost_usd=0.10, duration_s=5.0),
        # Round 1 (2 perspectives)
        RunnerResult(text="P0 round1", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round1", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text=_verdict_block(False), cost_usd=0.03, duration_s=2.0),
        RunnerResult(text="DRAFT V2", cost_usd=0.10, duration_s=5.0),
        # Round 2 (2 perspectives)
        RunnerResult(text="P0 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text=_verdict_block(False), cost_usd=0.03, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(stage_def, ctx)

    assert (task_dir / "design" / "final.md").read_text() == "DRAFT V2"
    assert "Max rounds (2) reached" in (task_dir / "design" / "debate.md").read_text()
    assert result["converged"] is False
    assert result["rounds"] == 2


def test_perspectives_get_only_draft_not_research(tmp_path):
    """Hard isolation: perspective agents must not see research/* in their messages.
    2 perspectives total (structural_skeptic auto-injected + 1 declared).
    """
    repo, task_dir = _make_repo_and_task(tmp_path, ["simplicity"])
    responses = [
        RunnerResult(text="DRAFT", cost_usd=0.10, duration_s=5.0),
        RunnerResult(text="P0_skeptic", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1_simplicity", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text=_verdict_block(True), cost_usd=0.03, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)

    # Perspective calls are index 1 (structural_skeptic) + 2 (simplicity); index 3 = facilitator
    sys_prompt_skeptic, _ = runner.calls[1]
    sys_prompt_simpl, user_msg = runner.calls[2]
    assert "PERSPECTIVE for structural_skeptic" in sys_prompt_skeptic
    assert "PERSPECTIVE for simplicity" in sys_prompt_simpl
    assert "current draft" in user_msg
    assert "DRAFT" in user_msg
    # Hard isolation invariants:
    assert "research/codebase.md" not in user_msg
    assert "Research codebase" not in user_msg
    assert "perspectives_for_rfc" not in user_msg  # plan not passed in


def test_writer_revision_sees_only_previous_draft_and_this_round(tmp_path):
    """Writer revision must NOT see debate.md history, only this round's perspectives.
    2 perspectives per round (structural_skeptic auto-injected + 1 declared).
    """
    repo, task_dir = _make_repo_and_task(tmp_path, ["simplicity"])
    responses = [
        RunnerResult(text="DRAFT V1", cost_usd=0.10, duration_s=5.0),
        # Round 1 (2 perspectives)
        RunnerResult(text="P0 round1 — skeptic", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round1 — concern X", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text=_verdict_block(False), cost_usd=0.03, duration_s=2.0),
        # Revision
        RunnerResult(text="DRAFT V2", cost_usd=0.10, duration_s=5.0),
        # Round 2 — converge (2 perspectives)
        RunnerResult(text="P0 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text="P1 round2", cost_usd=0.05, duration_s=3.0),
        RunnerResult(text=_verdict_block(True), cost_usd=0.03, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateWriterStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    stage.run(_stage_def(), ctx)

    # Writer revision call: 0=init, 1=p0, 2=p1, 3=fac1, 4=writer-revise
    sys_prompt, user_msg = runner.calls[4]
    assert "WRITER" in sys_prompt
    assert "previous_draft.md (v1)" in user_msg
    assert "DRAFT V1" in user_msg
    assert "P1 round1 — concern X" in user_msg
    assert "P0 round1 — skeptic" in user_msg
    # Should NOT contain the full debate.md (which includes "Round 0 — Writer initial draft")
    assert "Round 0 — Writer initial draft" not in user_msg
    assert "facilitator" not in user_msg
