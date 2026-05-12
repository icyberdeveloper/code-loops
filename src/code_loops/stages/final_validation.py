"""final_validation action — programmatic checks on the worktree.

Three checks:
1. **RFC file coverage**: parse `## File-level changes` paths from design/final.md,
   compare to `implementation/_files_changed.txt`. Missing = paths in RFC absent
   from diff. Unexpected = paths in diff not in RFC. Both surfaced; only
   `missing` blocks approval.
2. **pytest** in the worktree (full suite).
3. **ruff** in the worktree.

Output: `validation/result.yaml` + per-check log files + summary.
Returns dict with `passed: bool` and detail fields used by release_review.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import yaml
from rich.console import Console

from .prompt import StageContext

console = Console()


# Matches lines like: `path/to/file.py — what changes here`
# or:                  `app/foo.py (new) — ...`
# under the `## File-level changes` heading.
FILE_LINE_RE = re.compile(
    r"^\s*[-*]\s*`?([\w./_\-]+\.\w+)`?\s*(?:\(.*?\))?\s*[—\-]",
    re.MULTILINE,
)


def run_final_validation(stage_def: dict, ctx: StageContext) -> dict:
    out_dir = ctx.task_dir / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    wall_start = time.monotonic()

    rfc_files = _parse_rfc_files(ctx.task_dir / "design" / "final.md")
    diff_files = _read_diff_files(ctx.task_dir / "implementation" / "_files_changed.txt")

    missing = sorted(rfc_files - diff_files)
    unexpected = sorted(diff_files - rfc_files)

    coverage_complete = not missing

    # Worktree is at task_dir/worktree/wt
    worktree_path = ctx.task_dir / "worktree" / "wt"
    pytest_result = _run_in_worktree(worktree_path, ["uv", "run", "pytest", "-q"], timeout_s=900)
    ruff_result = _run_in_worktree(
        worktree_path, ["uv", "run", "ruff", "check", "."], timeout_s=120
    )

    (out_dir / "pytest.log").write_text(_log_block(pytest_result))
    (out_dir / "ruff.log").write_text(_log_block(ruff_result))

    passed = coverage_complete and pytest_result.returncode == 0 and ruff_result.returncode == 0

    coverage_md = _build_coverage_md(rfc_files, diff_files, missing, unexpected)
    (out_dir / "coverage.md").write_text(coverage_md)

    result = {
        "passed": passed,
        "coverage_complete": coverage_complete,
        "missing_files": missing,
        "unexpected_files": unexpected,
        "rfc_files_count": len(rfc_files),
        "diff_files_count": len(diff_files),
        "pytest_rc": pytest_result.returncode,
        "ruff_rc": ruff_result.returncode,
    }
    (out_dir / "result.yaml").write_text(
        yaml.safe_dump(result, sort_keys=False, allow_unicode=True)
    )

    duration = time.monotonic() - wall_start
    status_word = "passed" if passed else "FAILED"
    console.print(
        f"  [dim]validation:[/dim] {status_word} "
        f"(coverage: {len(missing)} missing / {len(unexpected)} unexpected; "
        f"pytest rc={pytest_result.returncode}; ruff rc={ruff_result.returncode}; "
        f"{duration:.0f}s)"
    )
    return {
        "outputs": {
            "validation/result.yaml": (out_dir / "result.yaml").read_text(),
            "validation/coverage.md": coverage_md,
            "validation/pytest.log": (out_dir / "pytest.log").read_text(),
            "validation/ruff.log": (out_dir / "ruff.log").read_text(),
        },
        "cost_usd": 0.0,  # programmatic, no LLM
        "duration_s": duration,
        "passed": passed,
        "missing_files": missing,
        "unexpected_files": unexpected,
        "pytest_rc": pytest_result.returncode,
        "ruff_rc": ruff_result.returncode,
        "summary": _build_summary(result),
    }


def _parse_rfc_files(rfc_path: Path) -> set[str]:
    """Extract file paths under the `## File-level changes` section.

    Robust to slight formatting variations: backticked paths, `(new)` markers,
    en-dash or hyphen separator. Returns empty set if section missing.
    """
    if not rfc_path.exists():
        return set()
    text = rfc_path.read_text()
    # Find the section
    m = re.search(
        r"##\s*File-level changes\s*\n(.*?)(?=\n##\s|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return set()
    section = m.group(1)
    files = set(FILE_LINE_RE.findall(section))
    return files


def _read_diff_files(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def _run_in_worktree(wt_path: Path, cmd: list[str], *, timeout_s: int):
    if not wt_path.exists():

        class _R:
            returncode = -1
            stdout = f"(worktree path missing: {wt_path})"
            stderr = ""

        return _R()
    return subprocess.run(
        cmd,
        cwd=str(wt_path),
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )


def _log_block(result) -> str:
    return f"# rc={result.returncode}\n\n## stdout\n{result.stdout}\n\n## stderr\n{result.stderr}\n"


def _build_coverage_md(rfc_files, diff_files, missing, unexpected) -> str:
    parts = [
        "# Final coverage report",
        "",
        f"- RFC declared files: **{len(rfc_files)}**",
        f"- Diff touched files: **{len(diff_files)}**",
        f"- Missing (in RFC, not in diff): **{len(missing)}**",
        f"- Unexpected (in diff, not in RFC): **{len(unexpected)}**",
        "",
    ]
    if missing:
        parts.append("## Missing files (BLOCKING)")
        parts.extend(f"- `{p}`" for p in missing)
        parts.append("")
    if unexpected:
        parts.append("## Unexpected files (informational)")
        parts.extend(f"- `{p}`" for p in unexpected)
        parts.append("")
    if not missing and not unexpected:
        parts.append("✓ Diff exactly matches RFC declared files.")
    return "\n".join(parts) + "\n"


def _build_summary(result: dict) -> str:
    n_missing = len(result["missing_files"])
    pytest_rc = result["pytest_rc"]
    ruff_rc = result["ruff_rc"]
    coverage_str = "✓ complete" if result["coverage_complete"] else f"✗ {n_missing} missing"
    pytest_str = "✓" if pytest_rc == 0 else f"✗ rc={pytest_rc}"
    ruff_str = "✓" if ruff_rc == 0 else f"✗ rc={ruff_rc}"
    lines = [
        f"Coverage: {coverage_str}",
        f"  RFC files: {result['rfc_files_count']}, diff files: {result['diff_files_count']}",
        f"pytest: {pytest_str}",
        f"ruff: {ruff_str}",
    ]
    if result["missing_files"]:
        lines.append("Missing files:")
        lines.extend(f"  • {f}" for f in result["missing_files"][:10])
        if n_missing > 10:
            lines.append(f"  • ... and {n_missing - 10} more")
    return "\n".join(lines)
