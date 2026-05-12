r"""auto_resurvey action — Stage 11.

Runs after release_docs (Stage 10). If tech-writer flagged brief.md
staleness via `docs/maintenance_notes.md`, this handler invokes
project-surveyor to overwrite `projects/<name>/brief.md` with fresh
content. Otherwise it skips silently — no cost, no LLM call.

Trigger contract:
  - tech-writer writes `docs/maintenance_notes.md` ONLY when its
    `_extract_maintenance_notes()` finds a real signal (the literal
    `Run \`code-loops resurvey` invocation in the agent's output).
  - This handler treats presence of the file + that command string
    as the trigger. Absence = no-op.

Cost: ~$0.30-3.00 per surveyor invocation (Opus on max effort,
scales with target project size). Charged to the task's meta.yaml
under stage `auto_resurvey`.
"""

from __future__ import annotations

import time
from pathlib import Path

from rich.console import Console

from ..project_loader import PROJECTS_DIR
from ..runner import RunnerFactory
from .prompt import StageContext

console = Console()

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SURVEYOR_PROMPT_PATH = PACKAGE_DIR / "agents" / "meta" / "project-surveyor.md"


def run_auto_resurvey(stage_def: dict, ctx: StageContext) -> dict:
    notes_path = ctx.task_dir / "docs" / "maintenance_notes.md"

    if not notes_path.exists():
        console.print("  [dim]auto_resurvey:[/dim] no maintenance_notes.md → skip")
        return _skipped("no maintenance_notes flagged")

    notes = notes_path.read_text()
    if "code-loops resurvey" not in notes:
        console.print(
            "  [dim]auto_resurvey:[/dim] maintenance_notes present but no resurvey command → skip"
        )
        return _skipped("no resurvey command in notes")

    project_config = ctx.project_config
    if not project_config:
        console.print("  [yellow]auto_resurvey: no project_config → skip[/yellow]")
        return _skipped("no project config")

    project_name = project_config["project"]["name"]
    base_repo = Path(project_config["project"]["base_repo"])
    target_dir = PROJECTS_DIR / project_name
    brief_path = target_dir / "brief.md"

    if not base_repo.exists():
        console.print(f"  [yellow]auto_resurvey: base_repo missing ({base_repo}) → skip[/yellow]")
        return _skipped(f"base_repo missing: {base_repo}")

    if not SURVEYOR_PROMPT_PATH.exists():
        console.print(
            f"  [red]auto_resurvey: surveyor prompt missing ({SURVEYOR_PROMPT_PATH}) → skip[/red]"
        )
        return _skipped("surveyor prompt missing")

    console.print(
        f"  [cyan]auto_resurvey:[/cyan] regenerating {brief_path} via "
        f"project-surveyor (Opus max, expect ~$0.30-3.00, 1-6 min)"
    )

    sys_prompt = SURVEYOR_PROMPT_PATH.read_text()
    user_msg = (
        f"=== Survey project (auto-triggered by code-loops Stage 11) ===\n"
        f"name: {project_name}\n"
        f"base_repo: {base_repo}\n"
        f"output_path: {brief_path}\n\n"
        f"Trigger: tech-writer flagged brief.md staleness after the latest "
        f"task. Below is the maintenance note from the task — use it to focus "
        f"your scan on what changed:\n\n"
        f"---\n{notes}\n---\n\n"
        f"Scan the project and use the Write tool to save the refreshed brief "
        f"to the absolute output_path: {brief_path}\n"
        f"Do NOT write anywhere else. Do NOT modify the target project."
    )

    pre_mtime = brief_path.stat().st_mtime if brief_path.exists() else 0.0

    factory = RunnerFactory()
    runner = factory.make({"model": "claude-opus-4-7", "effort": "max"})

    wall_start = time.monotonic()
    result = runner.run(sys_prompt, user_msg, cwd=str(base_repo))
    wall_duration = time.monotonic() - wall_start

    if not brief_path.exists():
        console.print(
            f"  [red]✗ surveyor finished but {brief_path} not found "
            f"(check {base_repo} for stray brief.md)[/red]"
        )
        return {
            "skipped": False,
            "success": False,
            "reason": "brief.md not written",
            "cost_usd": result.cost_usd or 0,
            "duration_s": wall_duration,
        }
    if brief_path.stat().st_mtime <= pre_mtime:
        console.print(
            f"  [yellow]⚠ surveyor finished but {brief_path} mtime unchanged "
            f"(may have written same content)[/yellow]"
        )

    console.print(
        f"  [green]✓[/green] brief.md updated ({wall_duration:.0f}s, ${result.cost_usd or 0:.2f})"
    )
    return {
        "skipped": False,
        "success": True,
        "outputs": {f"projects/{project_name}/brief.md": "regenerated"},
        "cost_usd": result.cost_usd or 0,
        "duration_s": wall_duration,
        "summary": f"resurvey {project_name} from {base_repo} → {brief_path}",
    }


def _skipped(reason: str) -> dict:
    return {
        "skipped": True,
        "reason": reason,
        "cost_usd": 0.0,
        "duration_s": 0.0,
    }
