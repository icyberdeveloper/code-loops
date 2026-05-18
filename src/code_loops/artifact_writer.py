"""Stage handler helper for scoped artifact writes + manifest sync.

Replaces ad-hoc `path.write_text(...)` calls in stage handlers with
explicit scoping (per-attempt, per-pass, per-round). Also writes a
"latest" copy at the legacy flat location so downstream stages reading
e.g. `design/final.md` keep working without modification.

Three scoping modes:
  - simple: single-attempt outputs (PRD, research, tech_writer)
  - attempt: per-retry outputs (impl_plan schema retries, final_review
    needs_more_work iterations)
  - pass: per-redesign-loop outputs (design + design_review)
  - round: per-debate-round outputs (within a pass)
"""

from __future__ import annotations

from pathlib import Path

from .manifest import Manifest


class ArtifactWriter:
    """Wraps Manifest + filesystem writes for a single task_dir."""

    def __init__(self, task_dir: Path, manifest: Manifest):
        self.task_dir = task_dir
        self.manifest = manifest

    # ---- Internal helpers ----

    def _write(self, abs_path: Path, content: str) -> None:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content)

    def _set_latest_copy(self, stage: str, name: str, content: str) -> Path:
        """Write `<stage>/<name>` (legacy flat path) + record in manifest."""
        latest = self.task_dir / stage / name
        self._write(latest, content)
        rel = f"{stage}/{name}"
        self.manifest.set_latest(stage, rel)
        return latest

    # ---- Public API ----

    def write_simple(
        self, stage: str, name: str, content: str, *, with_latest: bool = True
    ) -> Path:
        """Single-attempt output (e.g. PRD, research branch result).

        Writes to `<task>/<stage>/<name>`. No history scoping. Updates
        manifest.latest pointer when `with_latest=True` (default).

        Set `with_latest=False` for multi-output stages (parallel research
        branches) where N artifacts are produced and no single one is the
        canonical "latest" — otherwise each branch's call overwrites the
        previous, leaving an arbitrary iteration-order winner.
        """
        path = self.task_dir / stage / name
        self._write(path, content)
        if with_latest:
            self.manifest.set_latest(stage, f"{stage}/{name}")
        return path

    def write_attempt(self, stage: str, attempt_n: int, name: str, content: str) -> Path:
        """Per-retry artifact (e.g. impl_plan attempt 1 vs attempt 2).

        Writes to `<task>/<stage>/attempts/attempt_<N>/<name>` AND copies
        to `<task>/<stage>/<name>` as the "latest" downstream-readable copy.
        """
        scoped = self.task_dir / stage / "attempts" / f"attempt_{attempt_n}" / name
        self._write(scoped, content)
        self._set_latest_copy(stage, name, content)
        return scoped

    def write_pass(
        self,
        stage: str,
        pass_n: int,
        name: str,
        content: str,
        *,
        latest: bool = True,
    ) -> Path:
        """Per-redesign-pass artifact (e.g. design pass_1 vs pass_2 final).

        Writes to `<task>/<stage>/pass_<N>/<name>`. When `latest=True`
        (default), also copies to `<task>/<stage>/<name>` so downstream
        stages reading the legacy flat path see the latest pass's content.

        Set `latest=False` for intermediate per-pass artifacts (drafts,
        round outputs) where flat copying would mean pass 2's `draft_v1.md`
        overwrites pass 1's `draft_v1.md` — the exact bug per-pass scoping
        is meant to prevent. For those, only the pass-scoped path is the
        source of truth.
        """
        scoped = self.task_dir / stage / f"pass_{pass_n}" / name
        self._write(scoped, content)
        if latest:
            self._set_latest_copy(stage, name, content)
        return scoped

    def write_round(
        self,
        stage: str,
        pass_n: int,
        round_n: int,
        name: str,
        content: str,
    ) -> Path:
        """Per-debate-round artifact within a pass (e.g. critic v1 in round 1).

        Writes to `<task>/<stage>/pass_<N>/round_<R>/<name>`. No latest
        copy (rounds are intermediate; pass-level final.md is the "latest").
        """
        scoped = self.task_dir / stage / f"pass_{pass_n}" / f"round_{round_n}" / name
        self._write(scoped, content)
        return scoped

    def write_subtask_attempt(
        self,
        sid: str,
        attempt_n: int,
        role: str,
        content: str,
    ) -> Path:
        """Per-subtask per-attempt per-role artifact (implementation stage).

        Writes to `implementation/subtasks/<sid>/attempts/attempt_<N>/<role>.md`.
        """
        scoped = (
            self.task_dir
            / "implementation"
            / "subtasks"
            / sid
            / "attempts"
            / f"attempt_{attempt_n}"
            / f"{role}.md"
        )
        self._write(scoped, content)
        return scoped

    def write_subtask_final(self, sid: str, name: str, content: str) -> Path:
        """Per-subtask 'shipped' artifact (final test_writer.md, etc).

        Writes to `implementation/subtasks/<sid>/<name>`.
        """
        scoped = self.task_dir / "implementation" / "subtasks" / sid / name
        self._write(scoped, content)
        return scoped

    # ---- Append-only logs (debate.md, run.log) ----

    def append_log(self, relative_path: str, header: str, body: str) -> Path:
        """Append a section to a chronological log file.

        Used for debate.md (within a pass) and other per-pass/per-attempt
        chronologies. The path is taken verbatim — caller decides scoping.
        """
        path = self.task_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text() if path.exists() else ""
        path.write_text(f"{existing}\n## {header}\n\n{body}\n\n---\n")
        return path
