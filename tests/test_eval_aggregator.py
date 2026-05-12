"""Tests for eval_aggregator — meta.yaml scanning and aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from code_loops.eval_aggregator import (
    _aggregate_stages,
    _count_by,
    _empty_aggregation,
    _run_row,
    aggregate_recent_runs,
    build_eval_message,
)


def _write_meta(task_dir: Path, data: dict) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "meta.yaml").write_text(yaml.safe_dump(data, sort_keys=False))


def _meta(
    task_id: str,
    *,
    status: str = "completed",
    mode: str = "feature",
    cost: float = 5.0,
    created_at: str = "2026-05-08T10:00:00+00:00",
    redesign_loops: int = 0,
    final_loops: int = 0,
    stages: dict | None = None,
) -> dict:
    return {
        "task_id": task_id,
        "mode": mode,
        "status": status,
        "cost_usd": cost,
        "created_at": created_at,
        "redesign_loop_count": redesign_loops,
        "final_loop_count": final_loops,
        "stages": stages or {},
    }


# ---- aggregate_recent_runs ----


def test_aggregate_empty_dir(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    agg = aggregate_recent_runs(tasks)
    assert agg == _empty_aggregation()
    assert agg["total_runs"] == 0


def test_aggregate_missing_dir(tmp_path):
    agg = aggregate_recent_runs(tmp_path / "nope")
    assert agg["total_runs"] == 0


def test_aggregate_orders_by_created_at_desc(tmp_path):
    tasks = tmp_path / "tasks"
    _write_meta(tasks / "0001_a", _meta("0001_a", created_at="2026-05-01T10:00:00+00:00"))
    _write_meta(tasks / "0002_b", _meta("0002_b", created_at="2026-05-08T10:00:00+00:00"))
    _write_meta(tasks / "0003_c", _meta("0003_c", created_at="2026-05-05T10:00:00+00:00"))
    agg = aggregate_recent_runs(tasks, last_n=10)
    ids = [r["task_id"] for r in agg["per_run_summary"]]
    assert ids == ["0002_b", "0003_c", "0001_a"]


def test_aggregate_respects_last_n_cap(tmp_path):
    tasks = tmp_path / "tasks"
    for i in range(5):
        _write_meta(
            tasks / f"000{i}_x",
            _meta(f"000{i}_x", created_at=f"2026-05-0{i + 1}T10:00:00+00:00"),
        )
    agg = aggregate_recent_runs(tasks, last_n=3)
    assert agg["total_runs"] == 3


def test_aggregate_skips_non_meta_dirs(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "not_a_task").mkdir()  # no meta.yaml
    _write_meta(tasks / "0001_real", _meta("0001_real"))
    agg = aggregate_recent_runs(tasks)
    assert agg["total_runs"] == 1


def test_aggregate_skips_invalid_yaml(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    bad = tasks / "0001_bad"
    bad.mkdir()
    (bad / "meta.yaml").write_text("not: valid: yaml: [")
    _write_meta(tasks / "0002_good", _meta("0002_good"))
    agg = aggregate_recent_runs(tasks)
    assert agg["total_runs"] == 1


def test_aggregate_status_and_mode_breakdown(tmp_path):
    tasks = tmp_path / "tasks"
    _write_meta(tasks / "0001", _meta("0001", status="completed", mode="feature"))
    _write_meta(tasks / "0002", _meta("0002", status="completed", mode="from_problem"))
    _write_meta(tasks / "0003", _meta("0003", status="escalated", mode="feature"))
    agg = aggregate_recent_runs(tasks)
    assert agg["by_status"] == {"completed": 2, "escalated": 1}
    assert agg["by_mode"] == {"feature": 2, "from_problem": 1}


def test_aggregate_loop_counts_summed(tmp_path):
    tasks = tmp_path / "tasks"
    _write_meta(tasks / "0001", _meta("0001", redesign_loops=1, final_loops=2))
    _write_meta(tasks / "0002", _meta("0002", redesign_loops=0, final_loops=3))
    agg = aggregate_recent_runs(tasks)
    assert agg["redesign_loops_total"] == 1
    assert agg["final_loops_total"] == 5


def test_aggregate_cost_trend_oldest_to_newest(tmp_path):
    tasks = tmp_path / "tasks"
    _write_meta(tasks / "0001", _meta("0001", cost=10.0, created_at="2026-05-01T10:00:00+00:00"))
    _write_meta(tasks / "0002", _meta("0002", cost=20.0, created_at="2026-05-05T10:00:00+00:00"))
    _write_meta(tasks / "0003", _meta("0003", cost=15.0, created_at="2026-05-08T10:00:00+00:00"))
    agg = aggregate_recent_runs(tasks)
    # Cost trend reversed back to oldest-first for human reading
    assert agg["cost_trend_oldest_to_newest"] == [10.0, 20.0, 15.0]


# ---- _aggregate_stages ----


def test_aggregate_stages_means_and_max():
    runs = [
        {
            "stages": {
                "rfc": {"attempts": 2, "duration_s": 100, "cost_usd": 2.0},
                "critique": {"attempts": 1, "duration_s": 50, "cost_usd": 1.0},
            }
        },
        {
            "stages": {
                "rfc": {"attempts": 4, "duration_s": 200, "cost_usd": 4.0},
                "critique": {"attempts": 1, "duration_s": 60, "cost_usd": 1.5},
            }
        },
    ]
    out = _aggregate_stages(runs)
    assert out["rfc"]["mean_attempts"] == 3.0
    assert out["rfc"]["max_attempts"] == 4
    assert out["rfc"]["mean_duration_s"] == 150.0
    assert out["rfc"]["mean_cost_usd"] == 3.0
    assert out["rfc"]["total_cost_usd"] == 6.0
    assert out["critique"]["mean_attempts"] == 1.0


def test_aggregate_stages_handles_missing_fields():
    runs = [{"stages": {"prd": {}}}]  # all fields missing
    out = _aggregate_stages(runs)
    assert out["prd"]["mean_attempts"] == 1.0
    assert out["prd"]["mean_duration_s"] == 0


def test_aggregate_stages_skips_non_dict_entries():
    runs = [{"stages": {"prd": "not a dict"}}]
    out = _aggregate_stages(runs)
    assert "prd" not in out


# ---- _count_by ----


def test_count_by_handles_missing_field():
    runs = [{"x": "a"}, {"x": "b"}, {}]
    out = _count_by(runs, "x")
    assert out == {"a": 1, "b": 1, "unknown": 1}


# ---- _run_row ----


def test_run_row_summary_shape():
    r = _meta(
        "0042_x",
        cost=12.34,
        redesign_loops=1,
        final_loops=2,
        stages={"prd": {"status": "done"}, "plan": {"status": "running"}},
    )
    row = _run_row(r)
    assert row["task_id"] == "0042_x"
    assert row["cost_usd"] == 12.34
    assert row["stages_total"] == 2
    assert row["stages_completed"] == 1
    assert row["redesign_loops"] == 1
    assert row["final_loops"] == 2


# ---- build_eval_message ----


def test_build_message_contains_required_blocks():
    agg = _empty_aggregation()
    msg = build_eval_message(agg, agent_log="abc123 fix something")
    assert "=== Recent runs (last 0) ===" in msg
    assert "=== git diff agents/ ===" in msg
    assert "abc123 fix something" in msg
    assert "=== focus ===" not in msg


def test_build_message_includes_focus_when_provided():
    agg = _empty_aggregation()
    msg = build_eval_message(agg, agent_log="-", focus="why convergence dropped?")
    assert "=== focus ===" in msg
    assert "why convergence dropped?" in msg


def test_build_message_aggregation_is_valid_json():
    agg = aggregate_recent_runs(Path("/nonexistent"))
    msg = build_eval_message(agg, agent_log="-")
    # Extract the JSON block (between first '{' after the header line and matching '}')
    start = msg.index("{")
    # Walk forward until balanced braces
    depth = 0
    end = start
    for i, ch in enumerate(msg[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    parsed = json.loads(msg[start : end + 1])
    assert parsed["total_runs"] == 0
