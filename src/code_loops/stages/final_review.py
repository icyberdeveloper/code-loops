"""FinalReviewStage — single LLM review of full diff vs RFC.

After implementation + validation finish, this stage:
1. Calls a single LLM with the RFC + full diff + validation result.
2. Parses verdict (approved | needs_more_work).
3. If needs_more_work: also parses `corrective_subtasks` (same schema as
   impl_plan/subtasks.yaml entries) which the engine will append to the
   plan and re-run the iterator with.

For v0.1 this is a single-shot LLM call (not multi-critic debate). If
quality is insufficient in practice, upgrade to a debate_critique-shape
stage with coverage + quality critics.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import yaml
from rich.console import Console

from ..runner import RunnerFactory
from .impl_planner import _validate_subtasks  # reuse subtask schema validator
from .prompt import StageContext, load_agent_prompt

console = Console()


class FinalReviewError(RuntimeError):
    pass


class FinalReviewStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        wall_start = time.monotonic()
        out_dir = ctx.task_dir / "release_review"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Assemble inputs
        rfc = _read_optional(ctx.task_dir / "design" / "final.md")
        prd = _read_optional(ctx.task_dir / "prd" / "prd.md")
        diff = _read_optional(ctx.task_dir / "implementation" / "_full_diff.patch")
        validation_yaml = _read_optional(ctx.task_dir / "validation" / "result.yaml")
        coverage_md = _read_optional(ctx.task_dir / "validation" / "coverage.md")

        sys_prompt = load_agent_prompt(ctx.repo_root / stage_def["prompt"], ctx)
        user_msg = (
            f"=== design/final.md ===\n{rfc}\n\n"
            f"=== prd/prd.md ===\n{prd}\n\n"
            f"=== implementation/_full_diff.patch ===\n{diff}\n\n"
            f"=== validation/result.yaml ===\n{validation_yaml}\n\n"
            f"=== validation/coverage.md ===\n{coverage_md}\n"
        )

        runner = self.factory.make(stage_def)
        result = runner.run(sys_prompt, user_msg)

        (out_dir / "review.md").write_text(result.text)
        verdict_data = _parse_verdict(result.text)

        # If needs_more_work, persist proposed corrective subtasks for visibility
        if verdict_data["verdict"] == "needs_more_work":
            (out_dir / "corrective_subtasks.yaml").write_text(
                yaml.safe_dump(
                    {"corrective_subtasks": verdict_data.get("corrective_subtasks", [])},
                    sort_keys=False,
                    allow_unicode=True,
                )
            )

        # Write a short verdict.md for human readers
        verdict_lines = [
            f"# Final review verdict: {verdict_data['verdict']}",
            "",
            f"**Reason:** {verdict_data.get('reason', '')}",
        ]
        if verdict_data["verdict"] == "needs_more_work":
            cs = verdict_data.get("corrective_subtasks", [])
            verdict_lines += [
                "",
                f"**Corrective subtasks proposed:** {len(cs)}",
            ]
            for st in cs:
                verdict_lines.append(f"- `{st.get('id', '?')}`: {st.get('title', '')}")
        (out_dir / "verdict.md").write_text("\n".join(verdict_lines) + "\n")

        wall_duration = time.monotonic() - wall_start
        console.print(
            f"  [dim]release_review:[/dim] verdict={verdict_data['verdict']} "
            f"({wall_duration:.0f}s, ${result.cost_usd or 0:.2f})"
        )

        return {
            "outputs": {
                "release_review/review.md": result.text,
                "release_review/verdict.md": (out_dir / "verdict.md").read_text(),
            },
            "cost_usd": result.cost_usd or 0,
            "duration_s": wall_duration,
            "verdict": verdict_data["verdict"],
            "reason": verdict_data.get("reason", ""),
            "corrective_subtasks": verdict_data.get("corrective_subtasks", []),
        }


def _read_optional(path: Path) -> str:
    return path.read_text() if path.exists() else f"(missing: {path.name})"


def _parse_verdict(text: str) -> dict:
    """Extract the JSON verdict block. Returns dict with at least 'verdict' key.

    Schema:
      verdict: "approved" | "needs_more_work"
      reason: str
      corrective_subtasks: list[dict] (only when needs_more_work)
    """
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if not m:
        return {
            "verdict": "needs_more_work",
            "reason": "(could not parse final_review JSON verdict)",
            "corrective_subtasks": [],
        }
    raw = m.group(1).strip()
    try:
        v = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "verdict": "needs_more_work",
            "reason": f"(JSON parse error: {e})",
            "corrective_subtasks": [],
        }
    if not isinstance(v, dict) or "verdict" not in v:
        return {
            "verdict": "needs_more_work",
            "reason": "(verdict block missing required 'verdict' key)",
            "corrective_subtasks": [],
        }
    verdict = str(v["verdict"]).strip().lower()
    if verdict not in {"approved", "needs_more_work"}:
        verdict = "needs_more_work"
    out = {
        "verdict": verdict,
        "reason": str(v.get("reason", "")),
        "corrective_subtasks": [],
    }
    if verdict == "needs_more_work":
        cs_raw = v.get("corrective_subtasks", []) or []
        if not isinstance(cs_raw, list):
            cs_raw = []
        # Validate structure same way as impl_planner
        if cs_raw:
            try:
                _validate_subtasks({"subtasks": cs_raw})
                out["corrective_subtasks"] = cs_raw
            except Exception as e:
                out["reason"] = f"{out['reason']} | corrective_subtasks failed schema: {e}"
                out["corrective_subtasks"] = []
    return out
