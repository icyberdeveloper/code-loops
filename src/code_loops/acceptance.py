"""Acceptance criteria checker — runs subtask-spec'd machine-verifiable
post-conditions. No LLM. Returns list of violations (empty = all green).

Tech-lead emits `acceptance: [{type, file, pattern, target, outcomes, ...}]`
per subtask. Validator runs each check после role finishes editing — subtask
shipped только когда ВСЕ acceptance criteria met (in addition to pytest/ruff).

This is the structural answer к "agent says done но spec not met" — eliminates
need для LLM judgment per-subtask compliance.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AcceptanceViolation:
    check: dict  # original check spec
    reason: str

    def __str__(self) -> str:
        ctype = self.check.get("type", "?")
        target = self.check.get("file") or self.check.get("nodeid") or "?"
        return f"{ctype}({target}): {self.reason}"


def check_acceptance(
    criteria: list[dict], wt_path: Path, scope_files: list[str] | None = None
) -> list[AcceptanceViolation]:
    """Verify each check against worktree state. Returns violations list."""
    violations: list[AcceptanceViolation] = []
    for c in criteria:
        ctype = c.get("type")
        try:
            v = _dispatch(ctype, c, wt_path, scope_files)
            if v is not None:
                violations.append(v)
        except Exception as exc:
            violations.append(AcceptanceViolation(c, f"check raised: {exc}"))
    return violations


def _dispatch(ctype: str, c: dict, wt_path: Path, scope_files: list[str] | None):
    if ctype == "pytest_collected_count":
        return _check_pytest_collected_count(c, wt_path, scope_files)
    if ctype == "pytest_outcome_count":
        return _check_pytest_outcome_count(c, wt_path, scope_files)
    if ctype == "file_contains_pattern":
        return _check_file_contains(c, wt_path, must_match=True)
    if ctype == "file_not_contains_pattern":
        return _check_file_contains(c, wt_path, must_match=False)
    if ctype == "file_size_min":
        return _check_file_size_min(c, wt_path)
    if ctype == "ruff_clean":
        return _check_ruff_clean(c, wt_path)
    if ctype == "json_path_exists":
        return _check_json_path(c, wt_path)
    return AcceptanceViolation(c, f"unknown check type {ctype!r}")


def _check_pytest_collected_count(c, wt_path, scope_files):
    target = c["target"]
    args = ["uv", "run", "pytest", "--collect-only", "-q"]
    if scope_files:
        args.extend(scope_files)
    proc = subprocess.run(args, cwd=str(wt_path), capture_output=True, text=True)
    # Parse "N tests collected" from output
    m = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout + proc.stderr)
    if not m:
        return AcceptanceViolation(c, "could not parse pytest --collect-only output")
    actual = int(m.group(1))
    if actual != target:
        return AcceptanceViolation(c, f"expected {target} collected, got {actual}")
    return None


def _check_pytest_outcome_count(c, wt_path, scope_files):
    """Verify outcome counts: {passed: N, failed: M, xfailed: K, xpassed: L}."""
    expected: dict = c["outcomes"]
    args = ["uv", "run", "pytest", "-v", "--tb=no", "--no-header"]
    if scope_files:
        args.extend(scope_files)
    proc = subprocess.run(args, cwd=str(wt_path), capture_output=True, text=True)
    text = proc.stdout + proc.stderr
    # Look for summary line like "16 xfailed, 1 passed in 0.5s"
    summary = ""
    for line in text.splitlines():
        if " in " in line and any(k in line for k in ("passed", "failed", "xfailed", "xpassed", "error")):
            summary = line
    actual: dict[str, int] = {}
    for kind in ("passed", "failed", "xfailed", "xpassed", "skipped", "error", "errors"):
        m = re.search(rf"(\d+)\s+{kind}", summary)
        if m:
            actual[kind.rstrip("s") if kind == "errors" else kind] = int(m.group(1))
    mismatches = []
    for kind, want in expected.items():
        got = actual.get(kind, 0)
        if got != want:
            mismatches.append(f"{kind}: expected {want}, got {got}")
    if mismatches:
        return AcceptanceViolation(c, "; ".join(mismatches))
    return None


def _check_file_contains(c, wt_path, must_match: bool):
    target = wt_path / c["file"]
    if not target.exists():
        if must_match:
            return AcceptanceViolation(c, "file does not exist")
        return None  # vacuously satisfies "must NOT contain"
    content = target.read_text(errors="replace")
    try:
        found = bool(re.search(c["pattern"], content, re.MULTILINE))
    except re.error as exc:
        return AcceptanceViolation(c, f"invalid regex: {exc}")
    if must_match and not found:
        return AcceptanceViolation(c, f"pattern {c['pattern']!r} not found")
    if not must_match and found:
        return AcceptanceViolation(c, f"forbidden pattern {c['pattern']!r} present")
    return None


def _check_file_size_min(c, wt_path):
    target = wt_path / c["file"]
    if not target.exists():
        return AcceptanceViolation(c, "file does not exist")
    size = target.stat().st_size
    want = c["bytes"]
    if size < want:
        return AcceptanceViolation(c, f"file size {size}B < required {want}B")
    return None


def _check_ruff_clean(c, wt_path):
    target = c["file"]
    proc = subprocess.run(
        ["uv", "run", "ruff", "check", target],
        cwd=str(wt_path),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        return AcceptanceViolation(c, f"ruff failures: {' | '.join(tail)}")
    return None


def _check_json_path(c, wt_path):
    target = wt_path / c["file"]
    if not target.exists():
        return AcceptanceViolation(c, "file does not exist")
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return AcceptanceViolation(c, f"JSON parse failed: {exc}")
    cur = data
    for key in c["path"]:
        try:
            cur = cur[key]
        except (KeyError, IndexError, TypeError):
            return AcceptanceViolation(c, f"path element {key!r} not found")
    return None


def format_violations(violations: list[AcceptanceViolation]) -> str:
    if not violations:
        return ""
    lines = ["ACCEPTANCE VIOLATIONS — subtask spec criteria not met:"]
    for v in violations:
        lines.append(f"  - {v}")
    return "\n".join(lines)
