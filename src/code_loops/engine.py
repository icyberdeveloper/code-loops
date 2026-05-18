"""Engine: reads pipeline.yaml, runs stages sequentially.

For v0.1 only `prompt` type is registered. Other types come in later steps.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console

from .artifact_writer import ArtifactWriter
from .human_review import ReviewResult, review
from .manifest import Manifest
from .project_loader import load_project_config
from .runner import RunnerFactory
from .stages.action import ActionStage
from .stages.debate_critique import DebateCritiqueStage
from .stages.debate_writer import DebateWriterStage
from .stages.final_review import FinalReviewStage
from .stages.impl_planner import ImplPlannerStage
from .stages.parallel import ParallelStage
from .stages.prompt import PromptStage, StageContext
from .stages.subtask_iterator import SubtaskIteratorStage
from .stages.tech_writer import TechWriterStage

# Package data: pipeline.yaml + agents/ ship inside the wheel,
# resolved relative to this module's location. Works in both
# editable install (clone + uv sync) and pip install scenarios.
PACKAGE_DIR = Path(__file__).resolve().parent
console = Console()

# After this many redesign loops we stop trying — fall through forward with a
# marker rather than infinite-loop. Counter persists in manifest.json.
MAX_REDESIGN_LOOPS = 3

# After this many final-review correction loops we stop trying. Each loop appends
# corrective subtasks and re-runs subtask_iterator + final_validation + final_review.
MAX_FINAL_LOOPS = 5


def _stage_names(pipeline: dict) -> list[str]:
    return [s["name"] for s in pipeline.get("stages", [])]


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(
        self,
        task_dir: Path,
        project_config_path: Path | None = None,
        project_name: str | None = None,
        from_stage: str | None = None,
    ):
        self.task_dir = task_dir
        self.manifest = Manifest(task_dir / "manifest.json")
        # Engine is the sole writer of manifest.json — `code-loops new` already
        # called init_task. If we're here with an empty manifest, the task was
        # created out-of-band; default to the dir name + feature mode.
        if not self.manifest.data:
            self.manifest.init_task(task_dir.name, "feature")
        self.artifact_writer = ArtifactWriter(task_dir, self.manifest)
        self.pipeline = yaml.safe_load((PACKAGE_DIR / "pipeline.yaml").read_text())
        self._apply_defaults()
        if from_stage:
            self._reset_from_stage(from_stage)
        self.project_config = load_project_config(project_config_path, name=project_name)
        if self.project_config:
            project_name_log = (self.project_config.get("project") or {}).get("name", "?")
            console.print(f"[dim]project: {project_name_log} (config loaded)[/dim]")
        self.factory = RunnerFactory()
        self.handlers = {
            "prompt": PromptStage(self.factory),
            "parallel": ParallelStage(self.factory),
            "debate_writer": DebateWriterStage(self.factory),
            "debate_critique": DebateCritiqueStage(self.factory),
            "impl_planner": ImplPlannerStage(self.factory),
            "subtask_iterator": SubtaskIteratorStage(self.factory),
            "action": ActionStage(self.factory),
            "final_review": FinalReviewStage(self.factory),
            "tech_writer": TechWriterStage(self.factory),
        }

    def _apply_defaults(self) -> None:
        """Merge top-level `defaults:` block into every stage + every role.

        Stage/role values take precedence (setdefault, not overwrite).
        Currently used for `model` + `effort` so we don't repeat them in
        every stage.
        """
        defaults = self.pipeline.get("defaults") or {}
        if not defaults:
            return
        for stage in self.pipeline.get("stages", []):
            for k, v in defaults.items():
                stage.setdefault(k, v)
            roles = stage.get("roles")
            if isinstance(roles, list):
                for r in roles:
                    if isinstance(r, dict):
                        for k, v in defaults.items():
                            r.setdefault(k, v)
            elif isinstance(roles, dict):
                # legacy dict-of-dicts (still supported via normalize_roles)
                for r in roles.values():
                    if isinstance(r, dict):
                        for k, v in defaults.items():
                            r.setdefault(k, v)
                    elif isinstance(r, list):
                        for sub in r:
                            if isinstance(sub, dict):
                                for k, v in defaults.items():
                                    sub.setdefault(k, v)

    def _reset_from_stage(self, from_stage: str) -> None:
        """Mark all stages from `from_stage` onwards as not-done.

        Used to force-resume from a specific stage (e.g. after manual edit
        of design/final.md, run `code-loops run <task> --from-stage impl_plan`
        to re-run impl_plan and downstream without retracing prd/research/design).
        """
        names = _stage_names(self.pipeline)
        if from_stage not in names:
            raise EngineError(f"--from-stage {from_stage!r} not in pipeline. Valid stages: {names}")
        idx = names.index(from_stage)
        for name in names[idx:]:
            self.manifest.reset_stage(name)
        self.manifest.set_status("in_progress")
        console.print(
            f"[bold yellow]↻ resume from `{from_stage}` — "
            f"reset {len(names) - idx} downstream stage(s)[/bold yellow]"
        )

    def run(self) -> None:
        stages = self.pipeline["stages"]
        i = 0
        while i < len(stages):
            stage_def = stages[i]
            name = stage_def["name"]
            if self.manifest.is_stage_done(name):
                console.print(f"[dim]skip {name} (already done)[/dim]")
                i += 1
                continue

            result = self._run_stage(stage_def)

            # Detect final_review needs_more_work → append corrective subtasks
            # and rewind to subtask_iterator. Bounded by MAX_FINAL_LOOPS.
            if (
                stage_def["type"] == "final_review"
                and result is not None
                and result.get("verdict") == "needs_more_work"
            ):
                loop_n = self.manifest.increment_final_loop()
                corrective = result.get("corrective_subtasks", []) or []
                if loop_n > MAX_FINAL_LOOPS:
                    console.print(
                        f"[bold yellow]↯ final-review loops exhausted "
                        f"({loop_n}/{MAX_FINAL_LOOPS}); falling through with marker[/bold yellow]"
                    )
                    self.manifest.set_status("final_loops_exceeded")
                    i += 1
                    continue
                if not corrective:
                    console.print(
                        "[bold yellow]↯ final_review needs_more_work but no corrective_subtasks "
                        "proposed — falling through with marker[/bold yellow]"
                    )
                    self.manifest.set_status("final_review_blocked")
                    i += 1
                    continue
                # Append corrective subtasks to subtasks.yaml + reset downstream stages
                self._append_corrective_subtasks(corrective, loop_n)
                iter_idx = next(
                    (idx for idx, s in enumerate(stages) if s["name"] == "implementation"),
                    None,
                )
                if iter_idx is None:
                    console.print(
                        "[red]release_review needs_more_work but no implementation stage in "
                        "pipeline — falling through[/red]"
                    )
                    i += 1
                    continue
                console.print(
                    f"[bold yellow]↯ release-review loop {loop_n}/{MAX_FINAL_LOOPS} — "
                    f"appended {len(corrective)} corrective subtasks; "
                    "rewinding to implementation[/bold yellow]"
                )
                self.manifest.reset_stage("implementation")
                self.manifest.reset_stage("validation")
                self.manifest.reset_stage(name)
                i = iter_idx
                continue

            # Detect redesign_needed OR needs_revision_max_rounds verdict from
            # debate_critique → bubble back to rfc. Both signal "the current
            # design didn't get critic approval" and should be treated as a
            # redesign trigger, not a silent pass-through. debate_critique
            # synthesizes recurring_theme + design_guidance for both cases so
            # the downstream redesign_signal.md is well-formed.
            if (
                stage_def["type"] == "debate_critique"
                and result is not None
                and result.get("verdict") in ("redesign_needed", "needs_revision_max_rounds")
            ):
                verdict_kind = result["verdict"]
                loop_n = self.manifest.increment_redesign_loop()
                if loop_n > MAX_REDESIGN_LOOPS:
                    console.print(
                        f"[bold red]↯ redesign loops exhausted ({loop_n}/{MAX_REDESIGN_LOOPS}) "
                        f"(last verdict: {verdict_kind})[/bold red]\n"
                        f"[red]Aborting before downstream stages corrupt with bad design.[/red]\n"
                        f"[yellow]Options to resume:[/yellow]\n"
                        f"  1. Manually edit `{self.task_dir}/design/final.md` to fix the design,\n"
                        f"     then `code-loops run {self.task_dir.name} --from-stage impl_plan`\n"
                        f"  2. Bump MAX_REDESIGN_LOOPS in code_loops/engine.py and re-run\n"
                        f"  3. `code-loops cancel {self.task_dir.name}` to discard"
                    )
                    self.manifest.set_status("redesign_loops_exceeded")
                    raise EngineError(
                        f"redesign_loops_exceeded ({loop_n}/{MAX_REDESIGN_LOOPS}). "
                        f"See log above for resume options."
                    )

                # Find design stage index and rewind to it. Reset design + design_review state.
                rfc_idx = next(
                    (idx for idx, s in enumerate(stages) if s["name"] == "design"),
                    None,
                )
                if rfc_idx is None:
                    console.print(
                        f"[red]{verdict_kind} but no design stage in pipeline — falling through[/red]"
                    )
                    i += 1
                    continue
                console.print(
                    f"[bold yellow]↯ redesign loop {loop_n}/{MAX_REDESIGN_LOOPS} "
                    f"({verdict_kind}) — theme=`{result.get('recurring_theme', 'unknown')}` — "
                    "rewinding to design stage with redesign_signal.md[/bold yellow]"
                )
                # Pass scoping is now handled by debate_writer + debate_critique
                # writing directly under design/pass_<N>/ (Step 9.40 Phase 2c) —
                # no archive step needed. Just reset stage state for re-run.
                self.manifest.reset_stage("design")
                self.manifest.reset_stage(name)  # also reset design_review itself
                i = rfc_idx
                continue

            i += 1

        self.manifest.set_status("completed")
        console.print("[green]Pipeline complete.[/green]")

    def _run_stage(self, stage_def: dict, revision_inputs: list[Path] | None = None) -> dict | None:
        name = stage_def["name"]
        stype = stage_def["type"]
        handler = self.handlers.get(stype)
        if handler is None:
            raise EngineError(f"Unknown stage type: {stype} (stage {name})")

        console.print(f"[cyan]▶ {name}[/cyan]")
        self.manifest.stage_started(name)
        self.manifest.set_current_stage(name)
        ctx = StageContext(
            task_dir=self.task_dir,
            prompts_dir=PACKAGE_DIR / "prompts",
            repo_root=PACKAGE_DIR,
            revision_inputs=revision_inputs,
            project_config=self.project_config,
            artifact_writer=self.artifact_writer,
        )
        try:
            result = handler.run(stage_def, ctx)
        except Exception as exc:
            self.manifest.stage_failed(name, str(exc))
            console.print(f"[red]✗ {name} failed: {exc}[/red]")
            raise

        self.manifest.stage_completed(
            name,
            cost_usd=result.get("cost_usd") or 0,
            duration_s=result.get("duration_s") or 0,
        )
        cost_str = f"${result.get('cost_usd', 0):.4f}" if result.get("cost_usd") else "—"
        console.print(f"[green]✓ {name}[/green] ({result.get('duration_s', 0):.1f}s, {cost_str})")

        if stage_def.get("human_review"):
            self._handle_review(stage_def, result)

        return result

    def _handle_review(self, stage_def: dict, result: dict) -> None:
        name = stage_def["name"]
        outputs_paths = {rel: self.task_dir / rel for rel in result["outputs"]}
        summary = result.get("summary")
        rr: ReviewResult = review(name, outputs_paths, summary=summary)
        if rr.action == "approve":
            return
        if rr.action == "abort":
            self.manifest.set_status("cancelled")
            raise EngineError(f"User aborted at stage {name}")
        if rr.action == "revise":
            assert rr.comment is not None
            self._revise(stage_def, rr.comment)

    def _append_corrective_subtasks(self, corrective: list[dict], loop_n: int) -> None:
        """Append final_review's corrective subtasks to impl_plan/subtasks.yaml.

        Existing subtasks are preserved; new ones are tagged in their spec_md
        with the loop number for traceability.
        """
        import yaml as _yaml

        path = self.task_dir / "impl_plan" / "subtasks.yaml"
        data = _yaml.safe_load(path.read_text())
        existing = data.get("subtasks") or []
        existing_ids = {s["id"] for s in existing}
        appended = 0
        for st in corrective:
            if st["id"] in existing_ids:
                console.print(
                    f"[yellow]final-review proposed duplicate id `{st['id']}` — skipping[/yellow]"
                )
                continue
            tagged = dict(st)
            tagged["spec_md"] = (
                f"[Added by final_review loop {loop_n}]\n\n{tagged.get('spec_md', '')}"
            )
            existing.append(tagged)
            appended += 1
        data["subtasks"] = existing
        path.write_text(_yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        console.print(
            f"  [dim]engine:[/dim] appended {appended} corrective subtask(s) to subtasks.yaml"
        )

    def _revise(self, stage_def: dict, comment: str) -> None:
        first_output = stage_def["outputs"][0]
        revisions_dir = self.task_dir / Path(first_output).parent / "revisions"
        revisions_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(d for d in revisions_dir.iterdir() if d.is_dir())
        next_n = len(existing) + 1
        prev_dir = revisions_dir / f"v{next_n}"
        prev_dir.mkdir()

        revision_inputs: list[Path] = []
        for rel in stage_def["outputs"]:
            src = self.task_dir / rel
            if src.exists():
                snapshot = prev_dir / f"previous_{Path(rel).name}"
                snapshot.write_text(src.read_text())
                revision_inputs.append(snapshot)

        feedback_path = prev_dir / "feedback.md"
        feedback_path.write_text(comment)
        revision_inputs.append(feedback_path)

        console.print(
            f"[yellow]↻ Revising {stage_def['name']} with your feedback (v{next_n})[/yellow]"
        )
        self._run_stage(stage_def, revision_inputs=revision_inputs)
