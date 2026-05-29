"""Load target-project config — keeps code-loops decoupled from any specific project.

Layout (lives INSIDE code-loops repo, gitignored under projects/):

    code-loops/
    └── projects/
        └── <project-name>/
            ├── project.yaml          ← name + base_repo + brief_file ref
            └── brief.md              ← project-specific knowledge (auto-generated
                                        by project-surveyor agent; read by agents
                                        that need project context)

Resolution order:
1. Explicit `path` argument (typically from CLI flag --project-config)
2. CLI flag --project <name>  →  PROJECTS_DIR / <name> / project.yaml
3. CODE_LOOPS_PROJECT env var (absolute path to a project.yaml file)
4. Auto-select: if exactly ONE project exists under projects/, use it
5. Returns None — the engine then falls back to legacy stage_def values
   for stages that can tolerate missing config; subtask_iterator raises
   a clear error pointing the user at `code-loops init`.

Schema (project.yaml is universal, no project-specific keys):

    project:
      name: <free-form for logs>
      base_repo: <absolute path>
    brief_file: <relative-or-absolute path; default "brief.md" next to project.yaml>
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# Per-project state lives in the user's workspace directory (defaults
# to CWD; overridable via $CODE_LOOPS_WORKSPACE). Each subdirectory =
# one target project.
PROJECTS_DIR = Path(os.environ.get("CODE_LOOPS_WORKSPACE", Path.cwd())) / "projects"


def load_project_config(
    path: Path | None = None,
    *,
    name: str | None = None,
) -> dict | None:
    """Load target-project YAML. Returns None if nothing resolves.

    `path` wins over `name` wins over env var wins over auto-select.
    """
    resolved = _resolve_path(path, name)
    if resolved is None or not resolved.exists():
        return None
    data = yaml.safe_load(resolved.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"project config at {resolved} must parse to a YAML mapping at top level")
    # Annotate with the source path so callers can resolve brief_file relative to it.
    data["_source_path"] = str(resolved)
    return data


def _resolve_path(path: Path | None, name: str | None) -> Path | None:
    if path is not None:
        return path
    if name:
        return PROJECTS_DIR / name / "project.yaml"
    env = os.environ.get("CODE_LOOPS_PROJECT")
    if env:
        return Path(env)
    # Auto: single-project default. Multi-project requires explicit choice —
    # silently degrading to "no project" caused agents to run without
    # {PROJECT_BRIEF} during Step 10's first production run; loud error here
    # surfaces the ambiguity at startup instead of mid-pipeline.
    if PROJECTS_DIR.is_dir():
        candidates = sorted(
            d for d in PROJECTS_DIR.iterdir() if d.is_dir() and (d / "project.yaml").exists()
        )
        if len(candidates) == 1:
            return candidates[0] / "project.yaml"
        if len(candidates) > 1:
            names = ", ".join(c.name for c in candidates)
            raise ValueError(
                f"Multiple projects configured ({names}) and no --project / "
                f"--project-config / $CODE_LOOPS_PROJECT specified. Pass one "
                f"explicitly so agents get the correct {{PROJECT_BRIEF}}."
            )
    return None


def list_projects() -> list[str]:
    """Names of projects currently configured under projects/."""
    if not PROJECTS_DIR.is_dir():
        return []
    return sorted(
        d.name for d in PROJECTS_DIR.iterdir() if d.is_dir() and (d / "project.yaml").exists()
    )


def get_base_repo(project_config: dict | None) -> Path | None:
    """Convenience accessor for project.base_repo."""
    if not project_config:
        return None
    project = project_config.get("project") or {}
    base = project.get("base_repo")
    return Path(base) if base else None


def get_brief_path(project_config: dict | None) -> Path | None:
    """Resolve brief_file (relative to project.yaml) → absolute path. None if absent."""
    if not project_config:
        return None
    brief = project_config.get("brief_file")
    if not brief:
        return None
    brief_path = Path(brief)
    if brief_path.is_absolute():
        return brief_path
    source = project_config.get("_source_path")
    if not source:
        return None
    return Path(source).parent / brief_path


_DEFAULT_TEST_INFRA = {
    "enabled": True,
    "test_paths": ["tests"],
    "lock_strategy": "chmod_444_dir",
}
_VALID_LOCK_STRATEGIES = {"chmod_444_dir", "none"}


def get_test_infrastructure(project_config: dict | None) -> dict:
    """Return test_infrastructure config with defaults filled in.

    Default (preserves prior behavior):
        {enabled: True, test_paths: ["tests"], lock_strategy: "chmod_444_dir"}

    Schema:
    - enabled: bool. False → subtask_iterator skips test_writer entirely
      (project has no automated test infrastructure to lock).
    - test_paths: list[str]. Directories the coder MUST NOT touch — locked
      between test_writer and coder runs. Relative to base_repo / worktree
      root. Empty list disables locking even if enabled=True.
    - lock_strategy:
      - "chmod_444_dir" — chmod 444 the entire test_paths dir trees.
      - "none" — no chmod; locking is a no-op (project has tests but
        relies on convention / git diff guard alone).
    """
    if not project_config:
        return dict(_DEFAULT_TEST_INFRA)
    raw = project_config.get("test_infrastructure") or {}
    out = dict(_DEFAULT_TEST_INFRA)
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "test_paths" in raw:
        paths = raw["test_paths"]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("test_infrastructure.test_paths must be a list of strings")
        out["test_paths"] = paths
    if "lock_strategy" in raw:
        strat = raw["lock_strategy"]
        if strat not in _VALID_LOCK_STRATEGIES:
            raise ValueError(
                f"test_infrastructure.lock_strategy must be one of "
                f"{sorted(_VALID_LOCK_STRATEGIES)}, got {strat!r}"
            )
        out["lock_strategy"] = strat
    return out


_DEFAULT_REGRESSION = {
    "enabled": False,
    "command": None,
    "output_path": None,
    "threshold_pct": 5,
}


def get_regression_config(project_config: dict | None) -> dict:
    """Return regression-check config with defaults filled in.

    Default: {enabled: False, ...} — Stage 8.5 is a no-op for projects
    that don't opt in.

    Schema (under project.yaml `regression:`):
    - enabled: bool. False → regression_check stage skips entirely.
    - command: str | None. Shell-style argv joined with spaces, run in the
      worktree (e.g. "uv run pytest -m eval --json"). The command must
      write a JSON file at `output_path` containing `{metric: value, ...}`
      where higher = better.
    - output_path: str | None. Relative path under the worktree where the
      bench writes its results JSON (e.g. "tests/eval_results.json").
    - threshold_pct: int. Max allowed drop per metric before this stage
      flags regression. e.g. 5 → fail if `current < baseline * 0.95`.

    First run with no saved baseline: captures the current results as
    the new baseline and passes (writes `projects/<name>/baselines/eval.json`).
    """
    if not project_config:
        return dict(_DEFAULT_REGRESSION)
    raw = project_config.get("regression") or {}
    out = dict(_DEFAULT_REGRESSION)
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    if "command" in raw:
        cmd = raw["command"]
        if cmd is not None and not isinstance(cmd, str):
            raise ValueError("regression.command must be a string or null")
        out["command"] = cmd
    if "output_path" in raw:
        op = raw["output_path"]
        if op is not None and not isinstance(op, str):
            raise ValueError("regression.output_path must be a string or null")
        out["output_path"] = op
    if "threshold_pct" in raw:
        t = raw["threshold_pct"]
        if not isinstance(t, int | float) or t < 0 or t > 100:
            raise ValueError("regression.threshold_pct must be a number in [0, 100]")
        out["threshold_pct"] = t
    return out


def read_brief(project_config: dict | None) -> str | None:
    """Read brief.md content if present. None if absent / unreadable."""
    p = get_brief_path(project_config)
    if not p or not p.exists():
        return None
    return p.read_text()


_BRIEF_PLACEHOLDER = "{PROJECT_BRIEF}"
_BRIEF_FALLBACK = (
    "_(no project brief configured — agent operating without project "
    "context. Run `code-loops init <path>` or `code-loops resurvey <name>` "
    "to generate brief.md, or this agent will rely only on its built-in "
    "general guidance.)_"
)


def get_test_env_stub(project_config: dict | None) -> dict[str, str]:
    """Return key=value pairs to write into worktree .env for test runs.

    Values are fake stubs — just enough for Settings() to initialize at
    import time without real secrets. Real tokens must never go here.
    Returns empty dict if not configured (worktree gets no .env).
    """
    if not project_config:
        return {}
    raw = project_config.get("test_env_stub") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def inject_project_brief(prompt_text: str, project_config: dict | None) -> str:
    """Replace `{PROJECT_BRIEF}` placeholder in prompt with brief.md content.

    No-op if the placeholder isn't present. If the placeholder IS present
    but brief.md is missing/unreadable, replace with an explanatory note
    so the agent knows it's running without project context (rather than
    silently leaving the literal `{PROJECT_BRIEF}` string in the prompt).
    """
    if _BRIEF_PLACEHOLDER not in prompt_text:
        return prompt_text
    brief = read_brief(project_config)
    return prompt_text.replace(_BRIEF_PLACEHOLDER, brief or _BRIEF_FALLBACK)
