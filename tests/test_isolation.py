"""Tests for input slicing per specialization."""

from __future__ import annotations

from code_loops.isolation import slice_questions_for_spec

PLAN_FIXTURE = """\
# Plan: Some title

## Scope
Some scope.

## Research questions

1. **[codebase]** Where is event/note linkage defined and what fields connect them?
2. **[prompts]** Which prompts already touch the briefing context builder?
3. **[problems_lessons]** Have we had incidents around mood-trend or series binding?
4. **[codebase]** Does OutgoingQueue support arbitrary bytes as a binary document attachment?
5. **[prompts]** What format conventions does weekly_digest.md use?

## Expected modules
- foo
"""


def test_slice_codebase_returns_only_codebase_questions():
    out = slice_questions_for_spec(PLAN_FIXTURE, "codebase")
    assert "Where is event/note linkage" in out
    assert "OutgoingQueue support arbitrary bytes" in out
    assert "Which prompts already touch" not in out
    assert "incidents around mood-trend" not in out
    # Numbering preserved
    assert out.startswith("1. ")
    assert "4. " in out


def test_slice_prompts_returns_only_prompts_questions():
    out = slice_questions_for_spec(PLAN_FIXTURE, "prompts")
    assert "Which prompts already touch" in out
    assert "weekly_digest.md use" in out
    assert "event/note linkage" not in out
    assert out.startswith("2. ")


def test_slice_problems_lessons_returns_only_those():
    out = slice_questions_for_spec(PLAN_FIXTURE, "problems_lessons")
    assert "mood-trend or series binding" in out
    assert "event/note linkage" not in out
    assert "weekly_digest.md" not in out
    assert out.startswith("3. ")


def test_slice_unknown_spec_returns_fallback():
    out = slice_questions_for_spec(PLAN_FIXTURE, "nonexistent")
    assert "No questions tagged" in out
    assert "[nonexistent]" in out


def test_multiline_question_body_preserved():
    plan_with_multiline = """\
1. **[codebase]** First question
   with a continuation line that wraps.
2. **[prompts]** Second.
"""
    out = slice_questions_for_spec(plan_with_multiline, "codebase")
    assert "First question" in out
    assert "continuation line" in out
    assert "Second." not in out
