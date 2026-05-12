"""Curate inputs for stage handlers — each agent gets only what it needs.

Hard isolation principle: a parallel research branch should not see questions
tagged for other specializations. The Planner emits questions tagged
`**[codebase]**`, `**[prompts]**`, `**[problems_lessons]**`. We slice by tag
before handing off to each branch.
"""

from __future__ import annotations

import re


def parse_perspectives(plan_md: str) -> list[str]:
    """Extract perspective names from the perspectives_for_rfc YAML block in plan.md.

    Expected block format:
        perspectives_for_rfc:
          - simplicity
          - data_integrity
          - operational

    Returns a safe default if the block is missing or malformed so the
    pipeline can still proceed without a useless crash.
    """
    pattern = re.compile(
        r"perspectives_for_rfc:\s*\n((?:\s*-\s*[\w_]+\s*\n?)+)",
        re.MULTILINE,
    )
    m = pattern.search(plan_md)
    if not m:
        return ["simplicity", "correctness"]
    perspectives: list[str] = []
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("-"):
            perspectives.append(stripped[1:].strip())
    return perspectives or ["simplicity", "correctness"]


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
