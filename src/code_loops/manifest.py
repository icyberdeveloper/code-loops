"""Task artifact manifest — single source of truth for pipeline state.

The manifest.json file at the task root captures everything: task
metadata (id, mode, status, cost), per-stage status, per-pass
redesign-loop state, per-attempt retry state for impl_plan /
final_review, per-round debate state, per-subtask fix-loop attempts,
and `latest` artifact pointers for downstream stage inputs.

Schema versioning: incremented on any breaking shape change. Readers
must check `schema_version` before assuming fields.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Manifest:
    """Read/write tasks/<id>/manifest.json atomically.

    Operations are recorded incrementally as the engine runs. Each
    `record_*` method writes the file immediately so a crash mid-stage
    doesn't lose state.
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, Any] = self._load()

    @property
    def data(self) -> dict:
        return self._data

    # ---- I/O ----

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False))
        tmp.replace(self.path)

    # ---- Lifecycle ----

    def init_task(self, task_id: str, mode: str) -> None:
        self._data = {
            "task_id": task_id,
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "created_at": _now(),
            "status": "in_progress",
            "current_stage": None,
            "total_cost_usd": 0.0,
            "redesign_loop_count": 0,
            "final_loop_count": 0,
            "stages": {},
        }
        self._save()

    def set_status(self, status: str) -> None:
        self._data["status"] = status
        self._save()

    def set_current_stage(self, stage: str) -> None:
        self._data["current_stage"] = stage
        self._save()

    # ---- Stage-level ----

    def stage_entry(self, stage: str) -> dict:
        stages = self._data.setdefault("stages", {})
        return stages.setdefault(stage, {})

    def stage_started(self, stage: str) -> None:
        st = self.stage_entry(stage)
        st.setdefault("attempts_count", 0)
        st["status"] = "running"
        st["started_at"] = _now()
        self._save()

    def stage_completed(
        self,
        stage: str,
        *,
        cost_usd: float = 0,
        duration_s: float = 0,
    ) -> None:
        st = self._data["stages"][stage]
        st["status"] = "done"
        st["finished_at"] = _now()
        st["duration_s"] = round((st.get("duration_s") or 0) + duration_s, 2)
        st["cost_usd"] = round((st.get("cost_usd") or 0) + cost_usd, 4)
        self._data["total_cost_usd"] = round((self._data.get("total_cost_usd") or 0) + cost_usd, 4)
        self._save()

    def stage_failed(self, stage: str, reason: str) -> None:
        st = self._data["stages"][stage]
        st["status"] = "failed"
        st["failure_reason"] = reason
        st["finished_at"] = _now()
        self._save()

    def reset_stage(self, stage: str) -> None:
        """Wipe stage tracking for a re-run (preserves attempts_count history)."""
        stages = self._data.setdefault("stages", {})
        if stage in stages:
            preserved = {"attempts_count": stages[stage].get("attempts_count", 0)}
            stages[stage] = preserved
        self._save()

    def is_stage_done(self, stage: str) -> bool:
        return self._data.get("stages", {}).get(stage, {}).get("status") == "done"

    # ---- Pass-level (redesign loops in design / design_review) ----

    def record_pass(
        self,
        stage: str,
        pass_n: int,
        *,
        cost_usd: float = 0,
        duration_s: float = 0,
        converged: bool | None = None,
        rounds: int | None = None,
        verdict: str | None = None,
        theme: str | None = None,
        max_rounds_reached: bool | None = None,
        final_artifact: str | None = None,
    ) -> None:
        """Append a pass entry to stage.passes. Updates stage cost rollup."""
        st = self.stage_entry(stage)
        passes = st.setdefault("passes", [])
        entry: dict[str, Any] = {
            "pass_n": pass_n,
            "completed_at": _now(),
            "cost_usd": round(cost_usd, 4),
            "duration_s": round(duration_s, 2),
        }
        if rounds is not None:
            entry["rounds"] = rounds
        if converged is not None:
            entry["converged"] = converged
        if max_rounds_reached is not None:
            entry["max_rounds_reached"] = max_rounds_reached
        if verdict is not None:
            entry["verdict"] = verdict
        if theme is not None:
            entry["theme"] = theme
        if final_artifact is not None:
            entry["final"] = final_artifact
        passes.append(entry)
        st["passes_count"] = len(passes)
        self._save()

    # ---- Attempt-level (impl_plan retries, final_review needs_more_work, ...) ----

    def record_attempt(
        self,
        stage: str,
        attempt_n: int,
        *,
        outcome: str,
        cost_usd: float = 0,
        duration_s: float = 0,
        reason: str | None = None,
        extra: dict | None = None,
    ) -> None:
        """Append an attempt entry to stage.attempts."""
        st = self.stage_entry(stage)
        attempts = st.setdefault("attempts", [])
        entry: dict[str, Any] = {
            "attempt_n": attempt_n,
            "outcome": outcome,
            "completed_at": _now(),
            "cost_usd": round(cost_usd, 4),
            "duration_s": round(duration_s, 2),
        }
        if reason:
            entry["reason"] = reason
        if extra:
            entry.update(extra)
        attempts.append(entry)
        st["attempts_count"] = len(attempts)
        self._save()

    # ---- Subtask attempt-level (implementation stage) ----

    def record_subtask_attempt(
        self,
        sid: str,
        attempt_n: int,
        *,
        roles: dict | None = None,
        outcome: str | None = None,
    ) -> None:
        """Implementation stage: per-subtask per-attempt detail.

        `roles` is a free-form dict like
          {"test_writer": {"cost_usd": 0.2, "outcome": "ok"},
           "coder": {"cost_usd": 0.4, "outcome": "fail-test"},
           "triage": {"action": "retry-coder"}}
        """
        impl = self.stage_entry("implementation")
        subtasks = impl.setdefault("subtasks", {})
        entry = subtasks.setdefault(sid, {"attempts_count": 0, "attempts": []})
        attempt: dict[str, Any] = {
            "attempt_n": attempt_n,
            "completed_at": _now(),
        }
        if roles:
            attempt["roles"] = roles
        if outcome is not None:
            attempt["outcome"] = outcome
        entry["attempts"].append(attempt)
        entry["attempts_count"] = len(entry["attempts"])
        self._save()

    def set_subtask_final(
        self, sid: str, *, outcome: str, files_changed: list[str] | None = None
    ) -> None:
        impl = self.stage_entry("implementation")
        subtasks = impl.setdefault("subtasks", {})
        entry = subtasks.setdefault(sid, {"attempts_count": 0, "attempts": []})
        entry["outcome"] = outcome
        if files_changed is not None:
            entry["files_changed"] = files_changed
        self._save()

    # ---- Loop counters ----

    def increment_redesign_loop(self) -> int:
        n = (self._data.get("redesign_loop_count") or 0) + 1
        self._data["redesign_loop_count"] = n
        self._save()
        return n

    def increment_final_loop(self) -> int:
        n = (self._data.get("final_loop_count") or 0) + 1
        self._data["final_loop_count"] = n
        self._save()
        return n

    # ---- "Latest" pointer tracking ----

    def set_latest(self, stage: str, relative_path: str) -> None:
        """Record the 'latest' artifact path for a stage (relative to task_dir)."""
        self.stage_entry(stage)["latest"] = relative_path
        self._save()
