"""regression_check action — Stage 8.5.

Runs the target project's evaluation benchmark after Stage 8 validation
passes (pytest/ruff/file-coverage clean) and BEFORE Stage 9 release
review. Compares results against the saved baseline. Blocks the release
(emits `regression.md` that Release Manager reads) if any metric
regressed more than `threshold_pct`.

First run with no saved baseline: captures current results as the new
baseline, passes, logs informational message.

Skip-paths (no subprocess call, no cost):
  - regression.enabled is False (default)
  - regression.command is None / empty
  - regression.output_path is None / empty
  - no worktree at task_dir/worktree/wt (subtask_iterator didn't run)

Baseline location: `projects/<name>/baselines/eval.json` — sibling of
brief.md, per-project state. User can `git ignore` it or commit it
depending on their workflow (commit for reproducible CI; ignore for
local-only baseline).

Contract with the user's bench:
  - Bench writes a JSON file at `output_path` (relative to worktree).
  - JSON shape: `{ "<metric_name>": <number>, ... }`. Numbers are
    higher-is-better. Each metric independently checked.
  - Example: `{"recall_at_10": 0.92, "faithfulness": 0.88, "mrr": 0.71}`.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from ..project_loader import PROJECTS_DIR, get_regression_config
from .prompt import StageContext

console = Console()


def run_regression_check(stage_def: dict, ctx: StageContext) -> dict:
    cfg = get_regression_config(ctx.project_config)

    if ctx.artifact_writer is not None:
        ctx.artifact_writer.manifest.set_latest("regression_check", "validation/regression.md")

    if not cfg["enabled"]:
        console.print("  [dim]regression_check:[/dim] disabled in project config → skip")
        return _skipped("regression.enabled=false")

    if not cfg["command"]:
        console.print("  [dim]regression_check:[/dim] no bench command → skip")
        return _skipped("regression.command not set")

    if not cfg["output_path"]:
        console.print("  [dim]regression_check:[/dim] no output_path → skip")
        return _skipped("regression.output_path not set")

    wt_path = ctx.task_dir / "worktree" / "wt"
    if not wt_path.exists():
        console.print(f"  [yellow]regression_check: no worktree at {wt_path} → skip[/yellow]")
        return _skipped("worktree absent")

    project_name = (ctx.project_config or {}).get("project", {}).get("name", "unknown")
    baseline_dir = PROJECTS_DIR / project_name / "baselines"
    baseline_path = baseline_dir / "eval.json"

    # 1. Run the bench command in the worktree
    cmd = cfg["command"].split()
    console.print(f"  [cyan]regression_check:[/cyan] running `{cfg['command']}`")
    wall_start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(wt_path),
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        wall_duration = time.monotonic() - wall_start
        msg = "bench command timed out (600s)"
        _write_failure_report(ctx.task_dir, msg, current=None, baseline=None)
        return _failed(msg, wall_duration)
    wall_duration = time.monotonic() - wall_start

    if proc.returncode != 0:
        msg = f"bench command failed (exit {proc.returncode})"
        _write_failure_report(
            ctx.task_dir,
            msg,
            current=None,
            baseline=None,
            stderr_tail=proc.stderr[-2000:] if proc.stderr else "",
        )
        return _failed(msg, wall_duration)

    # 2. Read bench output JSON
    output_path = wt_path / cfg["output_path"]
    if not output_path.exists():
        msg = f"bench output not found at {output_path}"
        _write_failure_report(ctx.task_dir, msg, current=None, baseline=None)
        return _failed(msg, wall_duration)
    try:
        current = json.loads(output_path.read_text())
    except json.JSONDecodeError as e:
        msg = f"bench output is not valid JSON: {e}"
        _write_failure_report(ctx.task_dir, msg, current=None, baseline=None)
        return _failed(msg, wall_duration)
    if not isinstance(current, dict) or not current:
        msg = f"bench output JSON must be a non-empty dict, got {type(current).__name__}"
        _write_failure_report(ctx.task_dir, msg, current=current, baseline=None)
        return _failed(msg, wall_duration)

    # 3. Compare vs baseline (or capture if first run)
    if not baseline_path.exists():
        baseline_dir.mkdir(parents=True, exist_ok=True)
        _write_baseline(baseline_path, current, ctx)
        console.print(
            f"  [green]✓[/green] no prior baseline — captured current results "
            f"as new baseline at {baseline_path}"
        )
        _write_report(
            ctx.task_dir,
            status="baseline_captured",
            current=current,
            baseline=None,
            regressed=[],
            threshold_pct=cfg["threshold_pct"],
        )
        return {
            "skipped": False,
            "success": True,
            "regressed_metrics": [],
            "current": current,
            "baseline_captured": True,
            "cost_usd": 0.0,
            "duration_s": wall_duration,
            "summary": f"baseline captured ({len(current)} metrics)",
        }

    baseline_doc = json.loads(baseline_path.read_text())
    baseline = baseline_doc.get("metrics", baseline_doc)  # support both shapes

    threshold = cfg["threshold_pct"] / 100.0
    regressed: list[dict] = []
    for metric, base_val in baseline.items():
        if metric not in current:
            regressed.append(
                {
                    "metric": metric,
                    "baseline": base_val,
                    "current": None,
                    "delta_pct": None,
                    "reason": "metric absent in current results",
                }
            )
            continue
        cur_val = current[metric]
        if not isinstance(cur_val, int | float) or not isinstance(base_val, int | float):
            continue  # non-numeric — skip (user-defined, may be metadata)
        if base_val == 0:
            continue  # avoid div-by-zero; user picks meaningful metrics
        delta_pct = ((cur_val - base_val) / base_val) * 100
        if cur_val < base_val * (1 - threshold):
            regressed.append(
                {
                    "metric": metric,
                    "baseline": base_val,
                    "current": cur_val,
                    "delta_pct": round(delta_pct, 2),
                    "reason": f"dropped {abs(delta_pct):.1f}% (threshold {cfg['threshold_pct']}%)",
                }
            )

    if regressed:
        _write_report(
            ctx.task_dir,
            status="regression",
            current=current,
            baseline=baseline,
            regressed=regressed,
            threshold_pct=cfg["threshold_pct"],
        )
        console.print(
            f"  [red]✗[/red] regression detected on {len(regressed)} metric(s): "
            f"{', '.join(r['metric'] for r in regressed)}"
        )
        return {
            "skipped": False,
            "success": False,
            "regressed_metrics": regressed,
            "current": current,
            "baseline_captured": False,
            "cost_usd": 0.0,
            "duration_s": wall_duration,
            "summary": f"regression on {len(regressed)} metric(s)",
        }

    _write_report(
        ctx.task_dir,
        status="pass",
        current=current,
        baseline=baseline,
        regressed=[],
        threshold_pct=cfg["threshold_pct"],
    )
    console.print(
        f"  [green]✓[/green] all {len(current)} metrics within {cfg['threshold_pct']}% "
        f"threshold of baseline ({wall_duration:.0f}s)"
    )
    return {
        "skipped": False,
        "success": True,
        "regressed_metrics": [],
        "current": current,
        "baseline_captured": False,
        "cost_usd": 0.0,
        "duration_s": wall_duration,
        "summary": f"all {len(current)} metrics ≥ baseline - {cfg['threshold_pct']}%",
    }


def _skipped(reason: str) -> dict:
    return {
        "skipped": True,
        "reason": reason,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }


def _failed(reason: str, duration: float) -> dict:
    return {
        "skipped": False,
        "success": False,
        "reason": reason,
        "regressed_metrics": [],
        "cost_usd": 0.0,
        "duration_s": duration,
    }


def _write_baseline(path: Path, metrics: dict, ctx: StageContext) -> None:
    doc = {
        "metrics": metrics,
        "captured_at": datetime.now(UTC).isoformat(),
        "captured_from_task": ctx.task_dir.name,
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _write_report(
    task_dir: Path,
    *,
    status: str,
    current: dict | None,
    baseline: dict | None,
    regressed: list[dict],
    threshold_pct: float,
) -> None:
    validation_dir = task_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    report = validation_dir / "regression.md"

    lines = [f"# Regression check: {status}", ""]
    lines.append(f"**Threshold:** {threshold_pct}% per metric (higher is better).")
    lines.append("")

    if status == "baseline_captured":
        lines.append("No prior baseline — captured current results as new baseline.")
        lines.append("")
    if current:
        lines.append("## Current results")
        for k, v in current.items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if baseline:
        lines.append("## Baseline")
        for k, v in baseline.items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if regressed:
        lines.append("## Regressed metrics")
        for r in regressed:
            lines.append(
                f"- **`{r['metric']}`** — {r['reason']}. "
                f"baseline={r['baseline']}, current={r['current']}"
            )
        lines.append("")
    report.write_text("\n".join(lines))


def _write_failure_report(
    task_dir: Path,
    msg: str,
    current: dict | None,
    baseline: dict | None,
    stderr_tail: str = "",
) -> None:
    validation_dir = task_dir / "validation"
    validation_dir.mkdir(parents=True, exist_ok=True)
    report = validation_dir / "regression.md"
    lines = [
        "# Regression check: FAILED (could not run / parse bench)",
        "",
        f"**Error:** {msg}",
        "",
    ]
    if stderr_tail:
        lines.append("## stderr tail")
        lines.append("```")
        lines.append(stderr_tail)
        lines.append("```")
        lines.append("")
    if current is not None:
        lines.append("## Partial output")
        lines.append("```json")
        lines.append(json.dumps(current, indent=2))
        lines.append("```")
    report.write_text("\n".join(lines))
