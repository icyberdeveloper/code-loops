"""Tests for DebateCritiqueStage with mocked runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.debate_critique import (
    DebateCritiqueStage,
    _new_concerns_budget,
    _parse_facilitator_verdict,
)
from code_loops.stages.prompt import StageContext
from tests.conftest import FakeFactory

# ---- _new_concerns_budget ----


def test_critique_budget_narrows_per_round():
    # max_rounds=3 default for critique
    assert _new_concerns_budget(1, 3) == 3
    assert _new_concerns_budget(2, 3) == 2
    assert _new_concerns_budget(3, 3) == 1


# ---- _parse_facilitator_verdict ----


def test_critique_verdict_approved():
    text = '...analysis...\n```json\n{"verdict": "approved", "reason": "all good"}\n```\n'
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved"
    assert v["reason"] == "all good"


def test_critique_verdict_needs_revision():
    text = '...analysis...\n```json\n{"verdict": "needs_revision", "reason": "blocker X"}\n```\n'
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "needs_revision"
    assert v["reason"] == "blocker X"


def test_critique_verdict_unknown_value_falls_back_to_needs_revision():
    text = '```json\n{"verdict": "maybe", "reason": "unclear"}\n```\n'
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "needs_revision"


def test_critique_verdict_no_json_defaults_to_needs_revision():
    v = _parse_facilitator_verdict("just prose, no json")
    assert v["status"] == "needs_revision"
    assert "could not parse" in v["reason"]


def test_critique_verdict_redesign_needed_with_extras():
    text = (
        "...analysis...\n```json\n"
        '{"verdict": "redesign_needed", "reason": "theme X recurred", '
        '"recurring_theme": "state_sync_mismatch", '
        '"design_guidance": "use a different shape"}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "redesign_needed"
    assert v["recurring_theme"] == "state_sync_mismatch"
    assert v["design_guidance"] == "use a different shape"


# ---- ScriptedRunner ----


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
    (tmp_path / "agents" / "architects").mkdir(parents=True)
    (tmp_path / "agents" / "architects" / "architect-critic-safety.md").write_text("SAFETY")
    (tmp_path / "agents" / "architects" / "architect-critic-elegance.md").write_text("ELEGANCE")
    (tmp_path / "agents" / "architects" / "architect-critic-hallucination.md").write_text(
        "HALLUCINATION"
    )
    (tmp_path / "agents" / "architects" / "architect-review-arbiter.md").write_text("FACILITATOR")
    (tmp_path / "agents" / "architects" / "software-architect.md").write_text(
        "RESPONDER (rfc_writer reused)"
    )

    task_dir = tmp_path / "tasks" / "0001_x"
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text("Some task description\nLine 2\nLine 3\n")
    (task_dir / "design").mkdir()
    (task_dir / "design" / "final.md").write_text("# RFC v1\n\n## Context\noriginal RFC")
    return tmp_path, task_dir


def _verdict_block(status: str, reason: str = "ok") -> str:
    return f'analysis\n\n```json\n{{"verdict": "{status}", "reason": "{reason}"}}\n```\n'


def _stage_def(max_rounds: int = 3) -> dict:
    return {
        "name": "design_review",
        "type": "debate_critique",
        "max_rounds": max_rounds,
        "roles": {
            "critics": [
                {"name": "safety", "prompt": "agents/architects/architect-critic-safety.md"},
                {"name": "elegance", "prompt": "agents/architects/architect-critic-elegance.md"},
                {
                    "name": "hallucination",
                    "prompt": "agents/architects/architect-critic-hallucination.md",
                },
            ],
            "responder": {"prompt": "agents/architects/software-architect.md"},
            "facilitator": {"prompt": "agents/architects/architect-review-arbiter.md"},
        },
    }


def test_critique_approves_first_round_keeps_rfc(tmp_path):
    """Critics approve immediately. RFC unchanged. verdict.md = approved."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    responses = [
        RunnerResult(text="# Critic: safety\n... safety: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text="# Critic: elegance\n... elegance: APPROVE", cost_usd=0.10, duration_s=3.0
        ),
        RunnerResult(
            text="# Critic: hallucination\n... hallucination: APPROVE",
            cost_usd=0.10,
            duration_s=3.0,
        ),
        RunnerResult(text=_verdict_block("approved", "all good"), cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    assert (task_dir / "design_review" / "verdict.md").read_text().startswith("# Verdict: approved")
    assert (task_dir / "design_review" / "safety_v1.md").exists()
    assert (task_dir / "design_review" / "elegance_v1.md").exists()
    # No `{name}.md` mirror is written — versioned snapshots are the source of truth.
    assert not (task_dir / "design_review" / "safety.md").exists()
    assert "## Round 1 — critic: safety" in (task_dir / "design_review" / "debate.md").read_text()
    # RFC unchanged
    assert (task_dir / "design" / "final.md").read_text() == "# RFC v1\n\n## Context\noriginal RFC"
    assert result["verdict"] == "approved"
    assert result["rfc_revisions"] == 0
    assert result["cost_usd"] == pytest.approx(0.35)
    assert len(runner.calls) == 4  # 3 critics + facilitator, no responder needed


def test_critique_one_revision_then_approves(tmp_path):
    """R1 needs_revision -> responder revises -> R2 approved. final.md = revised RFC."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    responses = [
        # R1 critics
        RunnerResult(text="safety: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: APPROVE", cost_usd=0.10, duration_s=3.0),
        # R1 facilitator
        RunnerResult(
            text=_verdict_block("needs_revision", "safety blocker"), cost_usd=0.05, duration_s=2.0
        ),
        # Responder revises -> rfc_v2
        RunnerResult(text="# RFC v2 (revised)\n\n## Context\nfixed", cost_usd=0.30, duration_s=8.0),
        # R2 critics
        RunnerResult(text="safety: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: APPROVE", cost_usd=0.10, duration_s=3.0),
        # R2 facilitator
        RunnerResult(
            text=_verdict_block("approved", "blocker addressed"), cost_usd=0.05, duration_s=2.0
        ),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    # design/final.md was overwritten with revised RFC
    assert (
        task_dir / "design" / "final.md"
    ).read_text() == "# RFC v2 (revised)\n\n## Context\nfixed"
    assert (task_dir / "design_review" / "rfc_revision_v2.md").exists()
    assert result["verdict"] == "approved"
    assert result["rfc_revisions"] == 1
    assert len(runner.calls) == 9


def test_critique_max_rounds_emits_needs_revision_max(tmp_path):
    """Never approved by max_rounds=2. verdict = needs_revision_max_rounds."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    stage_def = _stage_def(max_rounds=2)
    responses = [
        # R1 critics + facilitator
        RunnerResult(text="safety: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_verdict_block("needs_revision", "x"), cost_usd=0.05, duration_s=2.0),
        # Responder revision
        RunnerResult(text="# RFC v2", cost_usd=0.30, duration_s=8.0),
        # R2 critics + facilitator (still no approval)
        RunnerResult(text="safety: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_verdict_block("needs_revision", "still bad"), cost_usd=0.05, duration_s=2.0
        ),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(stage_def, ctx)

    verdict_text = (task_dir / "design_review" / "verdict.md").read_text()
    assert verdict_text.startswith("# Verdict: needs_revision_max_rounds")
    assert result["verdict"] == "needs_revision_max_rounds"
    assert result["rfc_revisions"] == 1
    # Max rounds reached without approval — no second revision attempt
    assert len(runner.calls) == 9
    # Bug G fix: max_rounds-no-approval now synthesizes a redesign signal so
    # engine bubbles back to design (instead of silently auto-approving).
    assert result["recurring_theme"] == "no_approval_after_max_rounds"
    assert "did not converge on approval after 2 rounds" in result["design_guidance"]
    signal = (task_dir / "design" / "redesign_signal.md").read_text()
    assert "no_approval_after_max_rounds" in signal
    assert "iterative-revision path was exhausted" in signal
    # Previous RFC snapshot for the next architect pass to reference
    assert (task_dir / "design" / "previous_rfc.md").exists()


def test_redesign_needed_short_circuits_and_writes_signal(tmp_path):
    """R1 redesign_needed -> stop loop, write redesign_signal.md, no responder revision."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    redesign_verdict = (
        "...analysis...\n```json\n"
        '{"verdict": "redesign_needed", '
        '"reason": "theme state_sync_mismatch recurred in R1", '
        '"recurring_theme": "state_sync_mismatch", '
        '"design_guidance": "Use an explicit linkage table instead of '
        'time-window matching."}\n```\n'
    )
    responses = [
        RunnerResult(text="safety: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: NEEDS_REVISION", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=redesign_verdict, cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    # Loop stopped after R1 — no responder, no R2 critics
    assert len(runner.calls) == 4
    assert result["verdict"] == "redesign_needed"
    assert result["recurring_theme"] == "state_sync_mismatch"
    assert "Use an explicit linkage table" in result["design_guidance"]

    # redesign_signal.md was written
    signal_path = task_dir / "design" / "redesign_signal.md"
    assert signal_path.exists()
    body = signal_path.read_text()
    assert "state_sync_mismatch" in body
    assert "Use an explicit linkage table" in body
    assert "structurally impossible" in body  # the design-guidance framing

    # previous_rfc.md was snapshotted
    assert (task_dir / "design" / "previous_rfc.md").exists()

    # verdict.md surfaces the redesign details
    verdict_text = (task_dir / "design_review" / "verdict.md").read_text()
    assert verdict_text.startswith("# Verdict: redesign_needed")
    assert "state_sync_mismatch" in verdict_text
    assert "Design guidance" in verdict_text


def test_critics_get_only_rfc_not_prior_rfc_debate(tmp_path):
    """Hard isolation: critics must not see the prior rfc-writer debate."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    # Create design/debate.md to ensure it's NOT leaked
    (task_dir / "design" / "debate.md").write_text("# RFC Debate\n\nlots of perspective rounds")

    responses = [
        RunnerResult(text="safety: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_verdict_block("approved"), cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage.run(_stage_def(), ctx)

    # Critics calls are 0, 1, 2 (safety, elegance, hallucination)
    for _sys_prompt, user_msg in runner.calls[:3]:
        assert "current rfc" in user_msg
        assert "original RFC" in user_msg  # design/final.md content
        # Hard isolation invariants:
        assert "lots of perspective rounds" not in user_msg
        assert "RFC Debate" not in user_msg
