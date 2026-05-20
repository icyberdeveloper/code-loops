"""Curate inputs for stage handlers — each agent gets only what it needs.

Hard isolation principle: a parallel research branch should not see questions
tagged for other specializations. The Planner emits questions tagged
`**[codebase]**`, `**[prompts]**`, `**[problems_lessons]**`. We slice by tag
before handing off to each branch.
"""

from __future__ import annotations

import re

MANDATORY_PERSPECTIVE = "structural_skeptic"
# Триггерится cross-pass theme detector в debate_critique — добавляется
# automatically когда meta_reformulation_required в redesign_signal.
# Hard mandate "you MUST oppose framing" — research: 99.2% disagreement
# rate vs 48% для soft instructions (OpenReview mxBmj5LYU2).
DEVILS_ADVOCATE_PERSPECTIVE = "devils_advocate"


def parse_perspectives(plan_md: str) -> list[str]:
    """Extract perspective names from the perspectives_for_rfc YAML block in plan.md.

    Expected block format:
        perspectives_for_rfc:
          - structural_skeptic
          - simplicity
          - data_integrity
          - operational

    Returns a safe default if the block is missing or malformed so the
    pipeline can still proceed without a useless crash.

    Defense-in-depth: prepends MANDATORY_PERSPECTIVE if the planner
    forgot it. This perspective breaks the architect-blind-spot loop
    documented in run #3 (3 redesign loops, all rejected on same theme
    `validator_pipeline_trust_drift` because architect anchored on
    symptom location each time).
    """
    pattern = re.compile(
        r"perspectives_for_rfc:\s*\n((?:\s*-\s*[\w_]+\s*\n?)+)",
        re.MULTILINE,
    )
    m = pattern.search(plan_md)
    if not m:
        return [MANDATORY_PERSPECTIVE, "simplicity", "correctness"]
    perspectives: list[str] = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            perspectives.append(stripped[1:].strip())
    if not perspectives:
        return [MANDATORY_PERSPECTIVE, "simplicity", "correctness"]
    if MANDATORY_PERSPECTIVE not in perspectives:
        perspectives.insert(0, MANDATORY_PERSPECTIVE)
    return perspectives


def is_meta_reformulation_signaled(task_dir) -> bool:
    """Проверяет наличие marker `meta_reformulation_required = true` в
    redesign_signal.md.

    Marker эмиттится cross-pass theme detector в debate_critique когда
    recurring_theme повторяется в 2+ passes подряд. Когда True →
    debate_writer auto-добавляет DEVILS_ADVOCATE_PERSPECTIVE в список
    perspectives для текущего debate.

    Принимает task_dir как Path или str. Несуществующий signal → False
    (regular run, не redesign).
    """
    from pathlib import Path as _Path

    signal_path = _Path(task_dir) / "design" / "redesign_signal.md"
    if not signal_path.exists():
        return False
    try:
        return "meta_reformulation_required = true" in signal_path.read_text()
    except OSError:
        return False


def add_devils_advocate_if_signaled(perspectives: list[str], task_dir) -> list[str]:
    """Добавляет devils_advocate в perspectives когда meta_reformulation
    triggered, не дублирует если уже в списке.

    Используется debate_writer.run() сразу после parse_perspectives() —
    чтобы списки perspectives рассчитанные в plan.md дополнились
    framing-challenger'ом ТОЛЬКО когда нужно (recurring pattern detected).
    Не добавляется в обычных run без redesign_signal.
    """
    if not is_meta_reformulation_signaled(task_dir):
        return perspectives
    if DEVILS_ADVOCATE_PERSPECTIVE in perspectives:
        return perspectives
    # Prepend для приоритета — devils_advocate первый видит RFC
    return [DEVILS_ADVOCATE_PERSPECTIVE, *perspectives]


def slice_questions_for_spec(plan_md: str, spec: str) -> str:
    """Return only questions tagged with [<spec>] from plan.md, preserving numbering.

    Plan format from agents/research/research-lead.md:
        1. **[codebase]** Where is event/note linkage defined ...
        2. **[prompts]** Which prompts touch ...
    """
    pattern = re.compile(
        r"^(\d+)\.\s*\*\*\[" + re.escape(spec) + r"\]\*\*\s+(.+?)(?=^\d+\.\s*\*\*\[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    matches = []
    for m in pattern.finditer(plan_md):
        text = m.group(2).strip()
        matches.append(f"{m.group(1)}. {text}")
    if not matches:
        return (
            f"(No questions tagged [{spec}] found in plan. "
            "Cover what is in scope for your specialization based on task.md.)"
        )
    return "\n\n".join(matches)
