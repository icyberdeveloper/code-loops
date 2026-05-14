"""Prompt stage handler — single LLM call producing one or more artifacts.

For v0.1, the entire LLM text response is written verbatim to the first declared
output. Multi-artifact stages (like impl_plan that produces both plan.md and
subtasks.yaml) will be added in Step 7 when needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..project_loader import inject_project_brief
from ..runner import RunnerFactory


@dataclass
class StageContext:
    task_dir: Path
    prompts_dir: Path
    repo_root: Path
    revision_inputs: list[Path] | None = None
    # Target-project configuration loaded from project.yaml — see
    # project_loader.py. None when running without a project profile (some
    # stages will then fall back to legacy stage_def values; subtask_iterator
    # raises if base_repo is unresolvable).
    project_config: dict | None = None
    # ArtifactWriter for scoped artifact writes + manifest sync. Stage
    # handlers refactored in Step 9.40 use this instead of raw path.write_text
    # for outputs that benefit from per-pass / per-attempt / per-round scoping.
    # None for legacy callers / tests; new code paths should set it.
    artifact_writer: object | None = None


def load_agent_prompt(path: Path, ctx: StageContext) -> str:
    """Read an agent's system prompt and inject {PROJECT_BRIEF} placeholder.

    All stage handlers should use this instead of `path.read_text()` so that
    agents authored with `{PROJECT_BRIEF}` get the target-project's brief.md
    substituted in. No-op for prompts that don't include the placeholder.
    """
    return inject_project_brief(path.read_text(), ctx.project_config)


class PromptStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        system_prompt = self._load_system_prompt(stage_def, ctx)
        user_message = self._build_user_message(stage_def, ctx)
        runner = self.factory.make(stage_def)
        result = runner.run(system_prompt, user_message)

        outputs: dict[str, str] = {}
        for rel in stage_def["outputs"]:
            target = ctx.task_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(result.text)
            outputs[rel] = result.text
            break  # v0.1: single-artifact only

        return {
            "outputs": outputs,
            "cost_usd": result.cost_usd,
            "duration_s": result.duration_s,
            "in_tokens": result.in_tokens,
            "out_tokens": result.out_tokens,
        }

    def _load_system_prompt(self, stage_def: dict, ctx: StageContext) -> str:
        prompt_path = stage_def["prompt"]
        # paths in pipeline.yaml are relative to repo root (e.g. "agents/strategy/business-analyst.md")
        full = ctx.repo_root / prompt_path
        if not full.exists():
            raise FileNotFoundError(f"Prompt file not found: {full}")
        return load_agent_prompt(full, ctx)

    def _build_user_message(self, stage_def: dict, ctx: StageContext) -> str:
        parts: list[str] = []
        for rel in stage_def.get("inputs", []):
            inp = ctx.task_dir / rel
            if not inp.exists():
                raise FileNotFoundError(f"Stage input missing: {inp}")
            parts.append(f"=== {rel} ===\n{inp.read_text()}")
        if ctx.revision_inputs:
            parts.append("=== REVISION MODE ===")
            parts.append(
                "Your previous attempt and the user's feedback are below. "
                "Address every concrete point in the feedback. Produce a fully revised "
                "output (not a diff). Add a `## Revision notes` section at the end "
                "explaining what changed."
            )
            for rev in ctx.revision_inputs:
                parts.append(f"=== {rev.name} ===\n{rev.read_text()}")
        return "\n\n".join(parts)
