"""Tests for DebateCritiqueStage with mocked runner."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_loops.runner import RunnerResult
from code_loops.stages.debate_critique import (
    ConcernParseError,
    DebateCritiqueStage,
    _aggregate_round_concerns,
    _apply_gate_policy,
    _compute_plateau_metrics,
    _count_filtered_concerns,
    _load_gate_policy,
    _new_concerns_budget,
    _parse_critic_concerns,
    _parse_facilitator_verdict,
    _render_structured_concerns_block,
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


def test_critique_verdict_approved_with_followups_extracts_items():
    """Новый 4-tier verdict — facilitator marks RFC ship-safe но flags
    bounded concerns для tracking в impl plan."""
    text = (
        "...analysis...\n```json\n"
        '{"verdict": "approved_with_followups", '
        '"reason": "ships; safety FOLLOWUP tracked", '
        '"followups": ['
        '{"category": "safety", "summary": "add tz invariant test", '
        '"rfc_section": "Tests", "severity": "FOLLOWUP"},'
        '{"category": "elegance", "summary": "split helper later", '
        '"rfc_section": "Proposed approach", "severity": "FOLLOWUP"}'
        "]}\n```\n"
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved_with_followups"
    assert len(v["followups"]) == 2
    assert v["followups"][0]["category"] == "safety"
    assert v["followups"][0]["summary"] == "add tz invariant test"
    assert v["followups"][0]["severity"] == "FOLLOWUP"


def test_critique_verdict_approved_with_followups_empty_list_downgrades():
    """approved_with_followups с пустым followups[] не валиден — facilitator
    обязан перечислить items. Downgrade до needs_revision вместо silent
    ship — это безопаснее."""
    text = (
        "```json\n"
        '{"verdict": "approved_with_followups", "reason": "ok", "followups": []}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "needs_revision"
    assert "downgraded" in v["reason"]


def test_critique_verdict_approved_with_followups_caps_at_max():
    """Защита от degenerate case: LLM в quality treadmill переmark'ал ВСЕ
    concerns как FOLLOWUP. Cap к MAX_FOLLOWUPS_PER_VERDICT=10 + reason marker
    с количеством dropped items для forensic trail."""
    from code_loops.stages.debate_critique import MAX_FOLLOWUPS_PER_VERDICT

    # 15 followups — превышает cap=10
    items = ",".join(
        f'{{"category": "safety", "summary": "concern {i}", '
        f'"rfc_section": "Tests", "severity": "FOLLOWUP"}}'
        for i in range(15)
    )
    text = (
        '```json\n'
        '{"verdict": "approved_with_followups", "reason": "ship-ready",'
        f'"followups": [{items}]}}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved_with_followups"
    assert len(v["followups"]) == MAX_FOLLOWUPS_PER_VERDICT
    assert "truncated" in v["reason"]
    assert "5 of 15 followups dropped" in v["reason"]


def test_critique_verdict_approved_with_followups_at_cap_no_truncation():
    """Exactly MAX items — без truncation marker."""
    from code_loops.stages.debate_critique import MAX_FOLLOWUPS_PER_VERDICT

    items = ",".join(
        f'{{"category": "safety", "summary": "c{i}",'
        f'"rfc_section": "Tests", "severity": "FOLLOWUP"}}'
        for i in range(MAX_FOLLOWUPS_PER_VERDICT)
    )
    text = (
        '```json\n'
        '{"verdict": "approved_with_followups", "reason": "ok",'
        f'"followups": [{items}]}}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved_with_followups"
    assert len(v["followups"]) == MAX_FOLLOWUPS_PER_VERDICT
    assert "truncated" not in v["reason"]


def test_critique_verdict_approved_with_followups_normalizes_severity():
    """Severity нормализуется до UPPER, category до lower — engine relies
    on consistent shape для группировки по lens."""
    text = (
        "```json\n"
        '{"verdict": "approved_with_followups", "reason": "ship",'
        '"followups": [{"category": "SAFETY", "summary": "x",'
        '"rfc_section": "Risks", "severity": "followup"}]}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved_with_followups"
    assert v["followups"][0]["category"] == "safety"
    assert v["followups"][0]["severity"] == "FOLLOWUP"


# ---- ScriptedRunner ----


class ScriptedRunner:
    def __init__(self, responses: list[RunnerResult]):
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def run(self, system_prompt: str, user_message: str, **kwargs) -> RunnerResult:
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


def _critic_response(
    critic: str,
    round_n: int = 1,
    max_rounds: int = 3,
    concerns: list[dict] | None = None,
) -> str:
    """Render fake critic output в new YAML format.

    concerns: list of {severity, summary, confidence?, category?} dicts.
    Missing optional fields get defaults. Если concerns=None или [] — empty list.
    """
    items = concerns or []
    yaml_items = []
    for i, c in enumerate(items, 1):
        yaml_items.append(
            f"- id: {critic}-C{i}\n"
            f"  severity: {c['severity']}\n"
            f"  confidence: {c.get('confidence', 0.8)}\n"
            f"  category: {c.get('category', 'misc')}\n"
            f"  summary: \"{c['summary']}\"\n"
            f"  affected_section: \"{c.get('affected_section', '§n/a')}\"\n"
            f"  recommended_fix: \"{c.get('recommended_fix', 'tbd')}\"\n"
        )
    yaml_body = "".join(yaml_items) if yaml_items else "[]\n"
    return (
        f"# Critic: {critic} (round {round_n}/{max_rounds})\n\n"
        f"## Analysis\nstuff observed.\n\n"
        f"## Concerns\n```yaml\n{yaml_body}```\n"
    )


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
        RunnerResult(text=_critic_response("safety", concerns=[]), cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_critic_response("elegance", concerns=[]), cost_usd=0.10, duration_s=3.0
        ),
        RunnerResult(
            text=_critic_response("hallucination", concerns=[]), cost_usd=0.10, duration_s=3.0
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
        RunnerResult(
            text=_critic_response(
                "safety",
                concerns=[{"severity": "blocker", "summary": "data race", "confidence": 0.9}],
            ),
            cost_usd=0.10,
            duration_s=3.0,
        ),
        RunnerResult(text=_critic_response("elegance", concerns=[]), cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_critic_response("hallucination", concerns=[]), cost_usd=0.10, duration_s=3.0
        ),
        # R1 facilitator
        RunnerResult(
            text=_verdict_block("needs_revision", "safety blocker"), cost_usd=0.05, duration_s=2.0
        ),
        # Responder revises -> rfc_v2
        RunnerResult(text="# RFC v2 (revised)\n\n## Context\nfixed", cost_usd=0.30, duration_s=8.0),
        # R2 critics
        RunnerResult(text=_critic_response("safety", concerns=[]), cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_critic_response("elegance", concerns=[]), cost_usd=0.10, duration_s=3.0
        ),
        RunnerResult(
            text=_critic_response("hallucination", concerns=[]), cost_usd=0.10, duration_s=3.0
        ),
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
    _blocker_critic = lambda name: _critic_response(  # noqa: E731
        name,
        concerns=[{"severity": "blocker", "summary": f"{name} fail", "confidence": 0.9}],
    )
    responses = [
        # R1 critics + facilitator
        RunnerResult(text=_blocker_critic("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_blocker_critic("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_blocker_critic("hallucination"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_verdict_block("needs_revision", "x"), cost_usd=0.05, duration_s=2.0),
        # Responder revision
        RunnerResult(text="# RFC v2", cost_usd=0.30, duration_s=8.0),
        # R2 critics + facilitator (still no approval)
        RunnerResult(text=_blocker_critic("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_blocker_critic("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_blocker_critic("hallucination"), cost_usd=0.10, duration_s=3.0),
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


# ---- gate.yaml policy ----


def _make_policy(**severity_overrides) -> dict:
    """Test helper: minimal valid gate policy. Per-severity overrides via
    kwargs: _make_policy(blocker={'min_confidence': 0.2, 'ceiling': 1}).
    """
    base = {
        "severities": {
            "blocker": {"min_confidence": 0.3, "ceiling": 0},
            "major": {"min_confidence": 0.4, "ceiling": 1},
            "medium": {"min_confidence": 0.5, "ceiling": 5},
            "minor": {"min_confidence": 0.7, "ceiling": -1},
        }
    }
    for sev, rules in severity_overrides.items():
        if sev in base["severities"]:
            base["severities"][sev].update(rules)
    return base


def _make_concern(severity: str, confidence: float, summary: str = "x", cid: str = "c1") -> dict:
    """Test helper: minimal valid concern dict."""
    return {
        "id": cid,
        "severity": severity,
        "confidence": confidence,
        "category": "x",
        "summary": summary,
        "affected_section": "x",
        "recommended_fix": "x",
    }


def test_gate_policy_loads_from_yaml():
    """Production gate.yaml loads + имеет required severities block."""
    policy = _load_gate_policy()
    assert "severities" in policy
    for sev in ("blocker", "major", "medium", "minor"):
        assert sev in policy["severities"]
        assert "min_confidence" in policy["severities"][sev]
        assert "ceiling" in policy["severities"][sev]


def test_count_filtered_per_severity_confidence():
    """Каждая severity имеет свой min_confidence threshold. Concerns ниже
    threshold для своей severity bucketed в ignored_per_severity."""
    policy = _make_policy(
        blocker={"min_confidence": 0.3},
        minor={"min_confidence": 0.7},
    )
    concerns = [
        _make_concern("blocker", 0.4),    # >= 0.3 → counted
        _make_concern("blocker", 0.2),    # < 0.3 → ignored
        _make_concern("minor", 0.8),      # >= 0.7 → counted
        _make_concern("minor", 0.5),      # < 0.7 → ignored
    ]
    counts = _count_filtered_concerns(concerns, policy)
    assert counts["blocker"] == 1
    assert counts["minor"] == 1
    assert counts["ignored_per_severity"]["blocker"] == 1
    assert counts["ignored_per_severity"]["minor"] == 1


def test_gate_no_op_when_not_triggered():
    """Без plateau и last_pass — arbiter verdict passes through unchanged."""
    policy = _make_policy()
    aggregate = {"all_concerns": [_make_concern("blocker", 0.9)]}
    for status in ("approved", "needs_revision", "redesign_needed"):
        v = {"status": status, "reason": "x"}
        out, reason = _apply_gate_policy(
            v, aggregate, policy, is_plateau=False, is_last_pass=False
        )
        assert out == v, f"Gate trogал verdict {status} without trigger"
        assert reason is None


def test_gate_triggered_by_plateau_ships_when_numbers_within():
    """Plateau + clean numbers → final = approved (override arbiter)."""
    policy = _make_policy()
    aggregate = {"all_concerns": [_make_concern("minor", 0.8)]}
    v = {"status": "needs_revision", "reason": "arbiter не уверен"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=False
    )
    assert out["status"] == "approved"
    assert "gate-decided" in out["reason"]
    assert "plateau" in reason


def test_gate_triggered_by_last_pass_ships_when_numbers_within():
    """Last pass + clean numbers → ship (escape catastrophic exit)."""
    policy = _make_policy()
    aggregate = {"all_concerns": []}
    v = {"status": "redesign_needed", "reason": "arbiter sees theme"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=False, is_last_pass=True
    )
    assert out["status"] == "approved"
    assert "last_pass" in reason
    # Arbiter's reason preserved для record
    assert "arbiter sees theme" in out["reason"]


def test_gate_triggered_keeps_revising_when_numbers_exceeded():
    """Gate active + numbers fail → final = needs_revision (one more round)."""
    policy = _make_policy()
    aggregate = {"all_concerns": [_make_concern("blocker", 0.9)]}
    v = {"status": "approved", "reason": "arbiter says ship"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=False
    )
    assert out["status"] == "needs_revision"
    assert "blocker: 1 > ceiling 0" in reason


def test_gate_overrides_redesign_when_numbers_clean():
    """Gate triggered + arbiter redesign_needed + numbers within → ship.
    Никакого special-case для redesign — все verdicts treated equally."""
    policy = _make_policy()
    aggregate = {"all_concerns": []}
    v = {"status": "redesign_needed", "reason": "theme detected"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=False
    )
    assert out["status"] == "approved"


def test_gate_respects_unlimited_ceiling():
    """ceiling: -1 значит no limit для этой severity."""
    policy = _make_policy(minor={"ceiling": -1})
    aggregate = {"all_concerns": [_make_concern("minor", 0.9, cid=str(i)) for i in range(50)]}
    v = {"status": "approved", "reason": "ship"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=False
    )
    assert out["status"] == "approved"


def test_gate_ignores_low_confidence_blockers():
    """Blocker с confidence < min_confidence → не counted → no ceiling violation."""
    policy = _make_policy(blocker={"min_confidence": 0.5})
    aggregate = {"all_concerns": [_make_concern("blocker", 0.3)]}  # below threshold
    v = {"status": "approved", "reason": "x"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=False
    )
    # Blocker ignored as low-conf → numbers within → approved survives
    assert out["status"] == "approved"
    assert "blocker: 1" in reason  # ignored_per_severity reported в reason


def test_gate_both_triggers_active_same_result():
    """Когда оба plateau и last_pass active — same logic как single trigger."""
    policy = _make_policy()
    aggregate = {"all_concerns": []}
    v = {"status": "needs_revision", "reason": "x"}
    out, reason = _apply_gate_policy(
        v, aggregate, policy, is_plateau=True, is_last_pass=True
    )
    assert out["status"] == "approved"
    assert "plateau" in reason and "last_pass" in reason


# ---- _parse_critic_concerns + _aggregate_round_concerns ----


def test_parse_concerns_extracts_valid_yaml_block():
    """Happy path: critic emit ## Concerns с ```yaml fence + valid schema."""
    text = _critic_response(
        "safety",
        concerns=[
            {
                "severity": "blocker",
                "summary": "validator silently drops unicode entries",
                "confidence": 0.9,
                "category": "data_loss",
                "affected_section": "§Proposed approach",
                "recommended_fix": "Add normalize()",
            },
            {
                "severity": "minor",
                "summary": "duplicate logic in foo.py",
                "confidence": 0.5,
                "category": "duplication",
            },
        ],
    )
    parsed = _parse_critic_concerns(text)
    assert len(parsed) == 2
    assert parsed[0]["id"] == "safety-C1"
    assert parsed[0]["severity"] == "blocker"
    assert parsed[0]["confidence"] == 0.9
    assert parsed[1]["severity"] == "minor"


def test_parse_concerns_empty_list_allowed():
    """Zero concerns valid case (late rounds, all clear)."""
    text = _critic_response("safety", concerns=[])
    parsed = _parse_critic_concerns(text)
    assert parsed == []


def test_parse_concerns_missing_concerns_section_raises():
    """Critic без ## Concerns section = parse error (no fallback)."""
    text = "# Critic: safety\n\n## Analysis\nstuff\n"
    with pytest.raises(ConcernParseError, match="missing ## Concerns"):
        _parse_critic_concerns(text)


def test_parse_concerns_missing_yaml_fence_raises():
    """## Concerns section без ```yaml block = parse error."""
    text = "# Critic: safety\n\n## Concerns\n1. legacy free-text concern\n"
    with pytest.raises(ConcernParseError, match="missing ```yaml fence"):
        _parse_critic_concerns(text)


def test_parse_concerns_invalid_severity_raises():
    """Severity outside enum = parse error."""
    text = _critic_response(
        "safety",
        concerns=[{"severity": "catastrophic", "summary": "x", "confidence": 0.5}],
    )
    with pytest.raises(ConcernParseError, match="severity="):
        _parse_critic_concerns(text)


def test_parse_concerns_confidence_out_of_range_raises():
    """Confidence outside [0.0, 1.0] = parse error."""
    text = _critic_response(
        "safety",
        concerns=[{"severity": "blocker", "summary": "x", "confidence": 1.5}],
    )
    with pytest.raises(ConcernParseError, match="confidence="):
        _parse_critic_concerns(text)


def test_aggregate_round_concerns_counts_severity_buckets():
    """Aggregator across critics — per-severity counts + fingerprints union."""
    results = [
        RunnerResult(
            text=_critic_response(
                "safety",
                concerns=[
                    {"severity": "blocker", "summary": "data loss", "confidence": 0.9},
                    {"severity": "major", "summary": "race condition", "confidence": 0.7},
                ],
            ),
            cost_usd=0.1,
            duration_s=1.0,
        ),
        RunnerResult(
            text=_critic_response(
                "elegance",
                concerns=[
                    {"severity": "minor", "summary": "naming nit", "confidence": 0.4},
                ],
            ),
            cost_usd=0.1,
            duration_s=1.0,
        ),
    ]
    agg = _aggregate_round_concerns(["safety", "elegance"], results)
    assert agg["blocker_count"] == 1
    assert agg["major_count"] == 1
    assert agg["minor_count"] == 1
    assert agg["medium_count"] == 0
    assert len(agg["all_concerns"]) == 3
    assert agg["parse_errors"] == {}
    assert len(agg["fingerprints"]) == 3


def test_aggregate_round_concerns_records_parse_errors():
    """Если critic output malformed — aggregator skips но не raises. Error
    идёт в parse_errors dict для facilitator visibility."""
    results = [
        RunnerResult(text="# Critic: safety\nbroken output\n", cost_usd=0.1, duration_s=1.0),
        RunnerResult(
            text=_critic_response(
                "elegance",
                concerns=[{"severity": "minor", "summary": "ok", "confidence": 0.5}],
            ),
            cost_usd=0.1,
            duration_s=1.0,
        ),
    ]
    agg = _aggregate_round_concerns(["safety", "elegance"], results)
    assert "safety" in agg["parse_errors"]
    assert agg["concerns_by_critic"]["safety"] == []
    assert len(agg["concerns_by_critic"]["elegance"]) == 1
    assert agg["minor_count"] == 1


def test_render_structured_concerns_block_includes_aggregate_summary():
    """Rendered block содержит header counts + per-critic concerns."""
    results = [
        RunnerResult(
            text=_critic_response(
                "safety",
                concerns=[
                    {"severity": "blocker", "summary": "x", "confidence": 0.9},
                ],
            ),
            cost_usd=0.1,
            duration_s=1.0,
        ),
    ]
    agg = _aggregate_round_concerns(["safety"], results)
    rendered = _render_structured_concerns_block(agg)
    assert "structured_concerns" in rendered
    assert "blockers=1" in rendered
    assert "safety-C1" in rendered
    assert "severity: blocker" in rendered


# ---- _compute_plateau_metrics ----


def test_plateau_not_enough_rounds():
    """Need ≥2 rounds для measurement."""
    metrics = _compute_plateau_metrics([{"round_n": 1, "fingerprints": {"a"}, "blockers": 0}])
    assert metrics["is_plateau"] is False
    assert "need" in metrics["reason"]


def test_plateau_detects_quality_treadmill():
    """Blockers стабильно 0 + >50% concerns brand new → plateau."""
    rounds = [
        {"round_n": 1, "fingerprints": {"fp_a", "fp_b"}, "blockers": 0},
        {"round_n": 2, "fingerprints": {"fp_c", "fp_d", "fp_e"}, "blockers": 0},
    ]
    metrics = _compute_plateau_metrics(rounds)
    assert metrics["is_plateau"] is True
    assert metrics["delta_blockers"] == 0
    assert metrics["new_fp_ratio"] == 1.0  # 100% new


def test_plateau_blocker_present_disqualifies():
    """Если хоть один BLOCKER в последнем round — это НЕ plateau (real work
    остался). Plateau только когда critics нашли мелочи без showstoppers."""
    rounds = [
        {"round_n": 1, "fingerprints": {"a"}, "blockers": 0},
        {"round_n": 2, "fingerprints": {"b"}, "blockers": 1},
    ]
    metrics = _compute_plateau_metrics(rounds)
    assert metrics["is_plateau"] is False


def test_plateau_repeated_concerns_disqualifies():
    """Когда critics повторяют один и тот же концерн — это normal redesign
    signal (recurring theme), не plateau. Plateau это когда КАЖДЫЙ ROUND
    fresh concerns."""
    rounds = [
        {"round_n": 1, "fingerprints": {"fp_a", "fp_b"}, "blockers": 0},
        # Same fingerprints — 0% new
        {"round_n": 2, "fingerprints": {"fp_a", "fp_b"}, "blockers": 0},
    ]
    metrics = _compute_plateau_metrics(rounds)
    assert metrics["is_plateau"] is False
    assert metrics["new_fp_ratio"] == 0.0


def test_plateau_empty_fingerprints_in_last_round():
    """Edge case: critic нашёл blockers без structured items → fingerprints
    empty но blockers могут быть >0. Не plateau (нет concerns для tracking)."""
    rounds = [
        {"round_n": 1, "fingerprints": {"a"}, "blockers": 0},
        {"round_n": 2, "fingerprints": set(), "blockers": 0},
    ]
    metrics = _compute_plateau_metrics(rounds)
    assert metrics["is_plateau"] is False  # ratio = 0 → не quality treadmill


def test_approved_with_followups_writes_followups_md(tmp_path):
    """End-to-end: facilitator emits approved_with_followups → engine writes
    followups.md под design_review/ для impl_planner pickup. RFC ships без
    redesign loop."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    awf_verdict = (
        "ship-safe with concerns\n```json\n"
        '{"verdict": "approved_with_followups", "reason": "blockers all addressed; '
        'bounded followups tracked",'
        '"followups": ['
        '{"category": "safety", "summary": "add edge case for empty tz",'
        '"rfc_section": "Tests", "severity": "FOLLOWUP"},'
        '{"category": "elegance", "summary": "extract render helper later",'
        '"rfc_section": "Proposed approach", "severity": "FOLLOWUP"}'
        "]}\n```\n"
    )
    responses = [
        RunnerResult(text="safety: APPROVE_WITH_FOLLOWUPS", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="elegance: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text="hallucination: APPROVE", cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=awf_verdict, cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(_stage_def(), ctx)

    # Loop exit на R1 — no responder revision needed since verdict ship-safe
    assert len(runner.calls) == 4
    assert result["verdict"] == "approved_with_followups"
    # followups.md создан под design_review/ — impl_planner будет читать flat path
    followups_md = (task_dir / "design_review" / "followups.md").read_text()
    assert "# Followups — design_review pass 1" in followups_md
    assert "## safety" in followups_md
    assert "## elegance" in followups_md
    assert "add edge case for empty tz" in followups_md
    assert "extract render helper later" in followups_md
    # Verdict.md mentions followups count
    verdict_text = (task_dir / "design_review" / "verdict.md").read_text()
    assert "approved_with_followups" in verdict_text
    assert "Followups tracked" in verdict_text
    # No redesign_signal.md (это ship verdict, не bubble back)
    assert not (task_dir / "design" / "redesign_signal.md").exists()


def test_plateau_signal_injected_into_facilitator_msg(tmp_path):
    """End-to-end: critics дают NEW concerns each round без BLOCKERs → plateau
    detected на R2 → facilitator получает plateau_signal block в его msg
    (готовит его emit approved_with_followups)."""
    repo, task_dir = _make_repo_and_task(tmp_path)
    stage_def = _stage_def(max_rounds=3)
    # R1: 2 new concerns без blockers. R2: 2 brand-new concerns без blockers →
    # plateau detected (100% new_fp_ratio, blockers stable at 0).
    def _r1(name):
        return _critic_response(
            name,
            concerns=[
                {"severity": "medium", "summary": "first R1 widget validation gap", "confidence": 0.6},
                {"severity": "minor", "summary": "second R1 render layout nit", "confidence": 0.5},
            ],
        )

    def _r2(name):
        return _critic_response(
            name,
            concerns=[
                {"severity": "medium", "summary": "fresh R2 cache invalidation timing", "confidence": 0.7},
                {"severity": "minor", "summary": "fresh R2 timezone serialization edge", "confidence": 0.4},
            ],
        )
    awf_verdict = (
        "ship-safe with plateau\n```json\n"
        '{"verdict": "approved_with_followups", "reason": "plateau acknowledged",'
        '"followups": ['
        '{"category": "elegance", "summary": "cache timing concern",'
        '"rfc_section": "Proposed approach", "severity": "FOLLOWUP"}'
        "]}\n```\n"
    )
    responses = [
        # R1: 3 critics + facilitator (needs_revision)
        RunnerResult(text=_r1("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r1("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r1("hallucination"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_verdict_block("needs_revision", "concerns"), cost_usd=0.05, duration_s=2.0
        ),
        # Responder revision
        RunnerResult(text="# RFC v2", cost_usd=0.30, duration_s=8.0),
        # R2: critics dump 2 brand-new concerns → plateau
        RunnerResult(text=_r2("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r2("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r2("hallucination"), cost_usd=0.10, duration_s=3.0),
        # Facilitator (gets plateau signal) — emits approved_with_followups
        RunnerResult(text=awf_verdict, cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)

    result = stage.run(stage_def, ctx)

    # Facilitator call на R2 = index 8 в calls (0-7: R1 + responder + R2 critics)
    facilitator_r2_msg = runner.calls[8][1]
    assert "plateau_signal" in facilitator_r2_msg
    assert "PLATEAU DETECTED" in facilitator_r2_msg
    assert "approved_with_followups" in facilitator_r2_msg
    # R1 facilitator (call index 3) НЕ должен иметь plateau signal — слишком
    # рано (need ≥2 rounds для measurement)
    facilitator_r1_msg = runner.calls[3][1]
    assert "plateau_signal" not in facilitator_r1_msg
    # Plateau triggered gate → gate overrode arbiter's AWF to approved
    # (numbers clean, gate ships). Followups больше не writes когда gate активен
    # — gate-decided approved не имеет followups[].
    assert result["verdict"] == "approved"


def test_awf_verdict_disabled_downgrades_to_needs_revision(monkeypatch):
    """CODE_LOOPS_AWF_VERDICT=0 → если facilitator всё равно emit AWF,
    parser downgrade к needs_revision с marker. Это safety net для ablation
    runs где мы хотим test что Step 3+4 (rubric + GPT-5) делают без AWF."""
    monkeypatch.setenv("CODE_LOOPS_AWF_VERDICT", "0")
    text = (
        '```json\n'
        '{"verdict": "approved_with_followups", "reason": "ship-ready",'
        '"followups": [{"category": "safety", "summary": "x",'
        '"rfc_section": "Tests", "severity": "FOLLOWUP"}]}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "needs_revision"
    assert "downgraded" in v["reason"]
    assert "CODE_LOOPS_AWF_VERDICT=0" in v["reason"]


def test_awf_verdict_enabled_default_passes_through(monkeypatch):
    """По default (без env var) AWF parser работает as designed."""
    monkeypatch.delenv("CODE_LOOPS_AWF_VERDICT", raising=False)
    text = (
        '```json\n'
        '{"verdict": "approved_with_followups", "reason": "ship",'
        '"followups": [{"category": "safety", "summary": "x",'
        '"rfc_section": "Tests", "severity": "FOLLOWUP"}]}\n```\n'
    )
    v = _parse_facilitator_verdict(text)
    assert v["status"] == "approved_with_followups"
    assert len(v["followups"]) == 1


def test_awf_disabled_injects_note_in_facilitator_msg(tmp_path, monkeypatch):
    """Когда CODE_LOOPS_AWF_VERDICT=0, в facilitator msg добавляется
    explicit инструкция: 'AWF disabled — choose from approved/needs_revision/
    redesign_needed'. Это primary gate, parser downgrade — secondary safety net."""
    monkeypatch.setenv("CODE_LOOPS_AWF_VERDICT", "0")
    repo, task_dir = _make_repo_and_task(tmp_path)
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

    # Facilitator msg (index 3 = после 3 critics) должен содержать awf_disabled note
    facilitator_msg = runner.calls[3][1]
    assert "awf_disabled" in facilitator_msg
    assert "approved_with_followups verdict is DISABLED" in facilitator_msg
    assert "needs_revision" in facilitator_msg  # mentions alternatives


def test_plateau_signal_disabled_via_env_var(tmp_path, monkeypatch):
    """CODE_LOOPS_PLATEAU_DETECTOR=0 → trajectory всё ещё в manifest,
    но plateau_signal block НЕ инжектится в facilitator msg. Позволяет
    ablation: запустить retry без plateau bias, увидеть baseline behavior
    critics+facilitator на тех же inputs."""
    monkeypatch.setenv("CODE_LOOPS_PLATEAU_DETECTOR", "0")
    repo, task_dir = _make_repo_and_task(tmp_path)
    stage_def = _stage_def(max_rounds=3)
    r1_critic = "## Concerns\n1. R1 concern A\n2. R1 concern B\n"
    r2_critic = "## Concerns\n1. R2 fresh C\n2. R2 fresh D\n"
    responses = [
        # R1: 3 critics + facilitator
        RunnerResult(text=r1_critic, cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=r1_critic, cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=r1_critic, cost_usd=0.10, duration_s=3.0),
        RunnerResult(
            text=_verdict_block("needs_revision", "x"), cost_usd=0.05, duration_s=2.0
        ),
        # Responder
        RunnerResult(text="# RFC v2", cost_usd=0.30, duration_s=8.0),
        # R2: critics dump новые concerns → plateau WOULD detect
        RunnerResult(text=r2_critic, cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=r2_critic, cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=r2_critic, cost_usd=0.10, duration_s=3.0),
        # Facilitator → emit approved (без plateau bias)
        RunnerResult(text=_verdict_block("approved", "all clear"), cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    ctx = StageContext(task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo)
    stage.run(stage_def, ctx)

    # Facilitator R2 msg (index 8) — НЕ должен содержать plateau_signal
    facilitator_r2_msg = runner.calls[8][1]
    assert "plateau_signal" not in facilitator_r2_msg
    assert "PLATEAU DETECTED" not in facilitator_r2_msg
    # Trajectory всё равно accumulated — это только GATE на signal injection
    # (метрики мониторятся независимо от того, использованы они или нет)


def test_plateau_metrics_recorded_in_manifest(tmp_path):
    """Per-round plateau trajectory сохраняется в manifest.plateau_metrics
    для tuning threshold values на real runs. + followups_count для
    distribution analysis (≥10 → cap kicked, signal split RFC)."""
    from code_loops.artifact_writer import ArtifactWriter
    from code_loops.manifest import Manifest

    repo, task_dir = _make_repo_and_task(tmp_path)

    def _r1(name):
        return _critic_response(
            name,
            concerns=[
                {"severity": "medium", "summary": f"{name} R1 concern A", "confidence": 0.6},
                {"severity": "minor", "summary": f"{name} R1 concern B", "confidence": 0.5},
            ],
        )

    def _r2(name):
        return _critic_response(
            name,
            concerns=[
                {"severity": "medium", "summary": f"{name} fresh new C", "confidence": 0.7},
                {"severity": "minor", "summary": f"{name} fresh new D", "confidence": 0.4},
            ],
        )

    awf_verdict = (
        "ship-safe\n```json\n"
        '{"verdict": "approved_with_followups", "reason": "ok",'
        '"followups": [{"category": "safety", "summary": "x",'
        '"rfc_section": "Tests", "severity": "FOLLOWUP"}]}\n```\n'
    )
    responses = [
        # R1: 3 critics
        RunnerResult(text=_r1("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r1("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r1("hallucination"), cost_usd=0.10, duration_s=3.0),
        # R1 facilitator
        RunnerResult(text=_verdict_block("needs_revision", "x"), cost_usd=0.05, duration_s=2.0),
        # Responder
        RunnerResult(text="# RFC v2", cost_usd=0.30, duration_s=8.0),
        # R2 critics
        RunnerResult(text=_r2("safety"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r2("elegance"), cost_usd=0.10, duration_s=3.0),
        RunnerResult(text=_r2("hallucination"), cost_usd=0.10, duration_s=3.0),
        # R2 facilitator → AWF
        RunnerResult(text=awf_verdict, cost_usd=0.05, duration_s=2.0),
    ]
    runner = ScriptedRunner(responses)
    stage = DebateCritiqueStage(FakeFactory(runner))
    manifest = Manifest(task_dir / "manifest.json")
    manifest.init_task("0001_t", mode="feature")
    aw = ArtifactWriter(task_dir, manifest)
    ctx = StageContext(
        task_dir=task_dir, prompts_dir=repo / "agents", repo_root=repo, artifact_writer=aw
    )

    stage.run(_stage_def(max_rounds=3), ctx)

    passes = manifest.data["stages"]["design_review"]["passes"]
    assert len(passes) == 1
    pass_entry = passes[0]
    # plateau_metrics с trajectory per-round
    assert "plateau_metrics" in pass_entry
    trajectory = pass_entry["plateau_metrics"]["rounds"]
    assert len(trajectory) == 2  # R1 + R2 fingerprinted before facilitator
    assert trajectory[0]["round_n"] == 1
    assert trajectory[0]["fp_count"] == 6  # 3 critics × 2 concerns each в R1
    assert trajectory[0]["is_plateau"] is False  # R1 too early
    assert trajectory[1]["round_n"] == 2
    assert trajectory[1]["is_plateau"] is True  # quality treadmill detected
    assert trajectory[1]["new_fp_ratio"] == 1.0  # 100% new
    # Plateau triggered gate → gate overrode arbiter's AWF to approved
    # (numbers within ceilings). Final verdict ships без followups.
    assert pass_entry["verdict"] == "approved"


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
