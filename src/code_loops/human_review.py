"""Human review checkpoint with revise-with-comment loop.

Detects non-interactive sessions and auto-approves (for smoke tests / CI).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

console = Console()


@dataclass
class ReviewResult:
    action: str  # 'approve' | 'revise' | 'abort'
    comment: str | None = None


def review(stage_name: str, outputs: dict[str, Path], summary: str | None = None) -> ReviewResult:
    """Show outputs and prompt user for action.

    `outputs` is a mapping of {relative_path: absolute_path}.
    `summary` is an optional pre-formatted block (e.g. parsed subtasks table).
    """
    if not sys.stdin.isatty():
        console.print(
            f"[yellow]Non-interactive session: auto-approving stage `{stage_name}`[/yellow]"
        )
        return ReviewResult(action="approve")

    body = f"Stage [bold cyan]{stage_name}[/bold cyan] finished.\n\n"
    if summary:
        body += summary + "\n\n"
    body += "Outputs:\n"
    for rel_path, abs_path in outputs.items():
        size = abs_path.stat().st_size if abs_path.exists() else 0
        body += f"  • {rel_path} ({size} bytes)\n"
    body += (
        "\n[a] approve & continue   [r] revise with comment   [v] view full artifacts   [x] abort"
    )

    console.print(Panel(body, title="Human Review", border_style="cyan"))

    while True:
        choice = Prompt.ask("Action", choices=["a", "r", "v", "x"], default="a").lower()
        if choice == "a":
            return ReviewResult(action="approve")
        if choice == "x":
            return ReviewResult(action="abort")
        if choice == "v":
            for rel_path, abs_path in outputs.items():
                console.rule(rel_path)
                if abs_path.exists():
                    console.print(abs_path.read_text())
                else:
                    console.print(f"[red]missing: {abs_path}[/red]")
            continue
        if choice == "r":
            comment = _get_comment_via_editor(stage_name)
            if not comment.strip():
                console.print("[yellow]Empty comment, no revise triggered.[/yellow]")
                continue
            return ReviewResult(action="revise", comment=comment)


def _get_comment_via_editor(stage_name: str) -> str:
    editor = os.environ.get("EDITOR", "vi")
    template = (
        f"# Feedback for stage `{stage_name}`\n"
        "# Lines starting with # will be ignored.\n"
        "#\n"
        "# What should be revised? Be specific.\n"
        "\n"
    )
    fd, path_str = tempfile.mkstemp(suffix=".md", prefix=f"review_{stage_name}_")
    os.close(fd)
    path = Path(path_str)
    try:
        path.write_text(template)
        subprocess.call([editor, str(path)])
        content = path.read_text()
        lines = [ln for ln in content.splitlines() if not ln.strip().startswith("#")]
        return "\n".join(lines).strip()
    finally:
        path.unlink(missing_ok=True)
