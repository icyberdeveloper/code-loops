"""Task meta.yaml read/write — materialized state of a pipeline run."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MetaStore:
    """Single source of truth for task state. Atomic writes via tmp+rename."""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return yaml.safe_load(self.path.read_text()) or {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(self._data, sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)

    @property
    def data(self) -> dict:
        return self._data

    def init_task(self, task_id: str, mode: str) -> None:
        self._data = {
            "task_id": task_id,
            "mode": mode,
            "created_at": _now(),
            "status": "in_progress",
            "current_stage": None,
            "cost_usd": 0.0,
            "redesign_loop_count": 0,
            "stages": {},
        }
        self._save()

    def stage_started(self, name: str) -> None:
        self._data["current_stage"] = name
        stages = self._data.setdefault("stages", {})
        st = stages.setdefault(name, {})
        st["status"] = "running"
        st["started_at"] = _now()
        st["attempts"] = (st.get("attempts") or 0) + 1
        self._save()

    def stage_completed(
        self,
        name: str,
        *,
        cost_usd: float | None = None,
        duration_s: float = 0,
    ) -> None:
        st = self._data["stages"][name]
        st["status"] = "done"
        st["finished_at"] = _now()
        st["duration_s"] = round(duration_s, 2)
        if cost_usd is not None:
            st["cost_usd"] = round((st.get("cost_usd") or 0) + cost_usd, 4)
            self._data["cost_usd"] = round((self._data.get("cost_usd") or 0) + cost_usd, 4)
        self._save()

    def stage_failed(self, name: str, reason: str) -> None:
        st = self._data["stages"][name]
        st["status"] = "failed"
        st["failure_reason"] = reason
        st["finished_at"] = _now()
        self._save()

    def set_status(self, status: str) -> None:
        self._data["status"] = status
        self._save()

    def is_stage_done(self, name: str) -> bool:
        return self._data.get("stages", {}).get(name, {}).get("status") == "done"

    def reset_stage(self, name: str) -> None:
        """Wipe a stage's tracking so it re-runs. Preserves attempts counter."""
        stages = self._data.setdefault("stages", {})
        if name in stages:
            attempts = stages[name].get("attempts", 0)
            stages[name] = {"attempts": attempts}
        self._save()

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
