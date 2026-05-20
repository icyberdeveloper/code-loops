"""Тесты для Devil's Advocate perspective auto-loading.

Devil's Advocate имеет hard mandate "you MUST oppose framing" (research:
99.2% disagreement vs 48% soft baseline, OpenReview mxBmj5LYU2). Auto-
добавляется в perspectives list только когда cross-pass detector установил
meta_reformulation_required в redesign_signal. Не активируется в обычных
runs — иначе бы давал noise каждому debate без recurring pattern.
"""

from __future__ import annotations

from pathlib import Path

from code_loops.isolation import (
    DEVILS_ADVOCATE_PERSPECTIVE,
    add_devils_advocate_if_signaled,
    is_meta_reformulation_signaled,
)


def _write_signal(task_dir: Path, content: str):
    (task_dir / "design").mkdir(parents=True, exist_ok=True)
    (task_dir / "design" / "redesign_signal.md").write_text(content)


# ---- is_meta_reformulation_signaled ----


def test_signaled_returns_false_when_no_signal_file(tmp_path):
    """Без redesign_signal.md (regular run) — не triggered."""
    assert is_meta_reformulation_signaled(tmp_path) is False


def test_signaled_returns_true_when_marker_present(tmp_path):
    _write_signal(tmp_path, "# Signal\n\nSome text\n\nmeta_reformulation_required = true\n")
    assert is_meta_reformulation_signaled(tmp_path) is True


def test_signaled_returns_false_when_marker_missing(tmp_path):
    """Signal есть, но не указывает reformulation — обычный redesign
    (architecturally нормальный, не recurring pattern)."""
    _write_signal(tmp_path, "# Signal\n\nShape shift to Layer B.\n")
    assert is_meta_reformulation_signaled(tmp_path) is False


def test_signaled_accepts_str_path(tmp_path):
    """Helper должен принимать как Path так и str (вызывается из разных
    мест: tests, debate_writer)."""
    _write_signal(tmp_path, "meta_reformulation_required = true")
    assert is_meta_reformulation_signaled(str(tmp_path)) is True


# ---- add_devils_advocate_if_signaled ----


def test_add_devils_advocate_when_signaled(tmp_path):
    _write_signal(tmp_path, "meta_reformulation_required = true")
    result = add_devils_advocate_if_signaled(
        ["structural_skeptic", "simplicity", "operational"], tmp_path
    )
    assert result[0] == DEVILS_ADVOCATE_PERSPECTIVE  # prepended для приоритета
    assert "simplicity" in result
    assert "operational" in result


def test_no_add_when_not_signaled(tmp_path):
    """Regular run без signal — devils_advocate не добавляется."""
    result = add_devils_advocate_if_signaled(["structural_skeptic", "simplicity"], tmp_path)
    assert DEVILS_ADVOCATE_PERSPECTIVE not in result
    assert result == ["structural_skeptic", "simplicity"]


def test_no_duplicate_when_already_in_list(tmp_path):
    """Если devils_advocate уже в списке (manually добавлен в plan.md) —
    не дублируется."""
    _write_signal(tmp_path, "meta_reformulation_required = true")
    result = add_devils_advocate_if_signaled(
        [DEVILS_ADVOCATE_PERSPECTIVE, "structural_skeptic"], tmp_path
    )
    assert result.count(DEVILS_ADVOCATE_PERSPECTIVE) == 1


def test_devils_advocate_prompt_file_exists():
    """Prompt файл должен существовать чтобы debate_writer мог его
    загрузить когда devils_advocate активирован."""
    # Resolve repo root относительно этого test file
    repo_root = Path(__file__).resolve().parent.parent / "src" / "code_loops"
    prompt_path = repo_root / "agents" / "architects" / "architect-perspective-devils-advocate.md"
    assert prompt_path.exists(), f"Devil's Advocate prompt missing at {prompt_path}"


def test_devils_advocate_prompt_has_hard_mandate():
    """Per research — hard mandate language essential. Soft не работает.
    Проверяем что prompt содержит explicit MUST oppose, не think critically."""
    repo_root = Path(__file__).resolve().parent.parent / "src" / "code_loops"
    prompt_path = repo_root / "agents" / "architects" / "architect-perspective-devils-advocate.md"
    body = prompt_path.read_text()
    # Hard mandate signals
    assert "MUST oppose" in body
    assert "REFRAME_REQUIRED" in body
    assert "FRAMING_DEFENDED" in body
    # Default behavior должен быть REFRAME, не оборона
    assert "Default behavior: REFRAME_REQUIRED" in body
    # Research backing visible
    assert "99.2%" in body  # citation of disagreement-rate research


def test_devils_advocate_prompt_enumerates_t1_t5():
    """Prompt должен перечислять T1..T5 framing palette чтобы Devil's
    Advocate имел concrete alternatives для предложения."""
    repo_root = Path(__file__).resolve().parent.parent / "src" / "code_loops"
    prompt_path = repo_root / "agents" / "architects" / "architect-perspective-devils-advocate.md"
    body = prompt_path.read_text()
    for tier in ["**T1**", "**T2**", "**T3**", "**T4**", "**T5**"]:
        assert tier in body, f"Missing framing option {tier}"
