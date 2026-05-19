"""Renders RFC JSON document into human-readable markdown.

RFC внутренне хранится как JSON (constrained decoding из rfc_schema)
для машинной обработки и enforcement file_path enum. Downstream stages
(критики, impl_planner, tech_writer) исторически читают markdown — этот
renderer конвертирует JSON в markdown с той же структурой что
software-architect.md описывает, без изменений downstream consumer-ов.

Пустые секции пропускаются (не рендерятся в пустые заголовки).
"""

from __future__ import annotations

from typing import Any


def render_rfc(rfc: dict[str, Any]) -> str:
    """Конвертирует RFC JSON в markdown."""
    parts: list[str] = []

    # Phase 1: Shapes considered (всегда есть)
    parts.append(_render_shapes(rfc.get("shapes_considered", {})))
    parts.append("")
    parts.append("## End of Phase 1 — proceed to Phase 2")
    parts.append("")
    parts.append("---")
    parts.append("")

    # Title
    title = rfc.get("title", "").strip() or "untitled"
    parts.append(f"# RFC: {title}")
    parts.append("")

    # Context
    context = rfc.get("context", "").strip()
    if context:
        parts.append("## Context")
        parts.append("")
        parts.append(context)
        parts.append("")

    # Proposed approach
    approach = rfc.get("proposed_approach", "").strip()
    if approach:
        parts.append("## Proposed approach")
        parts.append("")
        parts.append(approach)
        parts.append("")

    # File-level changes
    file_changes = rfc.get("file_changes", []) or []
    new_files = rfc.get("new_files_proposed", []) or []
    if file_changes or new_files:
        parts.append("## File-level changes")
        parts.append("")
        for fc in file_changes:
            path = fc.get("path", "?")
            mod = fc.get("modification", "").strip()
            line = f"- `{path}` — {mod}"
            parts.append(line)
            rationale = fc.get("rationale", "").strip()
            if rationale:
                parts.append(f"  - Rationale: {rationale}")
            side_effects = fc.get("side_effects", "").strip()
            if side_effects:
                parts.append(f"  - Side effects: {side_effects}")
        for nf in new_files:
            path = nf.get("path", "?")
            purpose = nf.get("purpose", "").strip()
            parts.append(f"- `{path}` **(new)** — {purpose}")
            key_exports = nf.get("key_exports", "").strip()
            if key_exports:
                parts.append(f"  - Key exports: {key_exports}")
        parts.append("")

    # Tests
    tests = rfc.get("tests", []) or []
    if tests:
        parts.append("## Tests")
        parts.append("")
        for t in tests:
            name = t.get("name", "?")
            desc = t.get("description", "").strip()
            kind = t.get("kind", "")
            tag = f" *({kind})*" if kind else ""
            parts.append(f"- **{name}**{tag} — {desc}")
        parts.append("")

    # Eval design (optional)
    eval_design = (rfc.get("eval_design") or "").strip()
    if eval_design:
        parts.append("## Eval design")
        parts.append("")
        parts.append(eval_design)
        parts.append("")

    # Alternatives considered
    alts = (rfc.get("alternatives_considered") or "").strip()
    if alts:
        parts.append("## Alternatives considered")
        parts.append("")
        parts.append(alts)
        parts.append("")

    # Decision log
    decisions = (rfc.get("decision_log") or "").strip()
    if decisions:
        parts.append("## Decision log")
        parts.append("")
        parts.append(decisions)
        parts.append("")

    # Risks
    risks = rfc.get("risks", []) or []
    if risks:
        parts.append("## Risks")
        parts.append("")
        for i, r in enumerate(risks, 1):
            title = r.get("title", "?")
            desc = r.get("description", "").strip()
            mitigation = (r.get("mitigation") or "").strip()
            severity = r.get("severity", "")
            sev_tag = f" [{severity}]" if severity else ""
            parts.append(f"{i}. **{title}**{sev_tag} — {desc}")
            if mitigation:
                parts.append(f"   Mitigation: {mitigation}")
        parts.append("")

    # Open questions
    open_q = (rfc.get("open_questions") or "").strip()
    if open_q:
        parts.append("## Open questions")
        parts.append("")
        parts.append(open_q)
        parts.append("")

    # Rollback
    rollback = (rfc.get("rollback") or "").strip()
    if rollback:
        parts.append("## Rollback")
        parts.append("")
        parts.append(rollback)
        parts.append("")

    # Revision notes (только в revision mode)
    revision = (rfc.get("revision_notes") or "").strip()
    if revision:
        parts.append("## Revision notes")
        parts.append("")
        parts.append(revision)
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def _render_shapes(shapes: dict[str, Any]) -> str:
    """Рендерит Phase 1 — Shapes considered."""
    parts: list[str] = ["## Shapes considered", ""]

    axis1 = shapes.get("axis1_options", []) or []
    if axis1:
        parts.append("**Axis 1: WHERE the fix lives in the signal flow**")
        parts.append("")
        for opt in axis1:
            letter = opt.get("letter", "?")
            desc = opt.get("description", "").strip()
            verdict = opt.get("verdict", "").strip()
            parts.append(f"- **{letter}.** {desc} — {verdict}")
        parts.append("")

    axis2 = shapes.get("axis2_options", []) or []
    if axis2:
        parts.append("**Axis 2: WHAT KIND of intervention**")
        parts.append("")
        for opt in axis2:
            letter = opt.get("letter", "?")
            desc = opt.get("description", "").strip()
            verdict = opt.get("verdict", "").strip()
            parts.append(f"- **{letter}.** {desc} — {verdict}")
        parts.append("")

    chosen = shapes.get("chosen", "").strip()
    if chosen:
        parts.append(f"**Cross-axis chosen shape: {chosen}.**")
        parts.append("")

    rationale = shapes.get("rationale", "").strip()
    if rationale:
        parts.append("**Rationale.**")
        parts.append(rationale)
        parts.append("")

    pm_constraint = (shapes.get("postmortem_constraint") or "").strip()
    if pm_constraint:
        parts.append(f"**Postmortem-mode constraint.** {pm_constraint}")
        parts.append("")

    rev_constraint = (shapes.get("revision_constraint") or "").strip()
    if rev_constraint:
        parts.append(f"**Revision-mode constraint.** {rev_constraint}")
        parts.append("")

    return "\n".join(parts)
