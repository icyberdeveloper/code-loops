"""TechWriterStage — produces changelog entry + (conditional) ADR after final_review.

Single LLM call. Writes two artifacts to task_dir/docs/:
- changelog_entry.md (always)
- adr/NNNN-<slug>.md (conditional — content may be `# ADR: not needed`)

The user copies these into the target project repo manually after review
(cross-repo audit gate).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from rich.console import Console

from ..runner import RunnerFactory
from .prompt import StageContext, load_agent_prompt

console = Console()


class TechWriterStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        wall_start = time.monotonic()
        out_dir = ctx.task_dir / "docs"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "adr").mkdir(parents=True, exist_ok=True)

        prd = _read_optional(ctx.task_dir / "prd" / "prd.md")
        rfc = _read_optional(ctx.task_dir / "design" / "final.md")
        diff = _read_optional(ctx.task_dir / "implementation" / "_full_diff.patch")
        files_changed = _read_optional(ctx.task_dir / "implementation" / "_files_changed.txt")
        meta = _read_optional(ctx.task_dir / "meta.yaml")

        sys_prompt = load_agent_prompt(ctx.repo_root / stage_def["prompt"], ctx)
        user_msg = (
            f"=== prd/prd.md ===\n{prd}\n\n"
            f"=== design/final.md ===\n{rfc}\n\n"
            f"=== implementation/_full_diff.patch ===\n{diff}\n\n"
            f"=== implementation/_files_changed.txt ===\n{files_changed}\n\n"
            f"=== meta.yaml ===\n{meta}\n"
        )

        runner = self.factory.make(stage_def)
        result = runner.run(sys_prompt, user_msg)

        # Persist the LLM's full response for forensics
        (out_dir / "tech_writer_response.md").write_text(result.text)

        # Extract the changelog bullet and ADR if present.
        changelog_bullet = _extract_changelog_bullet(result.text)
        (out_dir / "changelog_entry.md").write_text(changelog_bullet + "\n")

        adr_filename, adr_body = _extract_adr(result.text)
        adr_skipped = "ADR: not needed" in adr_body
        adr_path = out_dir / "adr" / adr_filename
        adr_path.write_text(adr_body)

        # Maintenance notes are conditional — written only if the agent
        # emitted a `## Maintenance notes` section (signal that brief.md
        # or conventions doc may need a refresh after this change).
        maintenance_notes = _extract_maintenance_notes(result.text)
        maintenance_path = out_dir / "maintenance_notes.md"
        if maintenance_notes:
            maintenance_path.write_text(maintenance_notes)
        maintenance_status = (
            "written" if maintenance_notes else "no maintenance signal (brief stays accurate)"
        )

        wall_duration = time.monotonic() - wall_start
        console.print(
            f"  [dim]release_docs:[/dim] changelog written; "
            f"ADR {'skipped' if adr_skipped else f'created ({adr_filename})'}; "
            f"maintenance: {maintenance_status} "
            f"({wall_duration:.0f}s, ${result.cost_usd or 0:.2f})"
        )
        outputs = {
            "docs/changelog_entry.md": changelog_bullet,
            f"docs/adr/{adr_filename}": adr_body,
        }
        if maintenance_notes:
            outputs["docs/maintenance_notes.md"] = maintenance_notes
        return {
            "outputs": outputs,
            "cost_usd": result.cost_usd or 0,
            "duration_s": wall_duration,
            "adr_skipped": adr_skipped,
            "maintenance_present": bool(maintenance_notes),
            "summary": (
                f"changelog: {changelog_bullet[:120]}...\n"
                f"adr: {'skipped' if adr_skipped else adr_filename}\n"
                f"maintenance: {maintenance_status}"
            ),
        }


def _read_optional(path: Path) -> str:
    return path.read_text() if path.exists() else f"(missing: {path.name})"


CHANGELOG_BULLET_RE = re.compile(r"^\s*-\s+.+$", re.MULTILINE)


def _extract_changelog_bullet(text: str) -> str:
    """Pull the first markdown bullet that looks like a user-facing changelog entry.

    Falls back to a minimal bullet if nothing is found.
    """
    # Look for a `### 1.` or `## changelog` section first.
    section = re.search(
        r"changelog[_\s]?entry.*?\n(.*?)(?=###|##\s|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    haystack = section.group(1) if section else text
    m = CHANGELOG_BULLET_RE.search(haystack)
    if m:
        return m.group(0).strip()
    return "- (no changelog bullet emitted by tech_writer — review docs/tech_writer_response.md)"


ADR_TITLE_RE = re.compile(r"^#\s+ADR\s+(\d{4}):\s+(.+)$", re.MULTILINE)
ADR_BLOCK_RE = re.compile(r"(#\s+ADR\s+\d{4}:.*?)(?=\Z)", re.DOTALL)
SLUG_RE = re.compile(r"[^\w-]+")


def _extract_adr(text: str) -> tuple[str, str]:
    """Return (filename, body). If no ADR is needed, return a stub file.

    Supports bilingual LLM output (English "not needed" + Russian "не нужен").
    """
    if "ADR: not needed" in text or "не нужен" in text.lower() and "adr" in text.lower():
        return (
            "0000-not-needed.md",
            "# ADR: not needed\n\n(Tech writer judged the change as not warranting an ADR.)\n",
        )
    m = ADR_TITLE_RE.search(text)
    if not m:
        return (
            "0000-not-needed.md",
            "# ADR: not needed\n\n(No ADR block detected in tech_writer output.)\n",
        )
    number = m.group(1)
    title = m.group(2).strip()
    slug = SLUG_RE.sub("-", title.lower()).strip("-")[:50]
    body_match = ADR_BLOCK_RE.search(text)
    body = body_match.group(1).strip() if body_match else text
    return f"{number}-{slug}.md", body + "\n"


def _extract_maintenance_notes(text: str) -> str | None:
    """Return the `## Maintenance notes` section content or None if absent.

    Slices from the heading to the next top-level `## ` header (or EOF).
    Strips a trailing fence/whitespace. Returns None for empty / placeholder-only
    sections so docs/maintenance_notes.md is not created when no signal.
    """
    m = re.search(
        r"^##\s+Maintenance\s+notes\s*$(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    body = m.group(1).strip()
    if not body:
        return None
    # Real signal = the literal `Run \`code-loops resurvey` command
    # invocation. If absent, the section either had no triggers or the
    # agent left a placeholder — either way no file written.
    if "Run `code-loops resurvey" not in body:
        return None
    return f"# Maintenance notes\n\n{body}\n"
