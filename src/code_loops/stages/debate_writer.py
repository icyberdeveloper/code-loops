"""DebateWriterStage — RFC writer + N perspective agents + facilitator loop.

Flow:
  1. Writer drafts v1 from plan + research.
  2. For each round (max_rounds, default 5):
     a. N perspective agents in parallel critique the current draft.
        Each sees ONLY the draft + a brief task summary — never each other,
        never research, never debate history. Hard isolation.
        They receive `round_n`, `max_rounds`, and a `new_concerns_budget`
        (= max(1, max_rounds - round_n + 1)) so concerns narrow each round.
     b. Facilitator reads the full debate.md and decides convergence —
        based on whether the round raised any NEW THEMES, not whether
        every specific bug is closed out.
     c. If converged or max rounds: the latest draft becomes final.md.
     d. Else: Writer produces draft_v(N+1) addressing the round's critiques.
        Writer sees ONLY: previous draft + this round's perspective responses.

Per-role model/effort overrides come from stage_def["roles"][<role>] in
pipeline.yaml — typically Opus max for writer, Sonnet for cheap critique.

Cost / duration: cost is summed across every LLM call; duration is the
wall-clock total of the stage.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from rich.console import Console

from ..isolation import parse_perspectives
from ..runner import ClaudeRunner, RunnerFactory, RunnerResult
from .prompt import StageContext, load_agent_prompt
from .role_normalizer import normalize_roles

console = Console()


def _new_concerns_budget(round_n: int, max_rounds: int) -> int:
    """Per-round budget for NEW concerns (not previously raised by any perspective).

    Linear narrowing: round 1 of 5 → 5; round 2 → 4; ...; round 5 → 1. Min 1.
    Showstoppers above the budget are still allowed — see rfc_perspective.md.
    """
    return max(1, max_rounds - round_n + 1)


class DebateWriterStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        max_rounds: int = stage_def.get("max_rounds", 5)
        roles = normalize_roles(stage_def["roles"])
        out_dir = ctx.task_dir / "design"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Per-pass scoping (Step 9.40 Phase 2c). When ArtifactWriter is set,
        # all per-round artifacts (drafts, debate.md) live under
        # `design/pass_<N>/` so a redesign loop's pass 2 doesn't overwrite
        # pass 1's intermediate state. Legacy callers (no aw) write flat.
        if ctx.artifact_writer is not None:
            pass_n = ctx.artifact_writer.manifest.data.get("redesign_loop_count", 0) + 1
            work_dir = out_dir / f"pass_{pass_n}"
        else:
            pass_n = 1
            work_dir = out_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        debate_path = work_dir / "debate.md"
        debate_path.write_text("# RFC Debate\n")

        plan_md = (ctx.task_dir / "research_plan" / "plan.md").read_text()
        perspectives = parse_perspectives(plan_md)
        task_md = (ctx.task_dir / "task.md").read_text()
        research_dir = ctx.task_dir / "research"
        research_blocks = "\n\n".join(
            f"=== research/{p.name} ===\n{p.read_text()}" for p in sorted(research_dir.glob("*.md"))
        )

        writer_runner = self.factory.make(roles["writer"])
        perspective_runner = self.factory.make(roles["perspective"])
        facilitator_runner = self.factory.make(roles["facilitator"])

        writer_sys = load_agent_prompt(ctx.repo_root / roles["writer"]["prompt"], ctx)
        perspective_template = load_agent_prompt(
            ctx.repo_root / roles["perspective"]["prompt"], ctx
        )
        facilitator_sys = load_agent_prompt(ctx.repo_root / roles["facilitator"]["prompt"], ctx)

        cost_total = 0.0
        wall_start = time.monotonic()

        # ---- Initial draft (v1) ----
        # Detect redesign mode: if rfc/redesign_signal.md exists, the previous
        # critique cycle rejected an earlier RFC for design (not impl) reasons.
        # Pass that signal + the rejected RFC into the writer so it knows what
        # NOT to repeat.
        redesign_signal_path = ctx.task_dir / "design" / "redesign_signal.md"
        previous_rfc_path = ctx.task_dir / "design" / "previous_rfc.md"
        is_redesign = redesign_signal_path.exists()

        if is_redesign:
            console.print(
                f"  [bold yellow]rfc:[/bold yellow] REDESIGN MODE — incorporating "
                f"critique signal ({len(perspectives)} perspectives queued, "
                f"max_rounds={max_rounds})"
            )
            redesign_block = f"=== redesign_signal.md ===\n{redesign_signal_path.read_text()}\n\n"
            previous_block = (
                f"=== previous_rfc.md (REJECTED) ===\n{previous_rfc_path.read_text()}\n\n"
                if previous_rfc_path.exists()
                else ""
            )
            initial_msg = (
                f"{redesign_block}"
                f"{previous_block}"
                f"=== research_plan/plan.md ===\n{plan_md}\n\n"
                f"{research_blocks}\n\n"
                "REDESIGN MODE: the previous RFC attempt was rejected by critique for "
                "structural reasons described in redesign_signal.md. Do NOT patch the "
                "previous approach — propose a fundamentally different shape that "
                "makes the recurring failure modes structurally impossible. Cite the "
                "redesign_signal explicitly in your Context section."
            )
        else:
            console.print(
                f"  [dim]rfc:[/dim] writer drafting v1 "
                f"({len(perspectives)} perspectives queued, max_rounds={max_rounds})"
            )
            initial_msg = (
                f"=== research_plan/plan.md ===\n{plan_md}\n\n"
                f"{research_blocks}\n\n"
                "Produce the initial RFC draft (round 0). Perspective agents will critique "
                "it next."
            )
        result = writer_runner.run(writer_sys, initial_msg)
        cost_total += result.cost_usd or 0
        draft_version = 1
        draft_path = work_dir / f"draft_v{draft_version}.md"
        draft_path.write_text(result.text)
        _append_debate(debate_path, "Round 0 — Writer initial draft (v1)", result.text)
        console.print(
            f"  [dim]rfc:[/dim] ✓ draft_v1 ({result.duration_s:.0f}s, ${result.cost_usd or 0:.2f})"
        )

        # ---- Debate rounds ----
        converged = False
        for round_n in range(1, max_rounds + 1):
            current_draft = draft_path.read_text()
            budget = _new_concerns_budget(round_n, max_rounds)

            # Perspectives (parallel, isolated, budget-aware)
            console.print(
                f"  [dim]rfc:[/dim] round {round_n}/{max_rounds} — "
                f"{len(perspectives)} perspectives critiquing (new-concerns budget={budget})"
            )
            persp_results = asyncio.run(
                _run_perspectives(
                    runner=perspective_runner,
                    perspectives=perspectives,
                    perspective_template=perspective_template,
                    current_draft=current_draft,
                    task_md=task_md,
                    round_n=round_n,
                    max_rounds=max_rounds,
                )
            )
            for spec, pr in zip(perspectives, persp_results, strict=True):
                cost_total += pr.cost_usd or 0
                _append_debate(debate_path, f"Round {round_n} — perspective: {spec}", pr.text)
            persp_cost = sum((pr.cost_usd or 0) for pr in persp_results)
            persp_max_dur = max((pr.duration_s for pr in persp_results), default=0)
            console.print(
                f"  [dim]rfc:[/dim] ✓ perspectives ({persp_max_dur:.0f}s wall, ${persp_cost:.2f})"
            )

            # Facilitator
            facilitator_msg = (
                f"=== current_draft ===\n{current_draft}\n\n"
                f"=== debate.md ===\n{debate_path.read_text()}\n"
            )
            fac_result = facilitator_runner.run(facilitator_sys, facilitator_msg)
            cost_total += fac_result.cost_usd or 0
            _append_debate(debate_path, f"Round {round_n} — facilitator", fac_result.text)
            verdict = _parse_facilitator_verdict(fac_result.text)
            console.print(
                f"  [dim]rfc:[/dim] ✓ facilitator: "
                f"{'converged' if verdict['converged'] else 'continue'} "
                f"({fac_result.duration_s:.0f}s, ${fac_result.cost_usd or 0:.2f})"
            )

            if verdict["converged"]:
                converged = True
                break

            if round_n == max_rounds:
                _append_debate(
                    debate_path,
                    "STOPPED",
                    f"Max rounds ({max_rounds}) reached without convergence. "
                    "Using last draft as final.",
                )
                console.print(f"  [yellow]rfc:[/yellow] max_rounds={max_rounds} reached")
                break

            # Writer revises
            persp_block = "\n\n".join(
                f"=== {spec} perspective ===\n{r.text}"
                for spec, r in zip(perspectives, persp_results, strict=True)
            )
            revise_msg = (
                f"=== previous_draft.md (v{draft_version}) ===\n{current_draft}\n\n"
                f"=== perspective_responses (round {round_n}) ===\n{persp_block}\n\n"
                "Read each perspective response. Produce a fully revised RFC that "
                "addresses every substantive concern. Disagreements may be acknowledged "
                "and explained inline rather than capitulated to. End with "
                f"`## Revision notes for round {round_n}` listing what changed by "
                "perspective name."
            )
            revise_result = writer_runner.run(writer_sys, revise_msg)
            cost_total += revise_result.cost_usd or 0
            draft_version += 1
            draft_path = work_dir / f"draft_v{draft_version}.md"
            draft_path.write_text(revise_result.text)
            _append_debate(
                debate_path,
                f"Round {round_n} — Writer revision (v{draft_version})",
                revise_result.text,
            )
            console.print(
                f"  [dim]rfc:[/dim] ✓ draft_v{draft_version} "
                f"({revise_result.duration_s:.0f}s, ${revise_result.cost_usd or 0:.2f})"
            )

        # ---- Finalize ----
        # Per-pass final.md AND flat design/final.md for downstream stages.
        final_content = draft_path.read_text()
        pass_final_path = work_dir / "final.md"
        pass_final_path.write_text(final_content)
        flat_final_path = out_dir / "final.md"
        flat_final_path.write_text(final_content)
        wall_duration = time.monotonic() - wall_start
        rounds_done = draft_version - 1 if converged else max_rounds

        # Record pass in manifest (forensic per-pass detail).
        if ctx.artifact_writer is not None:
            ctx.artifact_writer.manifest.record_pass(
                "design",
                pass_n,
                cost_usd=cost_total,
                duration_s=wall_duration,
                rounds=rounds_done,
                converged=converged,
                max_rounds_reached=(not converged and rounds_done == max_rounds),
                final_artifact=f"design/pass_{pass_n}/final.md",
            )
            ctx.artifact_writer.manifest.set_latest("design", "design/final.md")

        outputs: dict[str, str] = {
            f"design/draft_v{draft_version}.md": draft_path.read_text(),
            "design/debate.md": debate_path.read_text(),
            "design/final.md": final_content,
        }
        return {
            "outputs": outputs,
            "cost_usd": cost_total,
            "duration_s": wall_duration,
            "converged": converged,
            "rounds": rounds_done,
        }


async def _run_perspectives(
    *,
    runner: ClaudeRunner,
    perspectives: list[str],
    perspective_template: str,
    current_draft: str,
    task_md: str,
    round_n: int,
    max_rounds: int,
) -> list[RunnerResult]:
    task_brief = "\n".join(task_md.splitlines()[:6])
    budget = _new_concerns_budget(round_n, max_rounds)
    tasks = []
    for spec in perspectives:
        sys_prompt = (
            perspective_template.replace("{perspective_name}", spec)
            .replace("{round_n}", str(round_n))
            .replace("{max_rounds}", str(max_rounds))
            .replace("{new_concerns_budget}", str(budget))
        )
        user_msg = (
            f"=== task brief ===\n{task_brief}\n\n"
            f"=== current draft ===\n{current_draft}\n\n"
            f"You are the **{spec}** perspective in round {round_n} of {max_rounds}. "
            f"Your new-concerns budget this round is {budget}. Critique from this "
            "angle only."
        )
        tasks.append(asyncio.to_thread(runner.run, sys_prompt, user_msg))
    return await asyncio.gather(*tasks)


def _append_debate(path: Path, header: str, body: str) -> None:
    existing = path.read_text() if path.exists() else ""
    path.write_text(f"{existing}\n## {header}\n\n{body}\n\n---\n")


def _parse_facilitator_verdict(text: str) -> dict:
    """Parse the JSON verdict block from facilitator output.

    Falls back to keyword detection if no JSON block is found.
    """
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict) and "converged" in v:
                return {
                    "converged": bool(v["converged"]),
                    "reason": str(v.get("reason", "")),
                }
        except json.JSONDecodeError:
            pass
    # Fallback heuristic — bias toward "not converged" so we get another round.
    if re.search(r'"converged"\s*:\s*true', text, re.IGNORECASE):
        return {"converged": True, "reason": "(parsed from inline text)"}
    return {"converged": False, "reason": "(could not parse facilitator verdict)"}
