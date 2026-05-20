"""Тесты для cross-pass theme detector в debate_critique.

Детектит когда recurring_theme повторяется across passes (не только
within одного pass debate как facilitator). При recurrence — trigger
meta-cognitive reformulation block в redesign_signal.

Research backing: RAGEN Echo Trap, MAR Confirmation Bias — patches на
одном attractor дают diminishing returns; только reformulation breaks
loop. См. _build_meta_reformulation_block docstring.
"""

from __future__ import annotations

from pathlib import Path

from code_loops.stages.debate_critique import (
    _build_meta_reformulation_block,
    _is_theme_recurring,
    _load_prior_pass_verdicts,
    _normalize_theme_tokens,
)


def _write_verdict(task_dir: Path, pass_n: int, theme: str | None, status: str = "redesign_needed"):
    """Helper — пишет verdict.md в той же форме что debate_critique."""
    pass_dir = task_dir / "design_review" / f"pass_{pass_n}"
    pass_dir.mkdir(parents=True, exist_ok=True)
    body = f"# Verdict: {status}\n\n**Reason:** test\n\n"
    if theme:
        body += f"**Recurring theme:** `{theme}`\n"
    (pass_dir / "verdict.md").write_text(body)


# ---- _load_prior_pass_verdicts ----


def test_load_prior_pass_verdicts_returns_empty_for_pass_1(tmp_path):
    """В pass_1 нет prior — empty list, не ошибка."""
    out = _load_prior_pass_verdicts(tmp_path, current_pass_n=1)
    assert out == []


def test_load_prior_pass_verdicts_loads_themes_from_existing_passes(tmp_path):
    _write_verdict(tmp_path, 1, "alpha_theme")
    _write_verdict(tmp_path, 2, "beta_theme")
    out = _load_prior_pass_verdicts(tmp_path, current_pass_n=3)
    assert len(out) == 2
    assert out[0]["pass_n"] == 1
    assert out[0]["recurring_theme"] == "alpha_theme"
    assert out[0]["verdict_status"] == "redesign_needed"
    assert out[1]["pass_n"] == 2
    assert out[1]["recurring_theme"] == "beta_theme"


def test_load_prior_pass_verdicts_skips_missing_verdicts(tmp_path):
    """Missing pass_2 verdict — graceful skip, не raise."""
    _write_verdict(tmp_path, 1, "alpha")
    # pass_2 не записываем
    _write_verdict(tmp_path, 3, "gamma")
    out = _load_prior_pass_verdicts(tmp_path, current_pass_n=4)
    assert len(out) == 2
    assert [v["pass_n"] for v in out] == [1, 3]


def test_load_prior_pass_verdicts_verdict_without_theme_returns_none(tmp_path):
    """Verdict approved (без recurring_theme) — recurring_theme = None."""
    _write_verdict(tmp_path, 1, theme=None, status="approved")
    out = _load_prior_pass_verdicts(tmp_path, current_pass_n=2)
    assert len(out) == 1
    assert out[0]["recurring_theme"] is None


# ---- _normalize_theme_tokens ----


def test_normalize_theme_tokens_splits_by_underscore():
    assert _normalize_theme_tokens("canonical_typo_partition_completeness") == {
        "canonical",
        "typo",
        "partition",
        "completeness",
    }


def test_normalize_theme_tokens_drops_short_tokens():
    """Tokens короче 4 символов отбрасываются — слишком noisy для fuzzy match."""
    assert _normalize_theme_tokens("a_bc_def_validator") == {"validator"}


def test_normalize_theme_tokens_handles_none():
    assert _normalize_theme_tokens(None) == set()


def test_normalize_theme_tokens_handles_empty():
    assert _normalize_theme_tokens("") == set()


# ---- _is_theme_recurring ----


def test_is_theme_recurring_no_priors_returns_false():
    is_rec, passes, count = _is_theme_recurring("any_theme", [])
    assert is_rec is False
    assert passes == []
    assert count == 0


def test_is_theme_recurring_exact_match():
    priors = [
        {"pass_n": 1, "recurring_theme": "partition_completeness"},
        {"pass_n": 2, "recurring_theme": "other_concern"},
    ]
    is_rec, passes, count = _is_theme_recurring("partition_completeness", priors)
    assert is_rec is True
    assert passes == [1]
    assert count == 2  # 1 prior + current


def test_is_theme_recurring_fuzzy_match():
    """`partition_completeness` ⊆ `canonical_typo_partition_completeness` через
    token overlap ≥ 2 значимых tokens (partition, completeness)."""
    priors = [
        {"pass_n": 2, "recurring_theme": "partition_completeness"},
        {"pass_n": 3, "recurring_theme": "canonical_typo_partition_completeness"},
    ]
    is_rec, passes, count = _is_theme_recurring("partition_completeness", priors)
    assert is_rec is True
    assert passes == [2, 3]  # Оба считаются recurring через fuzzy match
    assert count == 3  # 2 priors + current


def test_is_theme_recurring_no_overlap_returns_false():
    priors = [
        {"pass_n": 1, "recurring_theme": "validator_pipeline_trust_drift"},
    ]
    is_rec, _passes, _count = _is_theme_recurring("partition_completeness", priors)
    assert is_rec is False


def test_is_theme_recurring_skips_priors_without_theme():
    """Approved pass'ы без recurring_theme — skip."""
    priors = [
        {"pass_n": 1, "recurring_theme": None},
        {"pass_n": 2, "recurring_theme": "partition_completeness"},
    ]
    is_rec, passes, count = _is_theme_recurring("partition_completeness", priors)
    assert is_rec is True
    assert passes == [2]
    assert count == 2


def test_is_theme_recurring_single_token_overlap_not_enough():
    """Только 1 общий token (например 'validator') — недостаточно для fuzzy
    match чтобы избежать false-positive на generic слова."""
    priors = [
        {"pass_n": 1, "recurring_theme": "validator_api_drift"},
    ]
    is_rec, _, _ = _is_theme_recurring("validator_typo_partition", priors)
    assert is_rec is False  # Только 1 общий token "validator", не достаточно


def test_is_theme_recurring_current_theme_none_returns_false():
    """Если facilitator не emit recurring_theme в current pass — нечего
    recur'ить."""
    priors = [{"pass_n": 1, "recurring_theme": "x_theme"}]
    is_rec, _, _ = _is_theme_recurring(None, priors)
    assert is_rec is False


# ---- _build_meta_reformulation_block ----


def test_meta_reformulation_block_includes_recurrence_evidence():
    block = _build_meta_reformulation_block(
        "partition_completeness",
        matching_passes=[2, 3],
        recurrence_count=3,
        current_pass_n=4,
    )
    # Доказательства recurrence
    assert "partition_completeness" in block
    assert "pass_2" in block
    assert "pass_3" in block
    assert "pass_4" in block
    assert "recurrence count: 3" in block


def test_meta_reformulation_block_hard_mandate_language():
    """Per research (99.2% vs 48% disagreement): explicit MUST/MANDATORY/
    BINDING language. Soft language не work."""
    block = _build_meta_reformulation_block("x", [1], 2, 2)
    assert "MANDATORY" in block
    assert "STEP BACK" in block
    assert "FORBIDDEN" in block  # T1 binary запрещён в reformulation
    assert "BINDING" not in block.upper() or "binding" in block.lower()


def test_meta_reformulation_block_enumerates_axis3_options():
    """Все T1..T5 framing options перечислены чтобы architect видел
    full spectrum reformulations."""
    block = _build_meta_reformulation_block("x", [1], 2, 2)
    assert "**T1**" in block
    assert "**T2**" in block
    assert "**T3**" in block
    assert "**T4**" in block
    assert "**T5**" in block


def test_meta_reformulation_block_includes_flag():
    """Machine-readable flag для downstream parsing (debate_writer reads
    redesign_signal и проверяет flag)."""
    block = _build_meta_reformulation_block("x", [1], 2, 2)
    assert "meta_reformulation_required = true" in block


def test_meta_reformulation_block_requires_step_back_section():
    """Шаг 1 mandate — architect должен write `## Step-back reframing`
    section ДО Phase 1. Block должен явно указать это требование."""
    block = _build_meta_reformulation_block("x", [1], 2, 2)
    assert "Step-back reframing" in block
    assert "5 whys" in block
