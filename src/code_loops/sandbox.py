"""Per-invocation Claude Code permission sandbox.

Generates `.claude/settings.local.json` в worktree just before launching editor
subprocess. Permission engine refuses out-of-scope tool calls (Write/Edit beyond
subtask.files, dangerous Bash). Editor получает tool error → fix or emit STUCK.

Replaces 3 layers of post-hoc enforcement (chmod 444 tests, git-diff guard,
RoleScopeViolation rollback) с pre-execution mechanical prevention. F2/F4/F5
failure modes structurally closed.

Settings reference: https://docs.claude.com/en/docs/claude-code/iam#permission-rules
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

SETTINGS_RELATIVE = ".claude/settings.local.json"

# Tools editor is allowed to use. Anything else triggers permission denial.
DEFAULT_ALLOWED_TOOLS: list[str] = [
    "Read",     # full read access (no sandbox needed для reading)
    "Glob",     # pattern matching
    "Grep",     # content search
    "Edit",     # editing — но restricted к scope via deny rules
    "Write",    # writing — но restricted к scope via deny rules
    "Bash",     # restricted к safe commands via deny rules
]


def build_settings(
    allowed_write_paths: list[str],
    extra_deny: list[str] | None = None,
) -> dict:
    """Compose settings.local.json content из subtask scope.

    `allowed_write_paths` — relative paths (or globs) where Write/Edit allowed.
    Everything else under `wt.path/**` denied. Read tools unrestricted.

    Deny rules use Claude Code's pattern syntax: `Write(<glob>)` / `Edit(<glob>)`.
    Deny is evaluated before allow, so we deny everything by default и allow
    specific paths via inversion (Claude permission system supports `allow` overrides).
    """
    # Allow specific paths for Write/Edit; deny everything else
    allow_rules = []
    for p in allowed_write_paths:
        allow_rules.append(f"Write({p})")
        allow_rules.append(f"Edit({p})")
    # Block dangerous Bash patterns regardless of scope
    deny_bash = [
        "Bash(rm -rf *)",
        "Bash(git push *)",
        "Bash(git reset --hard *)",
        "Bash(curl * | sh*)",
        "Bash(curl * | bash*)",
        "Bash(wget * | sh*)",
        "Bash(wget * | bash*)",
    ]
    if extra_deny:
        deny_bash.extend(extra_deny)
    return {
        "permissions": {
            # Allow Write/Edit ONLY к declared scope. Anything outside →
            # permission prompt → in non-interactive mode = tool error.
            "allow": allow_rules,
            # Hard-block dangerous Bash. Other Bash invocations still allowed
            # but tracked via tool_events.
            "deny": deny_bash,
        }
    }


def install_settings(wt_path: Path, allowed_write_paths: list[str]) -> Path:
    """Write settings.local.json into worktree. Returns full path."""
    settings_path = wt_path / SETTINGS_RELATIVE
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    content = build_settings(allowed_write_paths)
    settings_path.write_text(json.dumps(content, indent=2, sort_keys=True) + "\n")
    return settings_path


def uninstall_settings(wt_path: Path) -> None:
    """Remove settings.local.json after run."""
    settings_path = wt_path / SETTINGS_RELATIVE
    if settings_path.exists():
        settings_path.unlink()


@contextmanager
def scoped_sandbox(wt_path: Path, allowed_write_paths: list[str]):
    """Context manager — installs settings on enter, removes on exit.

    Use around runner.run() call for editor role. Other roles (planner/reviewer)
    don't need sandbox — they're read-only or wrapped в different flow.
    """
    install_settings(wt_path, allowed_write_paths)
    try:
        yield
    finally:
        uninstall_settings(wt_path)
