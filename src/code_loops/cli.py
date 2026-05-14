"""code-loops CLI entrypoint.

Installed as the `code-loops` console script via pyproject.toml. From the
repo root: `uv run code-loops <command>`. From elsewhere after editable
install: `code-loops <command>`.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from .engine import Engine
from .eval_aggregator import (
    aggregate_recent_runs,
    build_eval_message,
    get_recent_agent_changes,
)
from .manifest import Manifest
from .meta import MetaStore
from .project_loader import PROJECTS_DIR, list_projects, load_project_config
from .runner import RunnerFactory

# Package data (pipeline.yaml, agents/) ships inside the wheel — resolved
# from this module's location. Works in both editable and pip install.
PACKAGE_DIR = Path(__file__).resolve().parent

# User workspace — where tasks/ + _eval/ live. Defaults to CWD so users
# can run `code-loops new ...` from any project directory and keep
# task artifacts there. Override via $CODE_LOOPS_WORKSPACE.
WORKSPACE_DIR = Path(os.environ.get("CODE_LOOPS_WORKSPACE", Path.cwd()))
TASKS_DIR = WORKSPACE_DIR / "tasks"

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _slugify(text: str, max_len: int = 50) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return s[:max_len] or "task"


def _next_task_id(slug: str) -> str:
    TASKS_DIR.mkdir(exist_ok=True)
    nums: list[int] = []
    for d in TASKS_DIR.iterdir():
        if not d.is_dir():
            continue
        m = re.match(r"^(\d{4})_", d.name)
        if m:
            nums.append(int(m.group(1)))
    n = (max(nums) if nums else 0) + 1
    return f"{n:04d}_{slug}"


_POSTMORTEM_MARKERS = (
    "## Postmortem",
    "## Incident",
    "## Problem",
    "## What happened",
    "## Symptoms",
    "## Reproduction",
    # Bilingual support — markers also recognized when task.md is written in Russian.
    "## Проблема",
    "## Что произошло",
    "## Симптомы",
    "## Воспроизведение",
)


def _detect_mode(task_text: str, source_path: Path | None) -> str:
    """Decide between 'feature' and 'from_problem' from content + path heuristics.

    Used by `new` to tag meta.yaml for downstream analytics
    (pipeline-evaluator groups runs by mode). Business-analyst still
    detects mode independently from task content — this is a hint.
    """
    if source_path is not None:
        path_str = str(source_path).lower()
        if "problem" in path_str or "postmortem" in path_str or "incident" in path_str:
            return "from_problem"
    if any(marker in task_text for marker in _POSTMORTEM_MARKERS):
        return "from_problem"
    return "feature"


@app.command()
def new(
    task: str = typer.Argument(
        ...,
        help=(
            "Task input — either a path to a .md file (auto-loaded) or a "
            "free-text description (used as-is). Engine auto-detects which."
        ),
    ),
):
    """Create a new task folder and stage task.md.

    Examples:
        code-loops new "Add /export-data command"
        code-loops new path/to/problems/2026-05-06_timeout_incident.md
        code-loops new ~/notes/feature_idea.md
    """
    p = Path(task).expanduser()
    if p.exists() and p.is_file():
        task_text = p.read_text()
        slug = _slugify(p.stem)
        mode = _detect_mode(task_text, p)
        source_label = f"file: {p}"
    else:
        task_text = task
        slug = _slugify(task)
        mode = _detect_mode(task_text, None)
        source_label = "string"

    task_id = _next_task_id(slug)
    task_dir = TASKS_DIR / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.md").write_text(task_text)
    MetaStore(task_dir / "meta.yaml").init_task(task_id, mode)
    Manifest(task_dir / "manifest.json").init_task(task_id, mode)

    console.print(f"Created task: [bold cyan]{task_id}[/bold cyan]")
    console.print(f"  Source: {source_label}")
    console.print(f"  Path: {task_dir}")
    console.print(f"  Mode: {mode}")
    console.print(f"\nRun: [yellow]uv run code-loops run {task_id}[/yellow]")


SURVEYOR_PROMPT_PATH = PACKAGE_DIR / "agents" / "meta" / "project-surveyor.md"


def _invoke_surveyor(project_name: str, base_repo: Path, target_dir: Path) -> None:
    """Invoke project-surveyor agent: scans base_repo, writes brief.md via Write tool.

    Used by both `init` (first-time bootstrap) and `resurvey` (refresh).
    Cost ~$0.30-3.00 per call (Opus on max effort scanning a typical repo —
    actual cost scales with project size).

    The agent's CWD is base_repo so its Read/Grep/Glob default to scanning
    the target project. The brief output path is passed as an absolute path
    in the user message so the agent's Write tool lands the file in
    code-loops/projects/<name>/brief.md regardless of CWD.
    """
    brief_path = target_dir / "brief.md"

    if not SURVEYOR_PROMPT_PATH.exists():
        console.print(
            f"[yellow]project-surveyor agent not found at {SURVEYOR_PROMPT_PATH}; "
            "writing placeholder brief.md instead.[/yellow]"
        )
        brief_path.write_text(
            f"# Project Brief: {project_name}\n\n"
            "_TODO: surveyor agent missing — fill in manually._\n"
            f"\n**Repo**: `{base_repo}`\n"
        )
        return

    console.print(
        f"[dim]invoking project-surveyor on {base_repo} "
        "(Opus max, expect ~$0.30-3.00, 1-6 min)[/dim]"
    )
    sys_prompt = SURVEYOR_PROMPT_PATH.read_text()
    user_msg = (
        f"=== Survey project ===\n"
        f"name: {project_name}\n"
        f"base_repo: {base_repo}\n"
        f"output_path: {brief_path}\n\n"
        f"Scan the project and use the Write tool to save the brief to the "
        f"absolute output_path: {brief_path}\n"
        f"Do NOT write anywhere else. Do NOT modify the target project."
    )
    # Snapshot mtime so we can verify the file was actually written this run
    # (not stale from a prior run).
    pre_mtime = brief_path.stat().st_mtime if brief_path.exists() else 0.0

    factory = RunnerFactory()
    runner = factory.make({"model": "claude-opus-4-7", "effort": "max"})
    result = runner.run(sys_prompt, user_msg, cwd=str(base_repo))

    if not brief_path.exists():
        console.print(
            f"[red]✗ Surveyor finished but brief.md not found at {brief_path}.[/red]\n"
            f"Response started with: {result.text.strip()[:200]!r}\n"
            f"Possible cause: agent wrote to wrong path. Check inside "
            f"{base_repo} for a stray brief.md and move it manually."
        )
        raise typer.Exit(2)
    if brief_path.stat().st_mtime <= pre_mtime:
        console.print(
            f"[red]✗ Surveyor finished but brief.md at {brief_path} was not "
            f"updated (mtime unchanged).[/red]"
        )
        raise typer.Exit(2)

    size = brief_path.stat().st_size
    line_count = sum(1 for _ in brief_path.open())
    cost_str = f"${result.cost_usd or 0:.2f}"
    console.print(
        f"[green]✓[/green] Wrote {brief_path} ({line_count} lines, "
        f"{size} bytes, {result.duration_s:.0f}s, {cost_str})"
    )


@app.command()
def init(
    project_path: str = typer.Argument(..., help="Absolute path to target project repo"),
    name: str = typer.Option(
        None,
        "--name",
        "-n",
        help="Short name for the project under projects/<name>/. Defaults to dir basename.",
    ),
    no_survey: bool = typer.Option(
        False,
        "--no-survey",
        help="Skip the project-surveyor LLM call; write a placeholder brief.md instead.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing projects/<name>/ if present.",
    ),
):
    """Bootstrap a target project for code-loops.

    Creates projects/<name>/{project.yaml,brief.md}. Invokes the
    project-surveyor agent to scan the repo and auto-write brief.md
    (architecture, conventions, glossary, key modules). Pass --no-survey
    to skip the LLM call.
    """
    proj_path = Path(project_path).expanduser().resolve()
    if not proj_path.exists():
        console.print(f"[red]Project path does not exist: {proj_path}[/red]")
        raise typer.Exit(1)
    if not (proj_path / ".git").exists():
        console.print(f"[red]Not a git repository (no .git/ at {proj_path})[/red]")
        raise typer.Exit(1)

    project_name = name or proj_path.name
    target_dir = PROJECTS_DIR / project_name
    if target_dir.exists() and not force:
        console.print(
            f"[red]Project already exists: {target_dir}[/red]\n"
            f"Use [yellow]--force[/yellow] to overwrite, or "
            f"[yellow]code-loops resurvey {project_name}[/yellow] to refresh brief.md only."
        )
        raise typer.Exit(1)

    target_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "project": {
            "name": project_name,
            "base_repo": str(proj_path),
        },
        "brief_file": "brief.md",
    }
    config_path = target_dir / "project.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    console.print(f"[green]✓[/green] Created {config_path}")

    if no_survey:
        (target_dir / "brief.md").write_text(
            f"# Project Brief: {project_name}\n\n"
            "_Placeholder — run `code-loops resurvey "
            f"{project_name}` to invoke project-surveyor._\n"
            f"\n**Repo**: `{proj_path}`\n"
        )
        console.print(
            f"[yellow]✓[/yellow] Wrote placeholder {target_dir / 'brief.md'} (--no-survey)."
        )
    else:
        _invoke_surveyor(project_name, proj_path, target_dir)

    console.print(
        f"\n[bold]Next:[/bold] [yellow]code-loops --project {project_name} run <task_id>[/yellow]"
    )
    console.print(f"To refresh brief.md: [yellow]code-loops resurvey {project_name}[/yellow]")


@app.command()
def resurvey(name: str):
    """Re-run project-surveyor on an existing project (overwrites brief.md)."""
    target_dir = PROJECTS_DIR / name
    if not target_dir.exists():
        available = list_projects()
        avail_str = ", ".join(available) if available else "(none)"
        console.print(
            f"[red]Project not found: {name}[/red]\n"
            f"Available: {avail_str}\n"
            f"Run [yellow]code-loops init <path-to-project>[/yellow] first."
        )
        raise typer.Exit(1)

    config_path = target_dir / "project.yaml"
    config = yaml.safe_load(config_path.read_text())
    base_repo = Path(config["project"]["base_repo"])
    if not base_repo.exists():
        console.print(
            f"[red]base_repo no longer exists: {base_repo}. Edit {config_path} or re-init.[/red]"
        )
        raise typer.Exit(1)

    _invoke_surveyor(name, base_repo, target_dir)


@app.command(name="projects")
def projects_cmd():
    """List configured target projects."""
    projects = list_projects()
    if not projects:
        console.print(
            "[dim]No projects configured. Run [yellow]code-loops init <path>[/yellow] "
            "to bootstrap one.[/dim]"
        )
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("name")
    table.add_column("base_repo")
    table.add_column("brief")
    for name in projects:
        cfg_path = PROJECTS_DIR / name / "project.yaml"
        cfg = yaml.safe_load(cfg_path.read_text())
        base = (cfg.get("project") or {}).get("base_repo", "?")
        brief_path = PROJECTS_DIR / name / "brief.md"
        brief_marker = "✓ present" if brief_path.exists() else "[yellow]missing[/yellow]"
        table.add_row(name, base, brief_marker)
    console.print(table)


@app.command()
def run(
    task_id: str,
    project: str = typer.Option(
        None,
        "--project",
        help=(
            "Named project under projects/<name>/project.yaml. If only one "
            "project exists in projects/, it's auto-selected — use this flag "
            "only when you have multiple projects configured."
        ),
    ),
    project_config: str = typer.Option(
        None,
        "--project-config",
        help=(
            "Explicit path to a project.yaml file (overrides --project and "
            "the autoselect behavior)."
        ),
    ),
    from_stage: str = typer.Option(
        None,
        "--from-stage",
        help=(
            "Force-restart from this stage (clears its done-marker and all "
            "downstream stages in meta.yaml). Use after a manual edit to a "
            "stage's output, or to recover from `redesign_loops_exceeded` "
            "by running `--from-stage impl_plan`."
        ),
    ),
):
    """Run / resume a task through the pipeline."""
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    project_config_path = Path(project_config) if project_config else None
    Engine(
        task_dir,
        project_config_path=project_config_path,
        project_name=project,
        from_stage=from_stage,
    ).run()


@app.command()
def status(task_id: str):
    """Show task status."""
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    meta = MetaStore(task_dir / "meta.yaml")
    d = meta.data
    console.print(
        f"[bold cyan]{d.get('task_id')}[/bold cyan]  status={d.get('status')}  cost=${d.get('cost_usd', 0):.4f}"
    )
    console.print(f"  mode: {d.get('mode')}")
    console.print(f"  current_stage: {d.get('current_stage')}")
    table = Table(show_header=True, header_style="bold")
    table.add_column("stage")
    table.add_column("status")
    table.add_column("attempts")
    table.add_column("duration_s")
    table.add_column("cost_usd")
    for name, st in (d.get("stages") or {}).items():
        table.add_row(
            name,
            str(st.get("status")),
            str(st.get("attempts", "")),
            str(st.get("duration_s", "")),
            f"${st.get('cost_usd', 0):.4f}" if st.get("cost_usd") else "",
        )
    console.print(table)


@app.command()
def cancel(task_id: str):
    """Mark task cancelled (artifacts preserved)."""
    task_dir = TASKS_DIR / task_id
    MetaStore(task_dir / "meta.yaml").set_status("cancelled")
    console.print(f"[yellow]Task {task_id} cancelled.[/yellow]")


@app.command()
def commit(
    task_id: str,
    project: str = typer.Option(
        None,
        "--project",
        help=(
            "Named project under projects/<name>/project.yaml. If only one "
            "project exists, it's auto-selected."
        ),
    ),
    project_config: str = typer.Option(
        None,
        "--project-config",
        help="Explicit path to a project.yaml file (overrides --project).",
    ),
):
    """Print summary + push instructions for a completed task's branch."""
    task_dir = TASKS_DIR / task_id
    if not task_dir.exists():
        console.print(f"[red]Task not found: {task_id}[/red]")
        raise typer.Exit(1)
    meta = MetaStore(task_dir / "meta.yaml").data
    branch = f"code-loops/{task_id}"
    worktree = task_dir / "worktree" / "wt"

    project_config_path = Path(project_config) if project_config else None
    config = load_project_config(project_config_path, name=project)
    base_repo = Path(config["project"]["base_repo"])

    console.print(
        f"[bold cyan]Task {task_id}[/bold cyan]  status={meta.get('status')}  cost=${meta.get('cost_usd', 0):.2f}"
    )
    console.print(f"  Branch (in {base_repo}): [yellow]{branch}[/yellow]")
    console.print(f"  Worktree: [yellow]{worktree}[/yellow]")

    final_verdict = task_dir / "release_review" / "verdict.md"
    if final_verdict.exists():
        console.print("\n[bold]Release review:[/bold]")
        console.print(final_verdict.read_text())

    final_loop_count = meta.get("final_loop_count", 0)
    redesign_loop_count = meta.get("redesign_loop_count", 0)
    if final_loop_count or redesign_loop_count:
        console.print(f"  loops: final={final_loop_count}, redesign={redesign_loop_count}")

    console.print("\n[bold]Suggested next steps:[/bold]")
    console.print(f"  cd {base_repo}")
    console.print(f"  git log --oneline -20 {branch}")
    console.print(f"  git push -u origin {branch}")
    console.print(f"  gh pr create --base main --head {branch}")
    console.print(
        "\n  [dim](push is left manual — review the diff first with "
        "`git diff main..." + branch + "`)[/dim]"
    )


@app.command(name="list")
def list_tasks():
    """List all tasks."""
    if not TASKS_DIR.exists():
        console.print("[dim]No tasks yet.[/dim]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("task_id")
    table.add_column("status")
    table.add_column("mode")
    table.add_column("cost")
    rows = 0
    for task_dir in sorted(TASKS_DIR.iterdir()):
        if not task_dir.is_dir():
            continue
        meta = MetaStore(task_dir / "meta.yaml")
        d = meta.data
        table.add_row(
            d.get("task_id", task_dir.name),
            str(d.get("status", "")),
            str(d.get("mode", "")),
            f"${d.get('cost_usd', 0):.4f}",
        )
        rows += 1
    if rows == 0:
        console.print("[dim]No tasks yet.[/dim]")
    else:
        console.print(table)


@app.command(name="eval")
def evaluate(
    last: int = typer.Option(20, "--last", help="Number of recent runs to aggregate"),
    focus: str = typer.Option(None, "--focus", help="Specific question for pipeline-evaluator"),
):
    """Run pipeline-evaluator (meta-pipeline evaluation) over recent task runs.

    Aggregates per-stage stats / cost trends / convergence rates from
    `tasks/*/meta.yaml`, includes recent `git log` of `agents/` for A/B
    framing, and invokes pipeline-evaluator.md to produce a trend report at
    `_eval/report_<timestamp>.md`.
    """
    if not TASKS_DIR.exists():
        console.print("[red]No tasks/ directory yet — run a task first[/red]")
        raise typer.Exit(1)

    aggregation = aggregate_recent_runs(TASKS_DIR, last_n=last)
    if aggregation["total_runs"] == 0:
        console.print("[yellow]No completed runs to evaluate yet[/yellow]")
        raise typer.Exit(0)

    by_status_str = ", ".join(f"{k}={v}" for k, v in sorted(aggregation["by_status"].items()))
    console.print(
        f"Aggregated [bold cyan]{aggregation['total_runs']}[/bold cyan] runs ({by_status_str})"
    )

    agent_log = get_recent_agent_changes(PACKAGE_DIR)
    user_msg = build_eval_message(aggregation, agent_log, focus)

    sys_prompt_path = PACKAGE_DIR / "agents" / "meta" / "pipeline-evaluator.md"
    if not sys_prompt_path.exists():
        console.print(f"[red]pipeline-evaluator.md not found at {sys_prompt_path}[/red]")
        raise typer.Exit(1)
    sys_prompt = sys_prompt_path.read_text()

    factory = RunnerFactory()
    runner = factory.make({"model": "claude-opus-4-7", "effort": "max"})

    console.print(
        "[dim]eval:[/dim] invoking pipeline-evaluator on Opus max — expect ~$1-3 / 1-3 min"
    )
    result = runner.run(sys_prompt, user_msg)

    eval_dir = WORKSPACE_DIR / "_eval"
    eval_dir.mkdir(exist_ok=True)
    today = datetime.now(UTC).strftime("%Y-%m-%d_%H%M%S")
    out_path = eval_dir / f"report_{today}.md"
    out_path.write_text(result.text)

    cost_str = f"${result.cost_usd:.4f}" if result.cost_usd else "—"
    console.print(f"[green]✓ Eval report:[/green] {out_path}")
    console.print(f"  Cost: {cost_str}  Duration: {result.duration_s:.0f}s")


if __name__ == "__main__":
    app()
