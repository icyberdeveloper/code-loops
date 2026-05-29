"""SubtaskExecutorStage — Aider-style architect/editor pivot.

Replaces 7-role implementation (eval_engineer + qa + coder + reviewer + triage +
replanner + dataset_curator + prompt_engineer) с single editor role per subtask.

Per-subtask cycle:
   editor → validator (mode-aware + acceptance checks) → ship | retry | replan

Source of truth:
- Tech-lead already produced subtask spec (stage 6) — that IS the architect.
- Editor (Sonnet/Opus) executes spec into code.
- Validator (deterministic, no LLM) checks pytest + ruff + acceptance criteria.
- Replanner (only on STUCK or attempt exhaustion) revises subtask spec.

Sandbox: Claude Code permission engine refuses out-of-scope writes (sandbox.py).
Eliminates RoleScopeViolation/TestProtectionViolation/VFC band-aid layers.

Feature flag: `CODE_LOOPS_EXECUTOR=v2` selects this stage type;
default keeps legacy `subtask_iterator`. Switched in pipeline.yaml after cutover.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import yaml
from rich.console import Console

from ..acceptance import AcceptanceViolation, check_acceptance, format_violations
from ..project_loader import get_test_infrastructure
from ..runner import RunnerFactory
from ..sandbox import scoped_sandbox
from ..worktree import Worktree
from .prompt import StageContext, load_agent_prompt
from .role_normalizer import normalize_roles

console = Console()

MAX_EDITOR_ATTEMPTS = 3  # editor retries with feedback before replan
MAX_REPLAN_ATTEMPTS = 2  # replanner revises before escalating к design


class SubtaskExecutorError(RuntimeError):
    pass


class DesignEscalation(RuntimeError):
    """Editor + replanner can't satisfy spec → bubble к design."""

    def __init__(self, subtask_id: str, reason: str, feedback: str):
        self.subtask_id = subtask_id
        self.reason = reason
        self.feedback = feedback
        super().__init__(f"design escalation on {subtask_id}: {reason}")


def _subtask_files(subtask: dict) -> list[str]:
    """All files declared в subtask scope (create + modify)."""
    files_block = subtask.get("files") or {}
    out: list[str] = []
    for action in ("create", "modify"):
        out.extend(str(p) for p in (files_block.get(action) or []))
    return out


def _scope_test_files(subtask: dict) -> list[str]:
    return [f for f in _subtask_files(subtask) if f.endswith(".py") and "test" in f]


class SubtaskExecutorStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        wall_start = time.monotonic()
        cost_total = 0.0

        subtasks_yaml = ctx.task_dir / "impl_plan" / "subtasks.yaml"
        subtasks = yaml.safe_load(subtasks_yaml.read_text())["subtasks"]

        base_repo = self._resolve_base_repo(stage_def, ctx)
        worktree_root = ctx.task_dir / "worktree"
        worktree_root.mkdir(parents=True, exist_ok=True)
        branch = f"code-loops/{ctx.task_dir.name}"
        wt_path = worktree_root / "wt"

        if wt_path.exists() and (wt_path / ".git").exists():
            try:
                wt = Worktree.adopt(base_repo, branch, wt_path)
                console.print(f"  [dim]implementation:[/dim] adopted worktree at {wt_path}")
            except Exception as exc:
                console.print(
                    f"  [yellow]implementation:[/yellow] adopt failed ({exc}), recreating"
                )
                wt = Worktree.create(base_repo, branch, wt_path, force=True)
                wt.tag_base()
        else:
            wt = Worktree.create(base_repo, branch, wt_path, force=True)
            wt.tag_base()

        stage_def = {**stage_def, "roles": normalize_roles(stage_def["roles"])}
        roles = stage_def["roles"]

        for subtask in subtasks:
            sub_cost = self._execute_subtask(subtask, ctx, wt, roles)
            cost_total += sub_cost

        full_diff = wt.diff_vs_base()
        full_diff_path = ctx.task_dir / "implementation" / "_full_diff.patch"
        full_diff_path.parent.mkdir(parents=True, exist_ok=True)
        full_diff_path.write_text(full_diff)
        files_list = sorted(wt.files_vs_base())
        (ctx.task_dir / "implementation" / "_files_changed.txt").write_text(
            "\n".join(files_list) + "\n" if files_list else ""
        )

        return {
            "outputs": {
                "implementation/_full_diff.patch": full_diff,
                "implementation/_files_changed.txt": "\n".join(files_list),
            },
            "cost_usd": cost_total,
            "duration_s": time.monotonic() - wall_start,
            "subtasks_completed": len(subtasks),
            "worktree_path": str(wt_path),
        }

    @staticmethod
    def _resolve_base_repo(stage_def: dict, ctx: StageContext) -> Path:
        if ctx.project_config:
            project = ctx.project_config.get("project") or {}
            base = project.get("base_repo")
            if base:
                return Path(base)
        legacy = stage_def.get("base_repo")
        if legacy:
            return Path(legacy)
        raise SubtaskExecutorError("base_repo not configured")

    # ---- per-subtask cycle ----

    def _execute_subtask(self, subtask: dict, ctx, wt: Worktree, roles: dict) -> float:
        sid = subtask["id"]
        sub_dir = ctx.task_dir / "implementation" / sid
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "spec.md").write_text(self._render_spec(subtask))

        test_infra = get_test_infrastructure(ctx.project_config)
        cost_total = 0.0
        replan_count = 0
        editor_attempts = 0
        feedback: str | None = None

        console.print(f"  [bold cyan]▶ subtask {sid}[/bold cyan]: {subtask['title']}")
        console.print(
            f"  [dim]mode={subtask.get('mode', 'tdd')} "
            f"scope={len(_subtask_files(subtask))} files[/dim]"
        )

        # PRE-CHECK: if acceptance уже satisfied (e.g. subtask was shipped on
        # prior run, worktree adopted, OR corrective_subtasks duplicate existing
        # work), skip editor entirely. Saves $$ + avoids confusing editor с
        # "fix what's already fine". Worktree-based check survives manifest resets.
        pre_verdict = self._run_validator(wt, sub_dir, subtask, test_infra)
        if pre_verdict["passed"]:
            console.print(
                f"  [green]✓ subtask {sid}[/green] already satisfied — skipping editor "
                "(worktree state matches acceptance)"
            )
            if ctx.artifact_writer is not None:
                ctx.artifact_writer.manifest.set_subtask_final(sid, outcome="shipped")
            return 0.0

        while True:
            editor_attempts += 1
            if editor_attempts > MAX_EDITOR_ATTEMPTS:
                # Editor exhausted → replan
                if replan_count >= MAX_REPLAN_ATTEMPTS:
                    raise DesignEscalation(
                        sid,
                        f"editor exhausted после {MAX_EDITOR_ATTEMPTS} attempts × "
                        f"{MAX_REPLAN_ATTEMPTS} replans",
                        feedback or "no specific feedback",
                    )
                cost_rp, new_subtask = self._run_replanner(
                    roles, subtask, feedback or "editor attempts exhausted", wt, sub_dir, ctx
                )
                cost_total += cost_rp
                replan_count += 1
                if new_subtask:
                    subtask.update(new_subtask)
                    (sub_dir / f"spec_replan_{replan_count}.md").write_text(
                        self._render_spec(subtask)
                    )
                editor_attempts = 0
                feedback = None
                continue

            # 1. Editor (sandboxed)
            cost_ed, status, response = self._run_editor(
                roles, subtask, wt, sub_dir, ctx, feedback, editor_attempts
            )
            cost_total += cost_ed

            if status == "STUCK":
                # Editor self-reports inability → replan immediately
                if replan_count >= MAX_REPLAN_ATTEMPTS:
                    raise DesignEscalation(
                        sid, "editor STUCK + replan budget exhausted", response
                    )
                cost_rp, new_subtask = self._run_replanner(
                    roles, subtask, response, wt, sub_dir, ctx
                )
                cost_total += cost_rp
                replan_count += 1
                if new_subtask:
                    subtask.update(new_subtask)
                    (sub_dir / f"spec_replan_{replan_count}.md").write_text(
                        self._render_spec(subtask)
                    )
                editor_attempts = 0
                feedback = None
                continue

            # 2. Validator (mode-aware + acceptance checks)
            verdict = self._run_validator(wt, sub_dir, subtask, test_infra)
            if verdict["passed"]:
                console.print(f"  [green]✓ subtask {sid}[/green] passed validator + acceptance")
                if ctx.artifact_writer is not None:
                    ctx.artifact_writer.manifest.set_subtask_final(sid, outcome="shipped")
                return cost_total

            # Failed → retry editor с feedback
            feedback = self._build_feedback(verdict)
            console.print(
                f"  [yellow]↻ subtask {sid}[/yellow]: validator failed "
                f"(editor attempt {editor_attempts}/{MAX_EDITOR_ATTEMPTS})"
            )

    # ---- role invocations ----

    def _run_editor(
        self, roles, subtask, wt, sub_dir, ctx, feedback, attempt_n
    ) -> tuple[float, str, str]:
        cfg = roles.get("editor")
        if not cfg:
            raise SubtaskExecutorError("`editor` role not configured в pipeline.yaml")
        runner = self.factory.make(cfg)
        sys_prompt = load_agent_prompt(ctx.repo_root / cfg["prompt"], ctx)
        user_msg = self._build_editor_input(subtask, wt, feedback)
        allowed = _subtask_files(subtask)
        with scoped_sandbox(wt.path, allowed):
            result = runner.run(sys_prompt, user_msg, cwd=str(wt.path))
        (sub_dir / f"editor_attempt_{attempt_n}.md").write_text(result.text)
        # Detect STUCK signal
        status = "STUCK" if re.search(r"^\s*STUCK\s*$", result.text, re.MULTILINE) else "DONE"
        return result.cost_usd or 0, status, result.text

    def _run_replanner(
        self, roles, subtask, failure_context: str, wt, sub_dir, ctx
    ) -> tuple[float, dict | None]:
        cfg = roles.get("replanner")
        if not cfg:
            console.print("  [yellow]no replanner role — skipping spec revision[/yellow]")
            return 0.0, None
        runner = self.factory.make(cfg)
        sys_prompt = load_agent_prompt(ctx.repo_root / cfg["prompt"], ctx)
        user_msg = (
            f"=== current_subtask ===\n```yaml\n"
            f"{yaml.safe_dump(subtask, sort_keys=False, allow_unicode=True)}```\n\n"
            f"=== failure_context ===\n{failure_context}\n\n"
            f"=== worktree path ===\n{wt.path}\n"
        )
        result = runner.run(sys_prompt, user_msg, cwd=str(wt.path))
        (sub_dir / "replanner_response.md").write_text(result.text)
        # Parse revised YAML
        m = re.search(r"```yaml\s*\n(.+?)\n```", result.text, re.DOTALL)
        if not m:
            return result.cost_usd or 0, None
        try:
            parsed = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            return result.cost_usd or 0, None
        if isinstance(parsed, list) and parsed:
            return result.cost_usd or 0, parsed[0]
        if isinstance(parsed, dict):
            return result.cost_usd or 0, parsed
        return result.cost_usd or 0, None

    # ---- validator (mode-aware) ----

    def _run_validator(self, wt: Worktree, sub_dir: Path, subtask: dict, test_infra: dict) -> dict:
        mode = subtask.get("mode", "tdd")
        scope_files = _subtask_files(subtask)
        scope_test = _scope_test_files(subtask) or scope_files
        scope_py = [f for f in scope_files if f.endswith(".py")]

        sub_dir_validation = sub_dir / "validation"
        sub_dir_validation.mkdir(parents=True, exist_ok=True)

        # pytest — mode determines what "passed" means
        pytest_args = ["uv", "run", "pytest"]
        if mode == "tdd":
            pytest_args.append("-x")  # stop on first failure
        pytest_args.extend(scope_test)
        proc = subprocess.run(
            pytest_args, cwd=str(wt.path), capture_output=True, text=True, timeout=180
        )
        pytest_log = proc.stdout + proc.stderr
        (sub_dir_validation / "pytest.log").write_text(pytest_log)
        pytest_rc = proc.returncode

        # ruff
        ruff_log = ""
        ruff_rc = 0
        if scope_py:
            r = subprocess.run(
                ["uv", "run", "ruff", "check", *scope_py],
                cwd=str(wt.path),
                capture_output=True,
                text=True,
            )
            ruff_log = r.stdout + r.stderr
            ruff_rc = r.returncode
            (sub_dir_validation / "ruff.log").write_text(ruff_log)

        # Acceptance criteria — mechanical
        acceptance = subtask.get("acceptance") or []
        acceptance_violations: list[AcceptanceViolation] = check_acceptance(
            acceptance, wt.path, scope_files=scope_test
        )

        # Mode-aware pass condition
        # - tdd: pytest_rc=0 AND ruff_rc=0 AND no acceptance violations
        # - baseline: ruff_rc=0 AND no acceptance violations (pytest failures OK if acceptance specifies)
        # - refactor: pytest_rc=0 AND ruff_rc=0
        # - hotfix: pytest_rc=0 AND ruff_rc=0 AND no acceptance violations
        if mode == "baseline":
            passed = ruff_rc == 0 and not acceptance_violations
        else:
            passed = pytest_rc == 0 and ruff_rc == 0 and not acceptance_violations

        return {
            "passed": passed,
            "mode": mode,
            "pytest_rc": pytest_rc,
            "pytest_log": pytest_log,
            "ruff_rc": ruff_rc,
            "ruff_log": ruff_log,
            "acceptance_violations": acceptance_violations,
        }

    # ---- helpers ----

    def _build_editor_input(self, subtask: dict, wt: Worktree, feedback: str | None) -> str:
        parts = [
            f"=== subtask_spec ===\n{self._render_spec(subtask)}",
            f"=== worktree path ===\n{wt.path}",
        ]
        if feedback:
            parts.append(f"=== feedback_from_previous_attempt ===\n{feedback}")
        return "\n\n".join(parts)

    def _render_spec(self, subtask: dict) -> str:
        return (
            f"# Subtask: {subtask['id']}\n\n"
            f"**Title:** {subtask['title']}\n"
            f"**Mode:** {subtask.get('mode', 'tdd')}\n\n"
            f"## Files (your scope — sandbox enforces)\n"
            f"```yaml\n{yaml.safe_dump(subtask.get('files', {}), allow_unicode=True)}```\n\n"
            f"## Acceptance criteria (validator will check mechanically)\n"
            f"```yaml\n{yaml.safe_dump(subtask.get('acceptance', []), allow_unicode=True)}```\n\n"
            f"## Spec\n{subtask['spec_md']}\n"
        )

    def _build_feedback(self, verdict: dict) -> str:
        parts = []
        if verdict["pytest_rc"] != 0 and verdict["mode"] != "baseline":
            tail = "\n".join(verdict["pytest_log"].splitlines()[-20:])
            parts.append(f"PYTEST FAILURE (rc={verdict['pytest_rc']}):\n{tail}")
        if verdict["ruff_rc"] != 0:
            tail = "\n".join(verdict["ruff_log"].splitlines()[-10:])
            parts.append(f"RUFF FAILURE (rc={verdict['ruff_rc']}):\n{tail}")
        if verdict["acceptance_violations"]:
            parts.append(format_violations(verdict["acceptance_violations"]))
        return "\n\n".join(parts)
