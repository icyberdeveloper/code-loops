"""Parallel stage handler — runs N branches concurrently via asyncio.to_thread.

Each branch is its own claude --print subprocess. Cost and duration are
aggregated: cost = sum, duration = max (parallel wall-clock).

Inputs are curated per branch via `isolation.slice_questions_for_spec` so each
researcher only sees its own slice of the plan's research questions.
"""

from __future__ import annotations

import asyncio

from ..isolation import slice_questions_for_spec
from ..runner import RunnerFactory, RunnerResult
from .prompt import StageContext, load_agent_prompt


class ParallelStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        from .role_normalizer import normalize_branches

        branches = normalize_branches(stage_def["branches"])
        task_md = (ctx.task_dir / "task.md").read_text()
        plan_md = (ctx.task_dir / "research_plan" / "plan.md").read_text()

        results: list[RunnerResult] = asyncio.run(
            self._run_branches(branches, task_md, plan_md, ctx)
        )

        outputs: dict[str, str] = {}
        total_cost = 0.0
        max_duration = 0.0
        for branch_def, result in zip(branches, results, strict=True):
            for rel in branch_def["outputs"]:
                target = ctx.task_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(result.text)
                outputs[rel] = result.text
                break  # v0.1: single artifact per branch
            total_cost += result.cost_usd or 0
            if result.duration_s > max_duration:
                max_duration = result.duration_s

        return {
            "outputs": outputs,
            "cost_usd": total_cost,
            "duration_s": max_duration,
        }

    async def _run_branches(
        self,
        branches: list[dict],
        task_md: str,
        plan_md: str,
        ctx: StageContext,
    ) -> list[RunnerResult]:
        tasks = []
        for branch_def in branches:
            spec = branch_def["name"]
            sliced = slice_questions_for_spec(plan_md, spec)
            user_message = (
                f"=== task.md ===\n{task_md}\n\n"
                f"=== Your research questions ([{spec}] only) ===\n{sliced}\n"
            )
            prompt_path = ctx.repo_root / branch_def["prompt"]
            sys_prompt = load_agent_prompt(prompt_path, ctx)
            runner = self.factory.make(branch_def)
            tasks.append(asyncio.to_thread(runner.run, sys_prompt, user_message))
        return await asyncio.gather(*tasks)
