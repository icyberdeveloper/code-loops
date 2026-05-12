"""Aggregate recent task runs into pipeline-evaluator input.

Scans `tasks/<id>/meta.yaml` for the last N runs, computes per-stage
stats + cost/wall-clock trends, and builds the user message that
triggers pipeline-evaluator's meta-evaluation report.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml


def aggregate_recent_runs(tasks_dir: Path, last_n: int = 20) -> dict[str, Any]:
    """Read meta.yaml for the last N task runs (sorted by created_at desc).

    Returns aggregation dict suitable for the user message of
    pipeline-evaluator.md. Skips tasks without parseable meta.yaml.
    """
    runs: list[dict] = []
    if not tasks_dir.exists():
        return _empty_aggregation()

    for task_dir in tasks_dir.iterdir():
        if not task_dir.is_dir():
            continue
        meta_path = task_dir / "meta.yaml"
        if not meta_path.exists():
            continue
        try:
            data = yaml.safe_load(meta_path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict) or "task_id" not in data:
            continue
        runs.append(data)

    runs.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    runs = runs[:last_n]

    return {
        "total_runs": len(runs),
        "by_status": _count_by(runs, "status"),
        "by_mode": _count_by(runs, "mode"),
        "per_run_summary": [_run_row(r) for r in runs],
        "per_stage_aggregations": _aggregate_stages(runs),
        "cost_trend_oldest_to_newest": [
            round(r.get("cost_usd", 0) or 0, 2) for r in reversed(runs)
        ],
        "redesign_loops_total": sum((r.get("redesign_loop_count") or 0) for r in runs),
        "final_loops_total": sum((r.get("final_loop_count") or 0) for r in runs),
    }


def _empty_aggregation() -> dict[str, Any]:
    return {
        "total_runs": 0,
        "by_status": {},
        "by_mode": {},
        "per_run_summary": [],
        "per_stage_aggregations": {},
        "cost_trend_oldest_to_newest": [],
        "redesign_loops_total": 0,
        "final_loops_total": 0,
    }


def _count_by(runs: list[dict], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in runs:
        v = str(r.get(field, "unknown"))
        counts[v] = counts.get(v, 0) + 1
    return counts


def _run_row(r: dict) -> dict[str, Any]:
    return {
        "task_id": r.get("task_id"),
        "mode": r.get("mode"),
        "status": r.get("status"),
        "cost_usd": round(r.get("cost_usd", 0) or 0, 2),
        "stages_completed": sum(
            1 for st in (r.get("stages") or {}).values() if st.get("status") == "done"
        ),
        "stages_total": len(r.get("stages") or {}),
        "redesign_loops": r.get("redesign_loop_count", 0) or 0,
        "final_loops": r.get("final_loop_count", 0) or 0,
        "current_stage": r.get("current_stage"),
    }


def _aggregate_stages(runs: list[dict]) -> dict[str, dict[str, Any]]:
    """Per-stage stats across runs: mean attempts / duration / cost, max attempts."""
    stage_data: dict[str, list[dict]] = {}
    for r in runs:
        for name, st in (r.get("stages") or {}).items():
            if not isinstance(st, dict):
                continue
            stage_data.setdefault(name, []).append(st)

    result: dict[str, dict[str, Any]] = {}
    for name, states in stage_data.items():
        durations = [s.get("duration_s", 0) or 0 for s in states]
        costs = [s.get("cost_usd", 0) or 0 for s in states]
        attempts = [s.get("attempts", 1) or 1 for s in states]
        n = len(states)
        result[name] = {
            "n_runs_with_stage": n,
            "mean_attempts": round(sum(attempts) / n, 2) if n else 0,
            "max_attempts": max(attempts) if attempts else 0,
            "mean_duration_s": round(sum(durations) / n, 1) if n else 0,
            "mean_cost_usd": round(sum(costs) / n, 4) if n else 0,
            "total_cost_usd": round(sum(costs), 4),
        }
    return result


def get_recent_agent_changes(repo_root: Path, last_n: int = 20) -> str:
    """Return `git log --oneline` of agents/ folder for A/B context."""
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", f"-{last_n}", "--", "agents/"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = proc.stdout.strip()
        return out if out else "(no agent changes recorded)"
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "(git log unavailable)"


def build_eval_message(
    aggregation: dict[str, Any],
    agent_log: str,
    focus: str | None = None,
) -> str:
    """Build the user message that triggers pipeline-evaluator."""
    parts = [
        f"=== Recent runs (last {aggregation['total_runs']}) ===",
        json.dumps(aggregation, indent=2, default=str, ensure_ascii=False),
        "",
        "=== git diff agents/ ===",
        agent_log,
    ]
    if focus:
        parts.extend(["", "=== focus ===", focus])
    return "\n".join(parts)
