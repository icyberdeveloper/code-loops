"""DebateCritiqueStage — RFC critic round + responder revision + facilitator verdict.

Flow:
  1. For each round (max_rounds, default 3):
     a. N critics in parallel review the current RFC.
        Each sees ONLY the RFC + a brief task summary — never each other,
        never the rfc-writer debate history. Hard isolation.
        They receive `round_n`, `max_rounds`, and `new_concerns_budget`
        (= max(1, max_rounds - round_n + 1)) so concerns narrow each round.
     b. Facilitator reads the full debate.md and emits a verdict
        (approved | needs_revision).
     c. If approved: write verdict.md (approved), final.md becomes the
        current RFC (potentially revised). Stop.
     d. If max rounds: write verdict.md (max_rounds_no_approval).
        Engine treats this as escalation — human review marker on the
        stage will surface it.
     e. Else: Responder (rfc_writer) reads critiques and produces a
        revised RFC. Critics review the revision next round.

Per-role model/effort overrides come from stage_def["roles"].
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from rich.console import Console

from ..runner import ClaudeRunner, RunnerFactory, RunnerResult
from .prompt import StageContext, load_agent_prompt
from .role_normalizer import normalize_roles

console = Console()


def _new_concerns_budget(round_n: int, max_rounds: int) -> int:
    """Linear narrowing budget for NEW concerns. Identical formula to debate_writer."""
    return max(1, max_rounds - round_n + 1)


class DebateCritiqueStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        max_rounds: int = stage_def.get("max_rounds", 3)
        roles = normalize_roles(stage_def["roles"])
        # critic role appears multiple times -> grouped as list by normalize_roles;
        # accept legacy `critics:` plural key too for backward compat.
        critics_cfg: list[dict] = roles.get("critic") or roles["critics"]
        if isinstance(critics_cfg, dict):
            critics_cfg = [critics_cfg]
        responder_cfg: dict = roles["responder"]
        facilitator_cfg: dict = roles["facilitator"]

        out_dir = ctx.task_dir / "design_review"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Per-pass scoping — when ArtifactWriter is present, scope all
        # per-round artifacts under design_review/pass_<N>/. Pass number
        # mirrors the redesign loop counter (post-redesign re-run = pass 2).
        if ctx.artifact_writer is not None:
            pass_n = ctx.artifact_writer.manifest.data.get("redesign_loop_count", 0) + 1
            work_dir = out_dir / f"pass_{pass_n}"
        else:
            pass_n = 1
            work_dir = out_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        debate_path = work_dir / "debate.md"
        debate_path.write_text("# Critique Debate\n")

        rfc_path = ctx.task_dir / "design" / "final.md"
        task_md = (ctx.task_dir / "task.md").read_text()
        current_rfc = rfc_path.read_text()
        rfc_version = 1  # the input design/final.md is "v1" for this stage

        critic_runners = [self.factory.make(c) for c in critics_cfg]
        critic_prompts = [load_agent_prompt(ctx.repo_root / c["prompt"], ctx) for c in critics_cfg]
        critic_names = [c["name"] for c in critics_cfg]
        responder_runner = self.factory.make(responder_cfg)
        responder_sys = load_agent_prompt(ctx.repo_root / responder_cfg["prompt"], ctx)
        facilitator_runner = self.factory.make(facilitator_cfg)
        facilitator_sys = load_agent_prompt(ctx.repo_root / facilitator_cfg["prompt"], ctx)

        cost_total = 0.0
        wall_start = time.monotonic()
        verdict_status: str = "unknown"
        verdict_reason: str = ""
        recurring_theme: str = ""
        design_guidance: str = ""

        # ---- Critique rounds ----
        for round_n in range(1, max_rounds + 1):
            budget = _new_concerns_budget(round_n, max_rounds)

            # 1. Critics in parallel
            console.print(
                f"  [dim]critique:[/dim] round {round_n}/{max_rounds} — "
                f"{len(critic_names)} critics reviewing (new-concerns budget={budget})"
            )
            critic_results = asyncio.run(
                _run_critics(
                    critic_runners=critic_runners,
                    critic_prompts=critic_prompts,
                    critic_names=critic_names,
                    current_rfc=current_rfc,
                    task_md=task_md,
                    round_n=round_n,
                    max_rounds=max_rounds,
                    redesign_signal=_read_redesign_signal(ctx.task_dir),
                )
            )
            for name, cr in zip(critic_names, critic_results, strict=True):
                cost_total += cr.cost_usd or 0
                # Per-round critic snapshot inside this pass
                (work_dir / f"{name}_v{round_n}.md").write_text(cr.text)
                # Stable "latest" pointer at pass-level (overwrites each round
                # within pass, NOT across passes since work_dir is pass-scoped)
                (work_dir / f"{name}.md").write_text(cr.text)
                _append_debate(debate_path, f"Round {round_n} — critic: {name}", cr.text)
            crit_cost = sum((cr.cost_usd or 0) for cr in critic_results)
            crit_max_dur = max((cr.duration_s for cr in critic_results), default=0)
            console.print(
                f"  [dim]critique:[/dim] ✓ critics ({crit_max_dur:.0f}s wall, ${crit_cost:.2f})"
            )

            # 2. Facilitator
            facilitator_msg = (
                f"=== current_rfc ===\n{current_rfc}\n\n"
                f"=== debate.md ===\n{debate_path.read_text()}\n"
            )
            fac_result = facilitator_runner.run(facilitator_sys, facilitator_msg)
            cost_total += fac_result.cost_usd or 0
            _append_debate(debate_path, f"Round {round_n} — facilitator", fac_result.text)
            verdict = _parse_facilitator_verdict(fac_result.text)
            verdict_status = verdict["status"]
            verdict_reason = verdict["reason"]
            recurring_theme = verdict.get("recurring_theme", "") or ""
            design_guidance = verdict.get("design_guidance", "") or ""
            console.print(
                f"  [dim]critique:[/dim] ✓ facilitator: {verdict_status} "
                f"({fac_result.duration_s:.0f}s, ${fac_result.cost_usd or 0:.2f})"
            )

            if verdict_status == "approved":
                break

            if verdict_status == "redesign_needed":
                # Short-circuit — no more responder revisions.
                # Engine will bubble back to rfc with redesign_signal.md.
                _append_debate(
                    debate_path,
                    "STOPPED — redesign_needed",
                    f"Facilitator detected recurring theme `{recurring_theme}` after "
                    f"round {round_n}. Engine will re-trigger rfc stage with "
                    "redesign context.",
                )
                console.print(
                    f"  [yellow]critique:[/yellow] redesign_needed (theme=`{recurring_theme}`)"
                )
                break

            if round_n == max_rounds:
                verdict_status = "needs_revision_max_rounds"
                _append_debate(
                    debate_path,
                    "STOPPED",
                    f"Max rounds ({max_rounds}) reached without approval. Verdict: {verdict_status}.",
                )
                console.print(
                    f"  [yellow]critique:[/yellow] max_rounds={max_rounds} reached without approval"
                )
                break

            # 3. Responder revises RFC (only if not last round)
            critic_block = "\n\n".join(
                f"=== critic: {name} ===\n{cr.text}"
                for name, cr in zip(critic_names, critic_results, strict=True)
            )
            revise_msg = (
                f"=== previous_rfc.md (v{rfc_version}) ===\n{current_rfc}\n\n"
                f"=== critic_responses (round {round_n}) ===\n{critic_block}\n\n"
                "These are critique-stage critics (not perspective agents). Their "
                "verdict suggestions (`safety: APPROVE/NEEDS_REVISION`, "
                "`elegance: APPROVE/NEEDS_REVISION`) at the bottom of each response "
                "indicate whether you need substantive revision.\n\n"
                "Read each critic response. Produce a fully revised RFC that "
                "addresses every substantive concern. Disagreements may be "
                "acknowledged and explained inline rather than capitulated to. "
                "End with "
                f"`## Critique-revision notes for round {round_n}` listing what "
                "changed by critic name."
            )
            revise_result = responder_runner.run(responder_sys, revise_msg)
            cost_total += revise_result.cost_usd or 0
            rfc_version += 1
            current_rfc = revise_result.text
            (work_dir / f"rfc_revision_v{rfc_version}.md").write_text(current_rfc)
            _append_debate(
                debate_path,
                f"Round {round_n} — Responder revision (rfc_v{rfc_version})",
                current_rfc,
            )
            console.print(
                f"  [dim]critique:[/dim] ✓ responder rfc_v{rfc_version} "
                f"({revise_result.duration_s:.0f}s, ${revise_result.cost_usd or 0:.2f})"
            )

        # ---- Finalize ----
        # If RFC was revised at any point, update design/final.md to the latest
        if rfc_version > 1:
            rfc_path.write_text(current_rfc)

        verdict_lines = [
            f"# Verdict: {verdict_status}",
            "",
            f"**Reason:** {verdict_reason}",
            "",
            f"**Rounds executed:** {min(round_n, max_rounds)}",
            f"**RFC revisions during critique:** {rfc_version - 1}",
        ]
        if verdict_status == "redesign_needed":
            verdict_lines += [
                "",
                f"**Recurring theme:** `{recurring_theme}`",
                "",
                "**Design guidance for next RFC attempt:**",
                "",
                design_guidance,
            ]
        verdict_md = "\n".join(verdict_lines) + "\n"
        # Per-pass copy + flat copy (downstream Release Manager reads flat).
        (work_dir / "verdict.md").write_text(verdict_md)
        verdict_path = out_dir / "verdict.md"
        verdict_path.write_text(verdict_md)
        # Record pass in manifest (forensic detail).
        if ctx.artifact_writer is not None:
            ctx.artifact_writer.manifest.record_pass(
                "design_review",
                pass_n,
                cost_usd=cost_total,
                duration_s=time.monotonic() - wall_start,
                rounds=min(round_n, max_rounds),
                verdict=verdict_status,
                theme=recurring_theme or None,
            )
            ctx.artifact_writer.manifest.set_latest("design_review", "design_review/verdict.md")

        # Write redesign_signal.md if redesign_needed — picked up by next rfc run
        if verdict_status == "redesign_needed":
            signal_path = ctx.task_dir / "design" / "redesign_signal.md"
            signal_path.write_text(
                "# Redesign signal\n\n"
                f"The previous RFC was rejected after critique because the same "
                f"underlying issue (theme: `{recurring_theme}`) kept producing new "
                f"variants of concerns across multiple rounds, indicating the "
                f"approach itself is wrong rather than the implementation.\n\n"
                f"## Recurring theme\n\n`{recurring_theme}`\n\n"
                f"## What was tried (and rejected)\n\n"
                f"See `design/previous_rfc.md` for the full prior attempt. "
                f"Critique reasoning:\n\n> {verdict_reason}\n\n"
                f"## Design guidance\n\n{design_guidance}\n\n"
                f"## Your task\n\n"
                f"Produce a fundamentally different shape of solution — do NOT "
                f"patch the variants of `{recurring_theme}` that the previous "
                f"approach exhibited. The new approach should make those "
                f"failure modes structurally impossible, not handled.\n"
            )
            # Snapshot the rejected RFC for the writer to reference
            (ctx.task_dir / "design" / "previous_rfc.md").write_text(current_rfc)

        wall_duration = time.monotonic() - wall_start
        outputs: dict[str, str] = {
            "design_review/verdict.md": verdict_path.read_text(),
            "design_review/debate.md": debate_path.read_text(),
        }
        return {
            "outputs": outputs,
            "cost_usd": cost_total,
            "duration_s": wall_duration,
            "verdict": verdict_status,
            "rfc_revisions": rfc_version - 1,
            "recurring_theme": recurring_theme,
            "design_guidance": design_guidance,
        }


def _read_redesign_signal(task_dir: Path) -> str | None:
    """Read design/redesign_signal.md if this is a post-redesign-loop critique pass.

    Presence of this file means the engine bubbled back from a prior
    design_review's `redesign_needed` verdict. Critics in this pass should
    know what theme triggered the redesign so they can verify the structural
    fix landed (rather than evaluating the new RFC from scratch with no
    memory of what was wrong before).
    """
    p = task_dir / "design" / "redesign_signal.md"
    return p.read_text() if p.exists() else None


async def _run_critics(
    *,
    critic_runners: list[ClaudeRunner],
    critic_prompts: list[str],
    critic_names: list[str],
    current_rfc: str,
    task_md: str,
    round_n: int,
    max_rounds: int,
    redesign_signal: str | None = None,
) -> list[RunnerResult]:
    task_brief = "\n".join(task_md.splitlines()[:6])
    budget = _new_concerns_budget(round_n, max_rounds)

    # Post-redesign-loop critics get the prior verdict's signal + guidance.
    # Otherwise (first design_review pass) this block is empty.
    redesign_block = ""
    if redesign_signal:
        redesign_block = (
            "=== prior_attempt_summary (this is a post-redesign-loop critique pass) ===\n"
            "The previous RFC was rejected by this review board. The architects "
            "rewrote it under the guidance below. Your job:\n"
            "  - Verify the structural fix actually landed (don't relitigate "
            "concerns the new RFC plausibly addressed).\n"
            "  - Apply your own fresh lens for new issues introduced by the "
            "restructure.\n"
            "  - If the recurring_theme below is still visible in the new RFC, "
            "that's a strong signal to escalate severity.\n\n"
            f"{redesign_signal}\n\n"
        )

    tasks = []
    for runner, prompt_text, name in zip(critic_runners, critic_prompts, critic_names, strict=True):
        sys_prompt = (
            prompt_text.replace("{round_n}", str(round_n))
            .replace("{max_rounds}", str(max_rounds))
            .replace("{new_concerns_budget}", str(budget))
        )
        user_msg = (
            f"=== task brief ===\n{task_brief}\n\n"
            f"{redesign_block}"
            f"=== current rfc ===\n{current_rfc}\n\n"
            f"You are the **{name}** critic in round {round_n} of {max_rounds}. "
            f"Your new-concerns budget this round is {budget}. Critique from your "
            "lane only and end with `{name}: APPROVE` or `{name}: NEEDS_REVISION`."
        )
        tasks.append(asyncio.to_thread(runner.run, sys_prompt, user_msg))
    return await asyncio.gather(*tasks)


def _append_debate(path: Path, header: str, body: str) -> None:
    existing = path.read_text() if path.exists() else ""
    path.write_text(f"{existing}\n## {header}\n\n{body}\n\n---\n")


def _parse_facilitator_verdict(text: str) -> dict:
    """Parse the JSON verdict block. Falls back to needs_revision if unparseable.

    Recognized verdict values: approved, redesign_needed, needs_revision.
    For redesign_needed, also extracts recurring_theme and design_guidance.
    """
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict) and "verdict" in v:
                status = str(v["verdict"]).strip().lower()
                if status not in {"approved", "needs_revision", "redesign_needed"}:
                    status = "needs_revision"
                out = {
                    "status": status,
                    "reason": str(v.get("reason", "")),
                }
                if status == "redesign_needed":
                    out["recurring_theme"] = str(v.get("recurring_theme", "")).strip()
                    out["design_guidance"] = str(v.get("design_guidance", "")).strip()
                return out
        except json.JSONDecodeError:
            pass
    if re.search(r'"verdict"\s*:\s*"approved"', text, re.IGNORECASE):
        return {"status": "approved", "reason": "(parsed from inline text)"}
    return {"status": "needs_revision", "reason": "(could not parse facilitator verdict)"}
