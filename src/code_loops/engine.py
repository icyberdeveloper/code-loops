"""Engine: reads pipeline.yaml, runs stages sequentially.

For v0.1 only `prompt` type is registered. Other types come in later steps.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console

from .human_review import ReviewResult, review
from .meta import MetaStore
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
# marker rather than infinite-loop. Counter persists in meta.yaml.
MAX_REDESIGN_LOOPS = 1

# After this many final-review correction loops we stop trying. Each loop appends
# corrective subtasks and re-runs subtask_iterator + final_validation + final_review.
MAX_FINAL_LOOPS = 5


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(
        self,
        task_dir: Path,
        project_config_path: Path | None = None,
        project_name: str | None = None,
    ):
        self.task_dir = task_dir
        self.meta = MetaStore(task_dir / "meta.yaml")
        self.pipeline = yaml.safe_load((PACKAGE_DIR / "pipeline.yaml").read_text())
        self._apply_defaults()
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

    def run(self) -> None:
        stages = self.pipeline["stages"]
        i = 0
        while i < len(stages):
            stage_def = stages[i]
            name = stage_def["name"]
            if self.meta.is_stage_done(name):
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
                loop_n = self.meta.increment_final_loop()
                corrective = result.get("corrective_subtasks", []) or []
                if loop_n > MAX_FINAL_LOOPS:
                    console.print(
                        f"[bold yellow]↯ final-review loops exhausted "
                        f"({loop_n}/{MAX_FINAL_LOOPS}); falling through with marker[/bold yellow]"
                    )
                    self.meta.set_status("final_loops_exceeded")
                    i += 1
                    continue
                if not corrective:
                    console.print(
                        "[bold yellow]↯ final_review needs_more_work but no corrective_subtasks "
                        "proposed — falling through with marker[/bold yellow]"
                    )
                    self.meta.set_status("final_review_blocked")
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
                self.meta.reset_stage("implementation")
                self.meta.reset_stage("validation")
                self.meta.reset_stage(name)
                i = iter_idx
                continue

            # Detect redesign_needed verdict from debate_critique → bubble back to rfc.
            if (
                stage_def["type"] == "debate_critique"
                and result is not None
                and result.get("verdict") == "redesign_needed"
            ):
                loop_n = self.meta.increment_redesign_loop()
                if loop_n > MAX_REDESIGN_LOOPS:
                    console.print(
                        f"[bold yellow]↯ redesign loops exhausted ({loop_n}/{MAX_REDESIGN_LOOPS}); "
                        "falling through with marker[/bold yellow]"
                    )
                    self.meta.set_status("redesign_loops_exceeded")
                    # Continue forward — downstream stages run on the latest RFC
                    i += 1
                    continue

                # Find design stage index and rewind to it. Reset design + design_review state.
                rfc_idx = next(
                    (idx for idx, s in enumerate(stages) if s["name"] == "design"),
                    None,
                )
                if rfc_idx is None:
                    console.print(
                        "[red]redesign_needed but no design stage in pipeline — falling through[/red]"
                    )
                    i += 1
                    continue
                console.print(
                    f"[bold yellow]↯ redesign loop {loop_n}/{MAX_REDESIGN_LOOPS} — "
                    f"theme=`{result.get('recurring_theme', 'unknown')}` — "
                    "rewinding to design stage with redesign_signal.md[/bold yellow]"
                )
                self.meta.reset_stage("design")
                self.meta.reset_stage(name)  # also reset design_review itself
                i = rfc_idx
                continue

            i += 1

        self.meta.set_status("completed")
        console.print("[green]Pipeline complete.[/green]")

    def _run_stage(self, stage_def: dict, revision_inputs: list[Path] | None = None) -> dict | None:
        name = stage_def["name"]
        stype = stage_def["type"]
        handler = self.handlers.get(stype)
        if handler is None:
            raise EngineError(f"Unknown stage type: {stype} (stage {name})")

        console.print(f"[cyan]▶ {name}[/cyan]")
        self.meta.stage_started(name)
        ctx = StageContext(
            task_dir=self.task_dir,
            prompts_dir=PACKAGE_DIR / "prompts",
            repo_root=PACKAGE_DIR,
            revision_inputs=revision_inputs,
            project_config=self.project_config,
        )
        try:
            result = handler.run(stage_def, ctx)
        except Exception as exc:
            self.meta.stage_failed(name, str(exc))
            console.print(f"[red]✗ {name} failed: {exc}[/red]")
            raise

        self.meta.stage_completed(
            name,
            cost_usd=result.get("cost_usd"),
            duration_s=result.get("duration_s", 0),
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
            self.meta.set_status("cancelled")
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
