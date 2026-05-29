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
import os
import re
import time
from pathlib import Path

import yaml
from rich.console import Console

from ..parallelism import gather_chunked
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
        # Concern-fingerprint history для plateau detector. Accumulates
        # {round_n, fingerprints, blockers} per round для KS-style
        # convergence check before facilitator emits verdict.
        rounds_history: list[dict] = []
        # Per-round plateau metrics trajectory для manifest accumulation.
        # Сериализуемая версия (no sets) — JSON-safe для manifest.json. Дает
        # post-hoc data чтобы тunить PLATEAU_DELTA_BLOCKERS_MAX / NEW_FP_RATIO_MIN
        # на real runs (currently thresholds subjective: 0 / 0.5).
        plateau_trajectory: list[dict] = []

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
                # Per-round critic snapshot inside this pass. Latest critic
                # output for a pass is just the highest-numbered _v<N>.md
                # (or read from manifest); no separate `{name}.md` mirror.
                (work_dir / f"{name}_v{round_n}.md").write_text(cr.text)
                _append_debate(debate_path, f"Round {round_n} — critic: {name}", cr.text)
            crit_cost = sum((cr.cost_usd or 0) for cr in critic_results)
            crit_max_dur = max((cr.duration_s for cr in critic_results), default=0)
            console.print(
                f"  [dim]critique:[/dim] ✓ critics ({crit_max_dur:.0f}s wall, ${crit_cost:.2f})"
            )

            # Parse structured YAML concerns из всех critics этого round'а.
            # Critics emit data-only (no verdict suggestions) — facilitator
            # решает на aggregated severity + confidence.
            round_aggregate = _aggregate_round_concerns(critic_names, critic_results)
            round_fps = round_aggregate["fingerprints"]
            round_blockers = round_aggregate["blocker_count"]
            if round_aggregate["parse_errors"]:
                for cname, err in round_aggregate["parse_errors"].items():
                    console.print(
                        f"  [red]critique:[/red] {cname} concerns parse error: {err}"
                    )
            rounds_history.append(
                {
                    "round_n": round_n,
                    "fingerprints": round_fps,
                    "blockers": round_blockers,
                }
            )

            # Plateau check — compute metrics, optionally inject signal в
            # facilitator msg чтобы он bias'нул toward approved_with_followups
            # вместо ещё одного round. Metrics также accumulated в manifest
            # plateau_metrics для post-hoc threshold tuning (see record_pass).
            plateau = _compute_plateau_metrics(rounds_history)
            plateau_trajectory.append(
                {
                    "round_n": round_n,
                    "blockers": round_blockers,
                    "fp_count": len(round_fps),
                    "delta_blockers": plateau["delta_blockers"],
                    "new_fp_ratio": (
                        round(plateau["new_fp_ratio"], 3)
                        if plateau["new_fp_ratio"] is not None
                        else None
                    ),
                    "is_plateau": plateau["is_plateau"],
                }
            )
            # Plateau signal injection gated env var CODE_LOOPS_PLATEAU_DETECTOR.
            # Default "1" (on) — preserves текущее behavior. Set к "0" для
            # ablation experiments: trajectory всё ещё accumulated в manifest,
            # но facilitator msg НЕ получает plateau_signal block — позволяет
            # измерить чистый contribution detector vs. baseline critic+facilitator.
            plateau_block = ""
            plateau_enabled = os.environ.get("CODE_LOOPS_PLATEAU_DETECTOR", "1") != "0"
            if plateau["is_plateau"] and plateau_enabled:
                plateau_block = (
                    "\n=== plateau_signal ===\n"
                    "PLATEAU DETECTED via cross-round fingerprint analysis:\n"
                    f"- delta_blockers (last 2 rounds): {plateau['delta_blockers']}\n"
                    f"- new_fp_ratio (current round): {plateau['new_fp_ratio']:.2f}\n"
                    f"- reason: {plateau['reason']}\n\n"
                    "This is the signal that critics are in quality-treadmill mode — "
                    "each round produces fresh non-blocker concerns rather than "
                    "converging on the same blockers. Per your 4-way verdict rubric, "
                    "STRONGLY BIAS toward `approved_with_followups`: ship the RFC and "
                    "track the current round's concerns as followups. Another round "
                    "of critique will likely produce yet another set of brand-new "
                    "non-blocker concerns — diminishing returns. Reserve "
                    "`needs_revision` only if you see a genuine BLOCKER this round.\n"
                )
                console.print(
                    f"  [yellow]critique:[/yellow] PLATEAU detected at round {round_n} "
                    f"(new_fp_ratio={plateau['new_fp_ratio']:.2f}); "
                    "facilitator biased toward approved_with_followups"
                )

            # AWF verdict toggle для ablation. Default ON (preserves текущее
            # 4-tier behavior). Set к "0" для test что Step 3+4 (critics rubric
            # + cross-family facilitator) делают одни без AWF safety valve.
            # Когда disabled — inject explicit note в facilitator msg чтобы он
            # не emit AWF; parser имеет safety net (downgrade к needs_revision
            # если facilitator всё равно emit AWF).
            awf_enabled = os.environ.get("CODE_LOOPS_AWF_VERDICT", "1") != "0"
            awf_block = ""
            if not awf_enabled:
                awf_block = (
                    "\n=== awf_disabled ===\n"
                    "approved_with_followups verdict is DISABLED для ablation. "
                    "Choose только из: `approved`, `needs_revision`, "
                    "`redesign_needed`. Если concerns ship-safe but tracked — "
                    "это `needs_revision` (architect должен address them inline), "
                    "не followup. Если all clear — `approved`.\n"
                )

            # 2. Facilitator — получает narrative debate + machine-readable
            # structured_concerns aggregate. Critics don't emit verdicts;
            # facilitator decides ship-readiness на aggregated severity.
            structured_concerns_block = _render_structured_concerns_block(round_aggregate)
            facilitator_msg = (
                f"=== current_rfc ===\n{current_rfc}\n\n"
                f"=== debate.md ===\n{debate_path.read_text()}\n\n"
                f"{structured_concerns_block}\n"
                f"{plateau_block}"
                f"{awf_block}"
            )
            fac_result = facilitator_runner.run(facilitator_sys, facilitator_msg)
            cost_total += fac_result.cost_usd or 0
            _append_debate(debate_path, f"Round {round_n} — facilitator", fac_result.text)
            verdict = _parse_facilitator_verdict(fac_result.text)
            # Engine-side gate policy. Triggers только когда:
            #   - is_plateau (cross-round fingerprint convergence) OR
            #   - is_last_pass (this pass is the last before redesign exhaust)
            # Без триггеров → arbiter authority preserved.
            # Когда triggered → policy решает final verdict from numbers,
            # arbiter verdict overridden (saved в debate.md для record).
            gate_policy = _load_gate_policy()
            is_last_pass = (
                ctx.pass_n is not None
                and ctx.max_redesign_loops is not None
                and ctx.pass_n >= ctx.max_redesign_loops
            )
            verdict, gate_reason = _apply_gate_policy(
                verdict,
                round_aggregate,
                gate_policy,
                is_plateau=plateau["is_plateau"],
                is_last_pass=is_last_pass,
            )
            if gate_reason:
                console.print(
                    f"  [yellow]critique:[/yellow] gate decision — {gate_reason}"
                )
                _append_debate(
                    debate_path,
                    f"Round {round_n} — gate decision",
                    gate_reason,
                )
            verdict_status = verdict["status"]
            verdict_reason = verdict["reason"]
            recurring_theme = verdict.get("recurring_theme", "") or ""
            design_guidance = verdict.get("design_guidance", "") or ""
            console.print(
                f"  [dim]critique:[/dim] ✓ facilitator: {verdict_status} "
                f"({fac_result.duration_s:.0f}s, ${fac_result.cost_usd or 0:.2f})"
            )

            if verdict_status in ("approved", "approved_with_followups"):
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
                # Synthesize theme + guidance so the engine can treat this
                # as a redesign signal (same downstream handling as
                # `redesign_needed`). Without this, the unresolved concerns
                # would be lost on the floor and downstream stages would
                # run against an unapproved RFC.
                recurring_theme = "no_approval_after_max_rounds"
                # Use structured concerns aggregate (новый YAML format)
                # вместо substring search. Critics emit data, не verdicts;
                # unresolved concerns identified через severity blockers + majors.
                unresolved_concerns = [
                    c
                    for c in round_aggregate["all_concerns"]
                    if c["severity"] in ("blocker", "major")
                ]
                if unresolved_concerns:
                    concerns_summary = "\n".join(
                        f"- [{c['severity']}/{c['category']}] {c['summary']} "
                        f"(confidence {c['confidence']:.2f}, in {c['affected_section']})"
                        for c in unresolved_concerns
                    )
                    design_guidance = (
                        f"Critics did not converge on approval after {max_rounds} "
                        f"rounds of critique + revision. Unresolved high-severity "
                        f"concerns from the final round:\n\n{concerns_summary}\n\n"
                        f"Treat these as binding signals for the next design pass."
                    )
                else:
                    design_guidance = (
                        f"Critics did not converge on approval after {max_rounds} "
                        f"rounds. No high-severity (blocker/major) concerns remain — "
                        f"facilitator declined approval based on aggregate signals; "
                        f"consult `design_review/pass_*/debate.md`."
                    )
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
                f"{structured_concerns_block}\n\n"
                "Critics emit structured concerns (YAML schema: id / severity / "
                "confidence / category / summary / affected_section / "
                "recommended_fix). Severity is one of: blocker | major | medium | "
                "minor. They do NOT emit ship verdicts — that's the facilitator's "
                "job.\n\n"
                "Address every concern с severity blocker AND major. For medium / "
                "minor — acknowledge inline or explain why you're deferring. "
                "Disagreements may be argued inline rather than capitulated to. "
                "End with "
                f"`## Critique-revision notes for round {round_n}` listing what "
                "changed per concern id (e.g. `safety-C1: addressed by ...`)."
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
        followups_items: list[dict] = verdict.get("followups", []) if verdict_status == "approved_with_followups" else []
        if verdict_status == "approved_with_followups":
            verdict_lines += [
                "",
                f"**Followups tracked:** {len(followups_items)} (see followups.md)",
            ]
        verdict_md = "\n".join(verdict_lines) + "\n"
        # Per-pass copy + flat copy (downstream Release Manager reads flat).
        (work_dir / "verdict.md").write_text(verdict_md)
        verdict_path = out_dir / "verdict.md"
        verdict_path.write_text(verdict_md)
        # Record pass in manifest (forensic detail). plateau_metrics +
        # followups_count помогают tune subjective thresholds на real runs
        # (см. record_pass docstring + PLATEAU_* constants).
        if ctx.artifact_writer is not None:
            ctx.artifact_writer.manifest.record_pass(
                "design_review",
                pass_n,
                cost_usd=cost_total,
                duration_s=time.monotonic() - wall_start,
                rounds=min(round_n, max_rounds),
                verdict=verdict_status,
                theme=recurring_theme or None,
                plateau_metrics={"rounds": plateau_trajectory} if plateau_trajectory else None,
                followups_count=(
                    len(followups_items) if verdict_status == "approved_with_followups" else None
                ),
            )
            ctx.artifact_writer.manifest.set_latest("design_review", "design_review/verdict.md")

        # Write followups.md when arbiter shipped с tracked followups.
        # impl_planner reads this and bakes it into the implementation plan
        # so non-blocking concerns don't get lost on the floor.
        if verdict_status == "approved_with_followups" and followups_items:
            followups_md = _render_followups_md(
                pass_n=pass_n,
                reason=verdict_reason,
                followups=followups_items,
            )
            # Per-pass copy для forensics + flat copy для impl_planner.
            (work_dir / "followups.md").write_text(followups_md)
            (out_dir / "followups.md").write_text(followups_md)

        # Write redesign_signal.md if redesign_needed OR max_rounds-without-approval
        # — both are bubbled back to the design stage by engine.py.
        if verdict_status in ("redesign_needed", "needs_revision_max_rounds"):
            signal_path = ctx.task_dir / "design" / "redesign_signal.md"
            # Cross-pass theme detector — детектит когда recurring_theme
            # повторяется across passes (не только within одного pass debate
            # как facilitator). При обнаружении trigger'ит meta-cognitive
            # reformulation block. См. research/Echo Trap, MAR Confirmation
            # Bias — patches на одном attractor дают diminishing returns,
            # только reformulation breaks the loop.
            prior_verdicts = _load_prior_pass_verdicts(ctx.task_dir, pass_n)
            is_recurring, matching_passes, recurrence_count = _is_theme_recurring(
                recurring_theme, prior_verdicts
            )
            meta_reformulation_block = ""
            if is_recurring and recurring_theme:
                meta_reformulation_block = _build_meta_reformulation_block(
                    recurring_theme, matching_passes, recurrence_count, pass_n
                )
            # Constraint shared by both verdict modes: force layer relocation,
            # not patching. Architect bias documented in run #3 — without this
            # constraint, the architect keeps the same shape and just patches
            # the most-recent bypass surface, regenerating the rejection.
            shape_shift_constraint = (
                "## Mandatory shape shift (binding constraint)\n\n"
                "Your next RFC's `## Shapes considered` block (Phase 1) MUST "
                "pick a DIFFERENT Axis-1 layer than the prior pass. The prior "
                "pass's chosen layer is documented in `design/previous_rfc.md` "
                "under its Phase 1 block.\n\n"
                "**Axis-1 layers** (see software-architect.md Phase 1):\n"
                "- A. At symptom location (patch existing code where bug originates)\n"
                "- B. Upstream of symptom (intercept before broken code receives input)\n"
                "- C. Downstream of symptom (transform output before user sees it)\n"
                "- D. Outside the pipeline (separate filter/transform on pipeline output)\n"
                "- E. Data-level (fix input distribution so broken path doesn't trip)\n\n"
                "If the prior pass picked A → your new pass must pick B, C, D, or E. "
                "Producing another A-shape RFC (even with different internals) will "
                "re-trigger the same critic rejection — the recurring theme "
                "documents that the *layer* is the problem, not the implementation "
                "within it. Justify your new Axis-1 choice in 2+ sentences in "
                "Phase 1 prose with explicit reference to why the prior layer "
                "produced the bypass-surface pattern.\n\n"
            )
            if verdict_status == "redesign_needed":
                signal_body = (
                    f"The previous RFC was rejected after critique because the same "
                    f"underlying issue (theme: `{recurring_theme}`) kept producing new "
                    f"variants of concerns across multiple rounds, indicating the "
                    f"approach itself is wrong rather than the implementation.\n\n"
                    f"## Recurring theme\n\n`{recurring_theme}`\n\n"
                    f"## What was tried (and rejected)\n\n"
                    f"See `design/previous_rfc.md` for the full prior attempt. "
                    f"Critique reasoning:\n\n> {verdict_reason}\n\n"
                    f"## Design guidance\n\n{design_guidance}\n\n"
                    f"{meta_reformulation_block}"
                    f"{shape_shift_constraint}"
                    f"## Your task\n\n"
                    f"Produce a fundamentally different shape of solution — do NOT "
                    f"patch the variants of `{recurring_theme}` that the previous "
                    f"approach exhibited. The new approach should make those "
                    f"failure modes structurally impossible, not handled. The "
                    f"shape shift above is binding.\n"
                )
            else:  # needs_revision_max_rounds
                signal_body = (
                    f"Critics did not converge on approval after {max_rounds} rounds "
                    f"of critique + revision. The RFC was iterated through "
                    f"{rfc_version - 1} responder revisions but still attracted "
                    f"unresolved BLOCKER / NEEDS_REVISION verdicts in the final round.\n\n"
                    f"## Signal\n\n`{recurring_theme}`\n\n"
                    f"## What was tried (and capped)\n\n"
                    f"See `design/previous_rfc.md` for the final RFC state. "
                    f"Facilitator reasoning (last round):\n\n> {verdict_reason}\n\n"
                    f"## Unresolved concerns from the final round\n\n{design_guidance}\n\n"
                    f"{meta_reformulation_block}"
                    f"{shape_shift_constraint}"
                    f"## Your task\n\n"
                    f"The iterative-revision path was exhausted without convergence. "
                    f"Step back and produce a fundamentally different shape — do NOT "
                    f"keep tweaking the same RFC's edges. Pick a different approach "
                    f"that closes the unresolved concerns structurally. The shape "
                    f"shift above is binding.\n"
                )
            signal_path.write_text("# Redesign signal\n\n" + signal_body)
            # Cross-pass history для architect — видит summary всех prior
            # attempts (theme + chosen shape + outcome), не только last
            # signal. Это даёт ему context для detection pattern recurrence
            # на уровне "я уже 3 раза менял layer, но pattern recurs".
            _write_redesign_history(ctx.task_dir, pass_n, prior_verdicts)
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


def _extract_chosen_shape_from_pass(task_dir: Path, pass_n: int) -> str | None:
    """Извлекает chosen shape из RFC pass'а.

    Приоритет: JSON (draft_v*.json или final.json) — structured, надёжно.
    Fallback: markdown final.md → regex по `Cross-axis chosen shape: X.`.
    Если ничего не найдено — None.
    """
    pass_dir = task_dir / "design" / f"pass_{pass_n}"
    if not pass_dir.exists():
        return None
    # Пробуем последний JSON (highest version number) — самый свежий
    json_files = sorted(
        pass_dir.glob("draft_v*.json"),
        key=lambda p: (
            int(re.search(r"draft_v(\d+)\.json", p.name).group(1))
            if re.search(r"draft_v(\d+)\.json", p.name)
            else 0
        ),
        reverse=True,
    )
    for jf in json_files:
        try:
            data = json.loads(jf.read_text())
            chosen = data.get("shapes_considered", {}).get("chosen")
            if chosen:
                return chosen
        except (json.JSONDecodeError, OSError):
            continue
    # Fallback на markdown final.md
    md_path = pass_dir / "final.md"
    if md_path.exists():
        try:
            text = md_path.read_text()
            m = re.search(r"Cross-axis chosen shape:\s*([^\n.]+)", text)
            if m:
                return m.group(1).strip()
        except OSError:
            pass
    return None


def _write_redesign_history(
    task_dir: Path, current_pass_n: int, prior_verdicts: list[dict]
) -> None:
    """Создаёт design/redesign_history.md — summary всех prior passes.

    Architect видит full history (theme + chosen shape + outcome для
    каждого pass'а), не only last redesign_signal. Это даёт ему context
    для cross-pass pattern detection — "я уже 3 раза менял layer, pattern
    recurs".

    Согласно Meta-Policy Reflexion (arxiv 2509.03990): "structured memory
    переживающая episode'ы — решает limitation Reflexion где reflections
    производят ephemeral task-specific traces не reused across tasks".
    Здесь tasks → passes within одного task.

    pass_1 first redesign signal — prior_verdicts пуст, файл не пишется.
    """
    if not prior_verdicts:
        return
    history_path = task_dir / "design" / "redesign_history.md"
    lines: list[str] = [
        "# Prior redesign attempts (cross-pass history)",
        "",
        "Read BEFORE Phase 1. This is the cumulative history of all prior",
        "redesign attempts for this RFC. If same theme recurred across passes,",
        "shape-shift alone (Axis-1 letter change) was insufficient — you need",
        "to reformulate the framing itself (see redesign_signal.md Axis-3 options).",
        "",
    ]
    themes_seen: list[str] = []
    for v in prior_verdicts:
        pn = v["pass_n"]
        theme = v.get("recurring_theme")
        status = v.get("verdict_status", "unknown")
        chosen = _extract_chosen_shape_from_pass(task_dir, pn)
        lines.append(f"## Pass {pn}")
        lines.append(f"- **Chosen shape**: `{chosen or '?'}`")
        lines.append(f"- **Verdict**: `{status}`")
        if theme:
            recurrence_marker = " (RECURRENCE)" if theme in themes_seen else ""
            lines.append(f"- **Recurring theme**: `{theme}`{recurrence_marker}")
            themes_seen.append(theme)
        lines.append("")
    # Pattern summary в конце
    from collections import Counter

    theme_counts = Counter(t for t in themes_seen if t)
    most_common = theme_counts.most_common(1)
    if most_common and most_common[0][1] >= 2:
        theme_name, theme_count = most_common[0]
        lines.append("## Pattern detected")
        lines.append("")
        lines.append(
            f"Theme `{theme_name}` recurred {theme_count} times across "
            f"{len(prior_verdicts)} prior passes. Despite Axis-1 layer shifts "
            "(mandated by shape-shift constraint), the same concern class "
            "kept re-emerging. **Signal**: the issue is in framing, not in "
            "layer choice. Reformulate per Axis-3 options in redesign_signal.md "
            "instead of trying yet another Axis-1 layer."
        )
        lines.append("")
    history_path.write_text("\n".join(lines))


def _load_prior_pass_verdicts(task_dir: Path, current_pass_n: int) -> list[dict]:
    """Загружает verdict.md из всех prior passes design_review.

    Возвращает список {pass_n, recurring_theme, verdict_status} для каждого
    пройденного pass'а до current_pass_n. Используется cross-pass theme
    detector чтобы детектить когда recurring_theme повторяется между
    разными passes (не только within одного pass debate как делает
    facilitator).

    Когда verdict отсутствует или невозможно распарсить — pass молча
    пропускается (graceful — legacy tasks могут не иметь structured
    verdicts).
    """
    if current_pass_n <= 1:
        return []
    out: list[dict] = []
    for pn in range(1, current_pass_n):
        verdict_path = task_dir / "design_review" / f"pass_{pn}" / "verdict.md"
        if not verdict_path.exists():
            continue
        try:
            text = verdict_path.read_text()
        except OSError:
            continue
        # Парсим Recurring theme строку — format: "**Recurring theme:** `X`"
        theme = None
        m = re.search(r"\*\*Recurring theme:\*\*\s*`([^`]+)`", text)
        if m:
            theme = m.group(1).strip()
        verdict_status = None
        m2 = re.search(r"^#\s*Verdict:\s*(\S+)", text, re.MULTILINE)
        if m2:
            verdict_status = m2.group(1).strip()
        out.append({"pass_n": pn, "recurring_theme": theme, "verdict_status": verdict_status})
    return out


# Plateau detector thresholds. Это pragmatic proxy inspired by the
# convergence-detection idea в KS-stability test (arxiv 2510.12697 NeurIPS
# 2025) — НЕ actual KS distribution test. Real KS measures distribution
# distance между sample populations; мы вместо этого используем simpler
# set arithmetic (Jaccard-style new-fingerprint ratio) + blocker count
# flatness, потому что у нас N=2-3 rounds per pass, недостаточно для
# meaningful distribution sampling.
#
# Тhresholds emperically chosen, НЕ derived из research:
#   - PLATEAU_DELTA_BLOCKERS_MAX=0 conservative: trigger plateau только
#     когда ZERO blockers stable (иначе real work остаётся).
#   - PLATEAU_NEW_FP_RATIO_MIN=0.5 aggressive: ≥50% brand new concerns =
#     quality treadmill. Может false-positive если critics legitimately
#     find 50% real concerns каждый round — log metrics в manifest для
#     tuning на real runs.
PLATEAU_DELTA_BLOCKERS_MAX = 0  # blocker count must be flat (zero blockers)
PLATEAU_NEW_FP_RATIO_MIN = 0.5  # >50% concerns brand new = quality treadmill
PLATEAU_MIN_ROUNDS = 2  # need 2+ rounds to compute meaningful trend

# Structured concerns parser. Critics emit YAML block внутри ## Concerns section
# (см. architect-critic-*.md prompts). Schema per concern: id, severity,
# confidence, category, summary, affected_section, recommended_fix.
#
# Critics НЕ emit verdict suggestions (APPROVE/NEEDS_REVISION) — это data-only
# format. Facilitator (arbiter) decides ship-readiness основываясь на
# aggregated severity + confidence через all critics.

REQUIRED_CONCERN_FIELDS = (
    "id",
    "severity",
    "confidence",
    "category",
    "summary",
    "affected_section",
    "recommended_fix",
)
VALID_SEVERITIES = frozenset({"blocker", "major", "medium", "minor"})

# YAML block внутри ## Concerns section. Critic prompts use ```yaml fence.
_CONCERNS_SECTION_RE = re.compile(
    r"##\s+Concerns?\b(.+?)(?=\n##|\Z)", re.DOTALL | re.IGNORECASE
)
_YAML_FENCE_RE = re.compile(r"```yaml\s*\n(.+?)\n```", re.DOTALL)


class ConcernParseError(ValueError):
    """Raised when critic output lacks valid YAML concerns block или schema invalid."""


def _parse_critic_concerns(critic_text: str) -> list[dict]:
    """Извлекает structured concerns из critic output.

    Expected format:
        ## Concerns
        ```yaml
        - id: safety-C1
          severity: blocker
          confidence: 0.9
          category: data_loss
          summary: "..."
          affected_section: "..."
          recommended_fix: "..."
        ```

    Empty list (`[]`) допустим — нулевые concerns valid case (late rounds).

    Raises ConcernParseError если:
      - ## Concerns section отсутствует
      - YAML block отсутствует / malformed
      - Required field missing
      - severity not в VALID_SEVERITIES
      - confidence not float в [0.0, 1.0]
    """
    concerns_match = _CONCERNS_SECTION_RE.search(critic_text)
    if not concerns_match:
        raise ConcernParseError("missing ## Concerns section")
    section_body = concerns_match.group(1)
    yaml_match = _YAML_FENCE_RE.search(section_body)
    if not yaml_match:
        raise ConcernParseError("missing ```yaml fence inside ## Concerns section")
    try:
        raw = yaml.safe_load(yaml_match.group(1))
    except yaml.YAMLError as exc:
        raise ConcernParseError(f"YAML parse error: {exc}") from exc
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list):
        raise ConcernParseError(f"expected YAML list, got {type(raw).__name__}")
    validated: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ConcernParseError(f"concerns[{i}] not a mapping")
        missing = [f for f in REQUIRED_CONCERN_FIELDS if f not in item]
        if missing:
            raise ConcernParseError(f"concerns[{i}] missing fields: {missing}")
        sev = str(item["severity"]).strip().lower()
        if sev not in VALID_SEVERITIES:
            raise ConcernParseError(
                f"concerns[{i}] severity={sev!r} not in {sorted(VALID_SEVERITIES)}"
            )
        conf = item["confidence"]
        if not isinstance(conf, int | float) or isinstance(conf, bool) or not (0.0 <= conf <= 1.0):
            raise ConcernParseError(f"concerns[{i}] confidence={conf!r} not float in [0.0, 1.0]")
        validated.append(
            {
                "id": str(item["id"]).strip(),
                "severity": sev,
                "confidence": float(conf),
                "category": str(item["category"]).strip(),
                "summary": str(item["summary"]).strip(),
                "affected_section": str(item["affected_section"]).strip(),
                "recommended_fix": str(item["recommended_fix"]).strip(),
            }
        )
    return validated


def _concern_fingerprint(concern: dict) -> str:
    """Normalized fingerprint of concern's summary для plateau tracking.

    Lowercase + strip non-alphanumeric + first 80 chars. Robust к minor
    phrasing variations critic'а across rounds.
    """
    normalized = re.sub(r"[^a-z0-9]+", " ", concern["summary"].lower()).strip()[:80]
    return normalized


def _aggregate_round_concerns(
    critic_names: list[str], critic_results: list[RunnerResult]
) -> dict:
    """Парсит concerns из всех critics одного round'а и aggregates.

    Returns:
        {
          "concerns_by_critic": {critic_name: [concern_dict, ...]},
          "all_concerns": [concern_dict, ...] (flat across all critics),
          "blocker_count": int (severity=blocker всех critics),
          "major_count": int,
          "medium_count": int,
          "minor_count": int,
          "fingerprints": set[str] (для plateau detector),
          "parse_errors": {critic_name: error_msg} (if any),
        }

    Parse errors не raise — critic с broken YAML просто skipped с error log.
    Facilitator увидит structured_concerns aggregate + raw debate.md для context.
    """
    concerns_by_critic: dict[str, list[dict]] = {}
    all_concerns: list[dict] = []
    parse_errors: dict[str, str] = {}
    for name, cr in zip(critic_names, critic_results, strict=True):
        try:
            parsed = _parse_critic_concerns(cr.text)
        except ConcernParseError as exc:
            parse_errors[name] = str(exc)
            concerns_by_critic[name] = []
            continue
        concerns_by_critic[name] = parsed
        all_concerns.extend(parsed)
    sev_counts = {sev: 0 for sev in VALID_SEVERITIES}
    fingerprints: set[str] = set()
    for c in all_concerns:
        sev_counts[c["severity"]] += 1
        fp = _concern_fingerprint(c)
        if fp:
            fingerprints.add(fp)
    return {
        "concerns_by_critic": concerns_by_critic,
        "all_concerns": all_concerns,
        "blocker_count": sev_counts["blocker"],
        "major_count": sev_counts["major"],
        "medium_count": sev_counts["medium"],
        "minor_count": sev_counts["minor"],
        "fingerprints": fingerprints,
        "parse_errors": parse_errors,
    }


_GATE_POLICY_PATH = Path(__file__).resolve().parent.parent / "gate.yaml"


def _load_gate_policy(path: Path = _GATE_POLICY_PATH) -> dict:
    """Loads release policy из gate.yaml.

    Schema: единый `severities:` block, per-severity `min_confidence` +
    `ceiling`. Engine applies этот policy только когда gate triggered
    (plateau OR last_pass) — без этих conditions arbiter имеет full authority.

    Raises if missing или malformed — gate critical для correctness.
    """
    if not path.exists():
        raise FileNotFoundError(f"gate.yaml not found at {path}")
    data = yaml.safe_load(path.read_text())
    if "severities" not in data:
        raise ValueError("gate.yaml missing required 'severities' block")
    for sev in ("blocker", "major", "medium", "minor"):
        if sev not in data["severities"]:
            raise ValueError(f"gate.yaml severities missing '{sev}'")
        rules = data["severities"][sev]
        if "min_confidence" not in rules or "ceiling" not in rules:
            raise ValueError(
                f"gate.yaml severities.{sev} missing min_confidence или ceiling"
            )
    return data


def _count_filtered_concerns(concerns: list[dict], policy: dict) -> dict:
    """Aggregates concerns per severity bucket с per-severity confidence filter.

    Каждая severity имеет свой min_confidence threshold (см. gate.yaml).
    Concerns ниже threshold для своей severity → ignored.

    Returns:
        {
          "blocker": int,    # filtered count
          "major": int,
          "medium": int,
          "minor": int,
          "ignored_per_severity": {blocker: int, major: int, ...}  # noise floor
        }
    """
    counts = {"blocker": 0, "major": 0, "medium": 0, "minor": 0}
    ignored = {"blocker": 0, "major": 0, "medium": 0, "minor": 0}
    severities = policy["severities"]
    for c in concerns:
        sev = c["severity"]
        if sev not in counts:
            continue  # unknown severity (parser should have caught — defensive)
        min_conf = severities[sev]["min_confidence"]
        if c["confidence"] < min_conf:
            ignored[sev] += 1
            continue
        counts[sev] += 1
    counts["ignored_per_severity"] = ignored
    return counts


def _apply_gate_policy(
    arbiter_verdict: dict,
    aggregate: dict,
    policy: dict,
    is_plateau: bool,
    is_last_pass: bool,
) -> tuple[dict, str | None]:
    """Engine-side decision: gate решает финальный verdict ТОЛЬКО когда
    triggered. Без триггеров — arbiter имеет full authority, gate passes
    arbiter verdict through.

    Triggers:
      - is_plateau: cross-round fingerprint convergence — arbiter не выходит
        из treadmill, нужен deterministic decision
      - is_last_pass: current pass == MAX_REDESIGN_LOOPS — next redesign
        бы caused catastrophic exit pipeline'а, нужен safety net

    Когда gate triggered, decision основан на numbers vs ceilings:
      numbers within все ceilings → final = approved (ship)
      numbers exceeded хотя бы один → final = needs_revision (one more round)

    Arbiter's verdict (incl redesign_needed) overridden — записан в
    debate.md/verdict.md для post-hoc analysis но не enforced.

    Returns (final_verdict_dict, gate_reason_or_None).
    gate_reason explains решение если gate был active.
    """
    if not (is_plateau or is_last_pass):
        return arbiter_verdict, None

    counts = _count_filtered_concerns(aggregate["all_concerns"], policy)
    severities = policy["severities"]
    violations: list[str] = []
    for sev in ("blocker", "major", "medium", "minor"):
        ceiling = severities[sev]["ceiling"]
        if ceiling < 0:  # -1 = unlimited
            continue
        actual = counts[sev]
        if actual > ceiling:
            violations.append(f"{sev}: {actual} > ceiling {ceiling}")

    trigger_label = ", ".join(
        label for label, active in (("plateau", is_plateau), ("last_pass", is_last_pass)) if active
    )
    ignored_summary = ", ".join(
        f"{sev}: {n}" for sev, n in counts["ignored_per_severity"].items() if n > 0
    ) or "none"
    arbiter_status = arbiter_verdict["status"]

    if not violations:
        # Numbers within all ceilings → ship
        gate_reason = (
            f"gate activated ({trigger_label}); numbers within ceilings → ship "
            f"(arbiter said '{arbiter_status}', overridden; "
            f"ignored low-confidence: {ignored_summary})"
        )
        return (
            {
                "status": "approved",
                "reason": f"[gate-decided: {gate_reason}] arbiter_reason: {arbiter_verdict['reason']}",
            },
            gate_reason,
        )

    # Numbers exceeded → keep revising
    gate_reason = (
        f"gate activated ({trigger_label}); ceiling violations: {', '.join(violations)} "
        f"(arbiter said '{arbiter_status}', overridden; ignored: {ignored_summary})"
    )
    return (
        {
            "status": "needs_revision",
            "reason": f"[gate-decided: {gate_reason}] arbiter_reason: {arbiter_verdict['reason']}",
        },
        gate_reason,
    )


def _render_structured_concerns_block(aggregate: dict) -> str:
    """Сериализует aggregated concerns как YAML block для inject в facilitator msg."""
    lines = ["=== structured_concerns ==="]
    if aggregate["parse_errors"]:
        lines.append("# Parse errors (critic skipped):")
        for name, err in aggregate["parse_errors"].items():
            lines.append(f"#   {name}: {err}")
        lines.append("")
    lines.append(
        f"# Aggregate: blockers={aggregate['blocker_count']}, "
        f"majors={aggregate['major_count']}, "
        f"mediums={aggregate['medium_count']}, "
        f"minors={aggregate['minor_count']}"
    )
    lines.append("")
    for name, concerns in aggregate["concerns_by_critic"].items():
        if not concerns:
            lines.append(f"# {name}: no concerns")
            continue
        lines.append(f"# {name}:")
        lines.append(yaml.safe_dump(concerns, sort_keys=False, allow_unicode=True))
    return "\n".join(lines)


def _compute_plateau_metrics(rounds_history: list[dict]) -> dict:
    """Считает plateau signal across rounds.

    rounds_history: list of {round_n, fingerprints: set[str], blockers: int}
    в порядке возрастания round_n. Минимум PLATEAU_MIN_ROUNDS rounds для
    meaningful trend.

    Возвращает {is_plateau, delta_blockers, new_fp_ratio, reason}.
    Plateau = blockers стабильно zero AND >50% concerns brand new
    (значит critics в режиме quality treadmill — каждый round новые мелкие
    вопросы вместо real BLOCKERs).
    """
    if len(rounds_history) < PLATEAU_MIN_ROUNDS:
        return {
            "is_plateau": False,
            "delta_blockers": None,
            "new_fp_ratio": None,
            "reason": f"need ≥{PLATEAU_MIN_ROUNDS} rounds, have {len(rounds_history)}",
        }
    last = rounds_history[-1]
    prev = rounds_history[-2]
    delta_blockers = abs(last["blockers"] - prev["blockers"])
    # All prior round fingerprints (для расчёта "new" в текущем round)
    prior_fps: set[str] = set()
    for r in rounds_history[:-1]:
        prior_fps |= r["fingerprints"]
    new_fps = last["fingerprints"] - prior_fps
    new_fp_ratio = (len(new_fps) / len(last["fingerprints"])) if last["fingerprints"] else 0.0
    is_plateau = (
        last["blockers"] == 0
        and prev["blockers"] == 0
        and delta_blockers <= PLATEAU_DELTA_BLOCKERS_MAX
        and new_fp_ratio >= PLATEAU_NEW_FP_RATIO_MIN
    )
    if is_plateau:
        reason = (
            f"BLOCKERS stable at 0 for {PLATEAU_MIN_ROUNDS}+ rounds AND "
            f"{int(new_fp_ratio * 100)}% concerns in current round are brand new "
            f"(threshold ≥{int(PLATEAU_NEW_FP_RATIO_MIN * 100)}%). Critics find "
            "fresh non-blocker concerns each round — quality treadmill pattern."
        )
    else:
        reason = (
            f"not plateau: blockers prev={prev['blockers']} last={last['blockers']}, "
            f"new_fp_ratio={new_fp_ratio:.2f}"
        )
    return {
        "is_plateau": is_plateau,
        "delta_blockers": delta_blockers,
        "new_fp_ratio": new_fp_ratio,
        "reason": reason,
    }


def _normalize_theme_tokens(theme: str | None) -> set[str]:
    """Разбивает тему на токены для fuzzy match.

    Пример: `canonical_typo_partition_completeness` → {canonical, typo, partition, completeness}.
    Используется для детектирования что `partition_completeness` и
    `canonical_typo_partition_completeness` — это варианты одной и той же
    темы (architect фабрикует чуть разные имена для одного и того же class).
    """
    if not theme:
        return set()
    # split по _ и пробелам, убрать общие "filler" слова
    raw = re.split(r"[_\s]+", theme.lower())
    return {t for t in raw if t and len(t) >= 4}


def _is_theme_recurring(
    current_theme: str | None, prior_verdicts: list[dict]
) -> tuple[bool, list[int], int]:
    """Детектит повторение темы across passes.

    Возвращает (is_recurring, list_of_prior_pass_ns, recurrence_count).

    Recurring если:
      a) точное совпадение имени с любой из 2 последних предыдущих тем, ИЛИ
      b) fuzzy match через token overlap ≥ 2 общих токенов длины ≥ 4.

    Fuzzy match нужен потому что architect-debate-arbiter может слегка
    варьировать имя темы между passes — `partition_completeness` vs
    `canonical_typo_partition_completeness` — это вариации одной проблемы,
    но точный string match их пропустит.
    """
    if not current_theme or not prior_verdicts:
        return False, [], 0
    current_tokens = _normalize_theme_tokens(current_theme)
    matching_passes: list[int] = []
    for v in prior_verdicts:
        prior_theme = v.get("recurring_theme")
        if not prior_theme:
            continue
        # exact match
        if prior_theme == current_theme:
            matching_passes.append(v["pass_n"])
            continue
        # fuzzy: token overlap ≥ 2 значимых tokens
        prior_tokens = _normalize_theme_tokens(prior_theme)
        if len(current_tokens & prior_tokens) >= 2:
            matching_passes.append(v["pass_n"])
    recurrence_count = len(matching_passes) + 1  # +1 для текущего pass
    return len(matching_passes) > 0, matching_passes, recurrence_count


def _build_meta_reformulation_block(
    recurring_theme: str, matching_passes: list[int], recurrence_count: int, current_pass_n: int
) -> str:
    """Структурный escalation message когда theme recurring across passes.

    Согласно research (RAGEN Echo Trap, MAR Confirmation Bias, MPR
    cross-episode memory): когда тот же class concern recurring в 2+
    iterations, signal должен force REFORMULATION, не очередной patch.

    Soft instructions ("think critically", "try different approach") по
    research имеют 48% disagreement rate vs 99.2% для explicit "you MUST
    oppose framing" (OpenReview mxBmj5LYU2). Поэтому формулировка hard:
    "MUST step back, MUST enumerate alternative framings, MUST justify
    why new framing makes concern class structurally impossible".
    """
    passes_str = ", ".join(f"pass_{p}" for p in matching_passes)
    return (
        "## CRITICAL: meta-cognitive reformulation required\n\n"
        f"Theme `{recurring_theme}` (or its variants) RECURRED across passes "
        f"{passes_str} and now again in pass_{current_pass_n} — "
        f"total recurrence count: {recurrence_count}.\n\n"
        "This is **NOT** a signal to patch yet another variant. This is a "
        "signal that your **framing of the problem is wrong**. Each prior "
        "pass shifted to a different Axis-1 layer (per shape-shift "
        "constraint), but the same concern class kept re-emerging because "
        "the underlying framing was preserved across layer shifts.\n\n"
        "**MANDATORY next steps** (binding — critics will check):\n\n"
        "1. **STEP BACK** before drafting Phase 1. Write a "
        "`## Step-back reframing` section in your RFC answering:\n"
        f"   - What assumption in prior framing produces the `{recurring_theme}` "
        "concern class?\n"
        "   - 5 whys: WHY does this CLASS keep recurring (not why this "
        "specific instance)?\n"
        "   - Enumerate 3 ALTERNATIVE framings; for each, would the "
        "recurring concern class still emerge?\n\n"
        "2. **CHOOSE Axis-3 framing** in your Phase 1 `## Shapes considered` "
        "block (in addition to Axis-1 / Axis-2). Axis-3 options:\n"
        "   - **T1**. Binary partition (canonical vs typo) — DEFAULT, "
        "leaves edge cases. **FORBIDDEN** when meta_reformulation required.\n"
        "   - **T2**. N-way with explicit unknown tier + fail-closed — "
        "open-world classification with reject option (classical CS pattern, "
        "see Open World Assumption literature).\n"
        "   - **T3**. Continuous score (no hard threshold) — confidence-"
        "weighted decisions.\n"
        "   - **T4**. Inverted problem — instead of detecting wrongness, "
        "project onto known canonical set (closed-form vs open-form).\n"
        "   - **T5**. Reformulate problem space — current framing wrong "
        "at problem-statement level, not solution-shape level.\n\n"
        "3. **JUSTIFY structural impossibility** — explain in 2-3 sentences "
        f"why your chosen Axis-3 framing makes the `{recurring_theme}` "
        "concern class STRUCTURALLY IMPOSSIBLE (not patched in a new "
        "location). If you cannot articulate structural impossibility, your "
        "reformulation is insufficient.\n\n"
        "Research backing: RAGEN Echo Trap, MAR Confirmation Bias, "
        "InvThink behavioral attractors. Patches on the same attractor "
        "produce diminishing returns — only reformulation breaks the loop.\n\n"
        "**Meta-reformulation flag**: meta_reformulation_required = true\n\n"
    )


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
            "lane only. Emit structured YAML concerns per your output format."
        )
        tasks.append(asyncio.to_thread(runner.run, sys_prompt, user_msg))
    # Chunked to bound peak memory — see parallelism.gather_chunked.
    return await gather_chunked(tasks)


def _append_debate(path: Path, header: str, body: str) -> None:
    existing = path.read_text() if path.exists() else ""
    path.write_text(f"{existing}\n## {header}\n\n{body}\n\n---\n")


def _render_followups_md(pass_n: int, reason: str, followups: list[dict]) -> str:
    """Render approve-with-followups artifact. impl_planner reads this и
    включает items в implementation plan как tracked NEXT-RFC work — they
    don't block ship of current RFC but must surface in subtask plan.

    Per-category grouping → human reviewer и tech-lead видят which lens
    flagged what (safety follow-up vs elegance follow-up have different
    triage criteria).
    """
    lines = [
        f"# Followups — design_review pass {pass_n}",
        "",
        "**Verdict:** approved_with_followups",
        f"**Reason:** {reason}",
        "",
        f"This RFC shipped despite {len(followups)} non-blocking concern(s) raised "
        "by the review board (FOLLOWUP severity per Ship-readiness rubric). The "
        "concerns are real but bounded — addressable in a separate small RFC "
        "without rework of the current implementation. impl_planner should "
        "surface them as tracked work in the subtask plan.",
        "",
    ]
    by_category: dict[str, list[dict]] = {}
    for f in followups:
        by_category.setdefault(f.get("category") or "uncategorized", []).append(f)
    for category in sorted(by_category):
        lines.append(f"## {category}")
        lines.append("")
        for i, item in enumerate(by_category[category], 1):
            section = item.get("rfc_section") or "(unspecified)"
            severity = item.get("severity") or "FOLLOWUP"
            summary = item.get("summary") or "(empty summary)"
            lines.append(f"{i}. **[{severity}]** {summary}")
            lines.append(f"   - RFC section: {section}")
        lines.append("")
    return "\n".join(lines) + "\n"


VALID_VERDICTS = {
    "approved",
    "approved_with_followups",
    "needs_revision",
    "redesign_needed",
}

# Upper bound на количество followups items в one verdict. Если facilitator
# emits больше — truncate + лог. Защита от degenerate case где LLM в quality-
# treadmill режиме переmark'ал ВСЕ concerns как FOLLOWUP (overwhelm tech-lead
# user_msg, превышение subtask budget). 10 — reasonable upper для design RFC:
# больше = либо need_revision (real BLOCKERS) либо split RFC.
MAX_FOLLOWUPS_PER_VERDICT = 10


def _parse_facilitator_verdict(text: str) -> dict:
    """Parse the JSON verdict block. Falls back to needs_revision if unparseable.

    Recognized verdict values: approved, approved_with_followups,
    redesign_needed, needs_revision.
    - For redesign_needed: extracts recurring_theme + design_guidance.
    - For approved_with_followups: extracts followups[] (each item has
      category, summary, rfc_section, severity). Engine writes them into
      followups.md and impl_planner reads.
    """
    awf_enabled = os.environ.get("CODE_LOOPS_AWF_VERDICT", "1") != "0"
    m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
    if m:
        try:
            v = json.loads(m.group(1))
            if isinstance(v, dict) and "verdict" in v:
                status = str(v["verdict"]).strip().lower()
                if status not in VALID_VERDICTS:
                    status = "needs_revision"
                # Safety net: facilitator emit AWF несмотря на disable note —
                # downgrade к needs_revision, force architect через revision.
                if status == "approved_with_followups" and not awf_enabled:
                    status = "needs_revision"
                    return {
                        "status": "needs_revision",
                        "reason": (
                            f"{v.get('reason', '')} [downgraded: AWF verdict "
                            "emitted but CODE_LOOPS_AWF_VERDICT=0 — architect "
                            "must address concerns inline, не defer к followups]"
                        ),
                    }
                out = {
                    "status": status,
                    "reason": str(v.get("reason", "")),
                }
                if status == "redesign_needed":
                    out["recurring_theme"] = str(v.get("recurring_theme", "")).strip()
                    out["design_guidance"] = str(v.get("design_guidance", "")).strip()
                elif status == "approved_with_followups":
                    raw_followups = v.get("followups", [])
                    cleaned: list[dict] = []
                    if isinstance(raw_followups, list):
                        for f in raw_followups:
                            if isinstance(f, dict):
                                cleaned.append(
                                    {
                                        "category": str(f.get("category", "")).strip().lower(),
                                        "summary": str(f.get("summary", "")).strip(),
                                        "rfc_section": str(f.get("rfc_section", "")).strip(),
                                        "severity": str(f.get("severity", "FOLLOWUP"))
                                        .strip()
                                        .upper(),
                                    }
                                )
                    # Empty followups list → downgrade to needs_revision (LLM
                    # claimed approved_with_followups but provided no items —
                    # ambiguous; safer to re-run round than to silently ship).
                    if not cleaned:
                        out["status"] = "needs_revision"
                        out["reason"] = (
                            f"{out['reason']} [downgraded: approved_with_followups verdict "
                            f"emitted with empty followups[]]"
                        )
                    else:
                        # Cap для protection от degenerate cases (LLM в quality
                        # treadmill режиме переmark'ал ВСЕ concerns как FOLLOWUP).
                        # Если truncate'нем — append marker в reason чтобы forensic
                        # trail сохранил факт truncation.
                        if len(cleaned) > MAX_FOLLOWUPS_PER_VERDICT:
                            dropped = len(cleaned) - MAX_FOLLOWUPS_PER_VERDICT
                            cleaned = cleaned[:MAX_FOLLOWUPS_PER_VERDICT]
                            out["reason"] = (
                                f"{out['reason']} [truncated: {dropped} of "
                                f"{dropped + MAX_FOLLOWUPS_PER_VERDICT} followups "
                                f"dropped per MAX_FOLLOWUPS_PER_VERDICT={MAX_FOLLOWUPS_PER_VERDICT} cap]"
                            )
                        out["followups"] = cleaned
                return out
        except json.JSONDecodeError:
            pass
    if re.search(r'"verdict"\s*:\s*"approved"', text, re.IGNORECASE):
        return {"status": "approved", "reason": "(parsed from inline text)"}
    return {"status": "needs_revision", "reason": "(could not parse facilitator verdict)"}
