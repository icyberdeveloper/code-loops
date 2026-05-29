"""Integration test: GPT-5 actually emits arbiter verdict в parser-friendly формате.

Real API call (~$0.05-0.20 per run). Skip by default — runs только когда
RUN_INTEGRATION=1 env set. Verifies Gap C/E из cross-check: arbiter prompt
написан для Claude, но pipeline.yaml дispatch'ит facilitator на GPT-5.
Need confirm что GPT-5:
  1. Follows analysis-paragraph + JSON-code-block format
  2. Emits valid verdict enum value
  3. Russian language в analysis section (или хотя бы не ломает parser)
  4. _parse_facilitator_verdict() извлекает verdict correctly

Запуск: RUN_INTEGRATION=1 OPENAI_API_KEY=sk-... uv run pytest \
    tests/test_openai_arbiter_integration.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_loops.openai_runner import OpenAIRunner
from code_loops.stages.debate_critique import _parse_facilitator_verdict

_REPO = Path(__file__).resolve().parent.parent / "src" / "code_loops"
_ARBITER_PROMPT_PATH = _REPO / "agents" / "architects" / "architect-review-arbiter.md"


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_INTEGRATION") != "1",
    reason="Integration test — opt-in via RUN_INTEGRATION=1 (costs API tokens)",
)


def _make_critique_debate(verdict_kind: str) -> tuple[str, str]:
    """Сборка пары (current_rfc, debate_md) для arbiter input.

    verdict_kind: какой verdict мы пытаемся вызвать у GPT-5.
      - "approved": все critics APPROVE
      - "approved_with_followups": один critic APPROVE_WITH_FOLLOWUPS, нет blockers
      - "needs_revision": один critic BLOCKER
    """
    current_rfc = """# RFC: Add validator для surname spelling normalization

## Context
Validator currently inverts correct → incorrect spelling в edge cases.

## Proposed approach
Add downstream filter (Axis 1 C) с canonical typo dictionary.

## File changes
- app/validators/surname.py: add normalize_surname() function
- tests/test_surname.py: add test_inverted_correction_edge_case

## Tests
- Unit test для каждого known typo pair
- Integration test через full validation pipeline
"""
    if verdict_kind == "approved":
        debate = """## Round 1 — critic: safety

## Concerns
1. Edge case с empty surname not addressed
   - Suggestion: add guard early return

safety: APPROVE

## Round 1 — critic: elegance

elegance: APPROVE

## Round 1 — critic: hallucination

hallucination: APPROVE
"""
    elif verdict_kind == "approved_with_followups":
        debate = """## Round 1 — critic: safety

## Concerns
1. Timezone serialization not tested for cross-day validation
   - Bounded blast radius: only affects audit log timestamps

safety: APPROVE_WITH_FOLLOWUPS

## Round 1 — critic: elegance

## Concerns
1. normalize_surname() could be split into smaller helpers later
   - Localized to one file, refactorable post-ship

elegance: APPROVE_WITH_FOLLOWUPS

## Round 1 — critic: hallucination

hallucination: APPROVE
"""
    elif verdict_kind == "needs_revision":
        debate = """## Round 1 — critic: safety

## Concerns
1. **[BLOCKER]** normalize_surname() doesn't handle Unicode normalization;
   surrogate pairs will crash на input
   - Cite: §Proposed approach

safety: NEEDS_REVISION

## Round 1 — critic: elegance

elegance: APPROVE

## Round 1 — critic: hallucination

hallucination: APPROVE
"""
    else:
        raise ValueError(f"unknown verdict_kind: {verdict_kind}")
    return current_rfc, debate


def _run_arbiter(verdict_kind: str) -> dict:
    """Real GPT-5 call с arbiter prompt + critique debate inputs."""
    arbiter_sys = _ARBITER_PROMPT_PATH.read_text()
    current_rfc, debate = _make_critique_debate(verdict_kind)
    user_msg = (
        f"=== current_rfc ===\n{current_rfc}\n\n"
        f"=== debate.md ===\n{debate}\n"
    )
    runner = OpenAIRunner(model="gpt-5", effort="max")
    result = runner.run(arbiter_sys, user_msg)
    parsed = _parse_facilitator_verdict(result.text)
    parsed["_raw_text"] = result.text
    parsed["_cost"] = result.cost_usd
    parsed["_duration"] = result.duration_s
    return parsed


def test_gpt5_emits_approved_verdict_in_parseable_format():
    """All critics APPROVE → GPT-5 should emit verdict=approved."""
    result = _run_arbiter("approved")
    assert result["status"] in (
        "approved",
        "approved_with_followups",
    ), f"Expected approval-class verdict, got {result['status']}. Raw: {result['_raw_text'][:500]}"


def test_gpt5_emits_approved_with_followups_for_bounded_concerns():
    """Critics emit APPROVE_WITH_FOLLOWUPS → GPT-5 should match the new 4-tier
    verdict + emit non-empty followups array."""
    result = _run_arbiter("approved_with_followups")
    # Может быть approved или approved_with_followups — GPT-5 имеет discretion.
    # Если AWF — обязан followups non-empty (parser downgrade'нет к
    # needs_revision если empty, что закрашит assertion).
    if result["status"] == "approved_with_followups":
        assert "followups" in result
        assert len(result["followups"]) >= 1
        for f in result["followups"]:
            assert f["severity"] == "FOLLOWUP"
            assert f["category"] in {"safety", "elegance", "hallucination", "ai"}
    else:
        # Acceptable: GPT-5 решил что concerns достаточно minor → plain approved
        assert result["status"] == "approved", (
            f"Expected approved* verdict, got {result['status']}. Raw: {result['_raw_text'][:500]}"
        )


def test_gpt5_emits_needs_revision_for_blocker():
    """Critic emits [BLOCKER] safety: NEEDS_REVISION → arbiter must NOT ship.
    Acceptable: needs_revision или redesign_needed."""
    result = _run_arbiter("needs_revision")
    assert result["status"] in (
        "needs_revision",
        "redesign_needed",
    ), f"BLOCKER must not be approved-class; got {result['status']}. Raw: {result['_raw_text'][:500]}"


def test_gpt5_output_contains_json_code_block():
    """Verify parser-friendly format: response должен иметь ```json ... ``` fence
    (otherwise _parse_facilitator_verdict regex misses → fallback needs_revision)."""
    result = _run_arbiter("approved")
    raw = result["_raw_text"]
    assert "```json" in raw or "```" in raw, (
        f"GPT-5 response без code fence — parser regex не matches. Raw: {raw[:500]}"
    )
