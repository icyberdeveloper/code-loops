"""SubtaskIteratorStage — TDD per-subtask loop with worktree + tests RO + fix router.

For each subtask in impl_plan/subtasks.yaml:
  1. test_writer writes tests under tests/ in the worktree.
  2. Engine commits + locks tests/ chmod 444.
  3. coder implements production code (NEVER touches tests/, OS-level
     locked + post-hoc git-diff guard).
  4. reviewer reviews tests + code.
  5. validator runs pytest + ruff in the worktree.
  6. On any failure: fix_router decides {coder | test_writer | escalate_design}
     with up to MAX_ATTEMPTS_PER_TARGET retries per target per subtask.

Coder runs in subprocess with cwd=worktree, so its Edit/Write/Bash tools
operate on the worktree by default. Each subtask = fresh subprocess =
inherent context reset between subtasks.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import yaml
from rich.console import Console

from ..project_loader import get_test_infrastructure
from ..runner import RunnerFactory
from ..worktree import TestProtectionViolation, Worktree
from .prompt import StageContext, load_agent_prompt
from .role_normalizer import normalize_roles

console = Console()

MAX_ATTEMPTS_PER_TARGET = 3
VALID_FIX_TARGETS = {"coder", "test_writer", "escalate_design"}


class SubtaskIteratorError(RuntimeError):
    pass


class DesignEscalation(RuntimeError):
    """fix_router decided the issue requires impl_plan / rfc-level rethink."""

    def __init__(self, subtask_id: str, reason: str, feedback: str):
        self.subtask_id = subtask_id
        self.reason = reason
        self.feedback = feedback
        super().__init__(f"design escalation on {subtask_id}: {reason}")


class SubtaskIteratorStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    @staticmethod
    def _resolve_base_repo(stage_def: dict, ctx: StageContext) -> Path:
        """Get base_repo from project_config or fall back to stage_def. Raise if neither."""
        if ctx.project_config:
            project = ctx.project_config.get("project") or {}
            base = project.get("base_repo")
            if base:
                return Path(base)
        legacy = stage_def.get("base_repo")
        if legacy:
            return Path(legacy)
        raise SubtaskIteratorError(
            "base_repo is not configured. Set it in project.yaml under "
            "`project.base_repo` and pass --project-config / set "
            "CODE_LOOPS_PROJECT, or set `base_repo` on the implementation "
            "stage in pipeline.yaml (legacy)."
        )

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        wall_start = time.monotonic()
        cost_total = 0.0

        # Parse subtasks.yaml
        subtasks_yaml = ctx.task_dir / "impl_plan" / "subtasks.yaml"
        subtasks = yaml.safe_load(subtasks_yaml.read_text())["subtasks"]

        # Setup worktree — base_repo comes from project.yaml (preferred) or
        # legacy stage_def fallback (deprecated; will be removed once
        # decoupling is complete).
        base_repo = self._resolve_base_repo(stage_def, ctx)
        worktree_root = ctx.task_dir / "worktree"
        worktree_root.mkdir(parents=True, exist_ok=True)
        branch = f"code-loops/{ctx.task_dir.name}"
        wt_path = worktree_root / "wt"

        console.print(f"  [dim]implementation:[/dim] creating worktree at {wt_path}")
        wt = Worktree.create(base_repo, branch, wt_path, force=True)
        wt.tag_base()  # mark fork point for diff_vs_base

        prior_files: list[tuple[str, str]] = []  # (subtask_id, path) for context

        # Wave-aware execution: subtasks with explicit `wave: N` are flagged
        # parallel-eligible. v0.1 still runs everything sequentially (true
        # parallel execution is deferred — requires multi-worktree merge logic
        # and validator serialization that's only justified after Step 10
        # measures actual wall-clock pain). For now we just print a wave
        # advisory so the operator sees the parallelization opportunity.
        explicit_waves: dict[int, list[str]] = {}
        for st in subtasks:
            if "wave" in st:
                explicit_waves.setdefault(st["wave"], []).append(st["id"])
        for wave_n, ids in sorted(explicit_waves.items()):
            if len(ids) > 1:
                console.print(
                    f"  [yellow]wave {wave_n}:[/yellow] {len(ids)} subtasks flagged "
                    f"parallel-eligible — running SEQUENTIALLY in v0.1 "
                    f"(parallel execution deferred). Members: {', '.join(ids)}"
                )

        # Normalize roles once so list-of-dicts pipeline.yaml works
        stage_def = {**stage_def, "roles": normalize_roles(stage_def["roles"])}

        try:
            for subtask in subtasks:
                stcost, escalated = self._run_subtask(subtask, ctx, wt, prior_files, stage_def)
                cost_total += stcost
                if escalated:
                    # DesignEscalation will have been raised; control won't reach here.
                    break
                # Track files this subtask touched for downstream context
                files = subtask.get("files", {})
                for action in ("create", "modify"):
                    for f in files.get(action, []):
                        prior_files.append((subtask["id"], f))

            # Save full diff as the consolidated artifact
            full_diff = wt.diff_vs_base()
            full_diff_path = ctx.task_dir / "implementation" / "_full_diff.patch"
            full_diff_path.parent.mkdir(parents=True, exist_ok=True)
            full_diff_path.write_text(full_diff)

            # Save list of files in full diff (used by validation stage)
            files_list = sorted(wt.files_vs_base())
            (ctx.task_dir / "implementation" / "_files_changed.txt").write_text(
                "\n".join(files_list) + "\n" if files_list else ""
            )
        except DesignEscalation:
            # Don't cleanup — leave worktree for inspection
            raise

        wall_duration = time.monotonic() - wall_start
        return {
            "outputs": {
                "implementation/_full_diff.patch": (
                    full_diff_path.read_text() if full_diff_path.exists() else ""
                ),
                "implementation/_files_changed.txt": (
                    (ctx.task_dir / "implementation" / "_files_changed.txt").read_text()
                    if (ctx.task_dir / "implementation" / "_files_changed.txt").exists()
                    else ""
                ),
            },
            "cost_usd": cost_total,
            "duration_s": wall_duration,
            "subtasks_completed": len(subtasks),
            "worktree_path": str(wt_path),
        }

    # ---- per-subtask loop ----

    def _run_subtask(
        self,
        subtask: dict,
        ctx: StageContext,
        wt: Worktree,
        prior_files: list[tuple[str, str]],
        stage_def: dict,
    ) -> tuple[float, bool]:
        sid = subtask["id"]
        sub_dir = ctx.task_dir / "implementation" / sid
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / "spec.md").write_text(self._render_spec(subtask))

        roles = stage_def["roles"]
        # Read test_infrastructure config — drives whether test_writer runs +
        # how worktree locking behaves. Defaults preserve prior behavior
        # (Python `tests/` dir + chmod 444).
        test_infra = get_test_infrastructure(ctx.project_config)
        test_enabled = test_infra["enabled"]
        test_paths = test_infra["test_paths"]
        lock_strategy = test_infra["lock_strategy"]

        attempts: dict[str, int] = {"test_writer": 0, "coder": 0, "reviewer": 0}
        cost_total = 0.0
        feedback: dict[str, str] = {}  # per-target feedback for next call

        console.print(f"  [bold cyan]▶ subtask {sid}[/bold cyan]: {subtask['title']}")
        if not test_enabled:
            console.print(
                f"  [dim]subtask {sid}[/dim]: test_infrastructure.enabled=false → "
                "skipping test_writer; coder runs directly"
            )

        # 0. Extra pre-roles declared by the planner via subtask.needs
        # (e.g. needs: [prompt_engineer, eval_engineer]).
        # Generic dispatch — engine doesn't know what each role does, just
        # invokes the agent whose name matches and commits worktree changes.
        # Project-agnostic: works for any role declared in pipeline.yaml's
        # implementation stage.
        for role_name in subtask.get("needs") or []:
            if role_name in {"test_writer", "coder", "reviewer", "fix_router"}:
                # Standard TDD-loop roles — always run via the dedicated path
                # below, ignore if listed in `needs` (defensive).
                continue
            if role_name not in roles:
                console.print(
                    f"  [yellow]subtask {sid}[/yellow]: needs `{role_name}` "
                    "but role is not declared in pipeline.yaml — skipping"
                )
                continue
            console.print(
                f"  [dim]subtask {sid}[/dim]: invoking pre-role `{role_name}` "
                f"(declared in subtask.needs)"
            )
            cost_extra = self._run_extra_role(
                role_name, roles, subtask, prior_files, wt, sub_dir, ctx
            )
            cost_total += cost_extra

        # 1. Test Writer (initial) — skipped if test_infrastructure disabled
        if test_enabled:
            cost_tw, _ = self._run_test_writer(
                roles, subtask, prior_files, wt, sub_dir, ctx, feedback.pop("test_writer", None)
            )
            cost_total += cost_tw
            attempts["test_writer"] += 1
            tests_commit_sha = wt.head_sha()  # for diff guard
            wt.lock_tests_readonly(test_paths=test_paths, strategy=lock_strategy)
        else:
            tests_commit_sha = wt.head_sha()  # baseline for any later diff_since calls

        # 2. Coder (initial)
        try:
            cost_c, code_disagreement = self._run_coder(
                roles,
                subtask,
                prior_files,
                wt,
                sub_dir,
                ctx,
                feedback.pop("coder", None),
                test_paths,
            )
            cost_total += cost_c
            attempts["coder"] += 1

            if code_disagreement and test_enabled:
                # Coder flagged TEST_DISAGREEMENT — route to test_writer
                feedback["test_writer"] = (
                    f"Coder flagged a test issue: {code_disagreement}\nReconsider the test."
                )
                # Fall through to fix loop
        except TestProtectionViolation as exc:
            wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
            raise SubtaskIteratorError(
                f"Coder violated test protection in subtask {sid}: {exc.files}"
            ) from exc

        # Fix loop
        while True:
            # Run reviewer + validator
            wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
            review_cost, review_verdict = self._run_reviewer(
                roles, subtask, wt, sub_dir, tests_commit_sha, ctx
            )
            cost_total += review_cost
            attempts["reviewer"] += 1

            validator_result = self._run_validator(wt, sub_dir)
            wt.lock_tests_readonly(test_paths=test_paths, strategy=lock_strategy)

            if validator_result["passed"] and review_verdict["verdict"] == "approved":
                # Subtask done — commit code (unlock for git mv etc.)
                wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
                console.print(f"  [green]✓ subtask {sid}[/green] passed all checks")
                if ctx.artifact_writer is not None:
                    ctx.artifact_writer.manifest.set_subtask_final(sid, outcome="shipped")
                return cost_total, False

            # Failure — assemble failure_input + ask fix_router
            failure_input = self._build_failure_input(
                validator_result,
                review_verdict,
                code_disagreement if "code_disagreement" in dir() else None,
            )

            wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
            route_cost, route = self._run_fix_router(roles, subtask, failure_input, attempts, ctx)
            cost_total += route_cost

            target = route["target"]
            # When test_infrastructure disabled, test_writer is not a valid
            # routing target (no role to invoke). Coerce to coder.
            if not test_enabled and target == "test_writer":
                console.print(
                    f"  [yellow]subtask {sid}[/yellow]: fix_router picked test_writer "
                    "but test_infrastructure disabled — coercing to coder"
                )
                target = "coder"
            target_attempts_used = attempts.get(target, 0)
            console.print(
                f"  [yellow]↻ subtask {sid}[/yellow]: fix_router → {target} "
                f"(attempts used: {target_attempts_used}/{MAX_ATTEMPTS_PER_TARGET})"
            )

            if target == "escalate_design" or target_attempts_used >= MAX_ATTEMPTS_PER_TARGET:
                wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
                if ctx.artifact_writer is not None:
                    ctx.artifact_writer.manifest.set_subtask_final(sid, outcome="escalated")
                raise DesignEscalation(
                    subtask_id=sid,
                    reason=route["reason"],
                    feedback=route.get("feedback_for_target", ""),
                )

            # Re-run target with feedback
            feedback[target] = route.get("feedback_for_target", "")
            if target == "test_writer":
                wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
                cost_tw, _ = self._run_test_writer(
                    roles, subtask, prior_files, wt, sub_dir, ctx, feedback.pop("test_writer", None)
                )
                cost_total += cost_tw
                attempts["test_writer"] += 1
                # Re-commit tests, refresh tests_commit_sha
                tests_commit_sha = wt.head_sha()
                wt.lock_tests_readonly(test_paths=test_paths, strategy=lock_strategy)
            elif target == "coder":
                wt.lock_tests_readonly(test_paths=test_paths, strategy=lock_strategy)
                try:
                    cost_c, code_disagreement = self._run_coder(
                        roles,
                        subtask,
                        prior_files,
                        wt,
                        sub_dir,
                        ctx,
                        feedback.pop("coder", None),
                        test_paths,
                    )
                    cost_total += cost_c
                    attempts["coder"] += 1
                    if code_disagreement and test_enabled:
                        feedback["test_writer"] = f"Coder flagged a test issue: {code_disagreement}"
                except TestProtectionViolation as exc:
                    wt.unlock_tests(test_paths=test_paths, strategy=lock_strategy)
                    raise SubtaskIteratorError(
                        f"Coder violated test protection in subtask {sid}: {exc.files}"
                    ) from exc
            # Loop back — reviewer + validator again

    # ---- per-stage runners ----

    def _run_extra_role(self, role_name, roles, subtask, prior_files, wt, sub_dir, ctx):
        """Generic dispatcher for planner-declared pre-roles (subtask.needs).

        Engine doesn't know what the role does semantically — it just runs
        the agent's prompt with standard subtask context, saves the response
        for forensics, and commits any worktree changes the role made.
        """
        runner = self.factory.make(roles[role_name])
        sys_prompt = load_agent_prompt(ctx.repo_root / roles[role_name]["prompt"], ctx)
        user_msg = self._build_user_msg(role_name, subtask, prior_files, wt, None)
        result = runner.run(sys_prompt, user_msg, cwd=wt.path)
        (sub_dir / f"{role_name}_response.md").write_text(result.text)
        try:
            wt.commit_all(f"{role_name}: {subtask['id']}")
        except Exception as e:
            console.print(
                f"  [yellow]subtask {subtask['id']}[/yellow]: {role_name} made no commit ({e})"
            )
        return result.cost_usd or 0

    def _run_test_writer(self, roles, subtask, prior_files, wt, sub_dir, ctx, feedback):
        runner = self.factory.make(roles["test_writer"])
        sys_prompt = load_agent_prompt(ctx.repo_root / roles["test_writer"]["prompt"], ctx)
        user_msg = self._build_user_msg("test_writer", subtask, prior_files, wt, feedback)
        result = runner.run(sys_prompt, user_msg, cwd=wt.path)
        # Save the LLM response for forensics
        (sub_dir / "test_writer_response.md").write_text(result.text)
        # Commit any worktree changes the test_writer made
        try:
            wt.commit_all(f"tests: {subtask['id']}")
            tests_diff = wt.diff_since(wt.head_sha() + "~1")
            (sub_dir / "tests.diff").write_text(tests_diff)
        except Exception as e:
            console.print(
                f"  [yellow]subtask {subtask['id']}[/yellow]: test_writer made no commit ({e})"
            )
        return result.cost_usd or 0, result.text

    def _run_coder(
        self,
        roles,
        subtask,
        prior_files,
        wt,
        sub_dir,
        ctx,
        feedback,
        test_paths=None,
    ):
        runner = self.factory.make(roles["coder"])
        sys_prompt = load_agent_prompt(ctx.repo_root / roles["coder"]["prompt"], ctx)
        user_msg = self._build_user_msg("coder", subtask, prior_files, wt, feedback)
        before_sha = wt.head_sha()
        result = runner.run(sys_prompt, user_msg, cwd=wt.path)
        (sub_dir / "coder_response.md").write_text(result.text)

        # Detect TEST_DISAGREEMENT signal
        disagreement = None
        if "TEST_DISAGREEMENT" in result.text.split("\n")[0:5][0:1] or re.search(
            r"^\s*TEST_DISAGREEMENT\s*$", result.text, re.MULTILINE
        ):
            disagreement = result.text  # full body as feedback

        # Hard test-protection guard BEFORE we commit. Project's test_paths
        # come from project.yaml (default: ["tests"]). Empty list = no guard.
        wt.assert_no_test_changes(before_sha, test_paths=test_paths)

        # Commit code changes
        try:
            wt.commit_all(f"code: {subtask['id']}")
            code_diff = wt.diff_since(before_sha)
            (sub_dir / "code.diff").write_text(code_diff)
        except Exception as e:
            console.print(f"  [yellow]subtask {subtask['id']}[/yellow]: coder made no commit ({e})")

        return result.cost_usd or 0, disagreement

    def _run_reviewer(self, roles, subtask, wt, sub_dir, tests_sha, ctx):
        runner = self.factory.make(roles["reviewer"])
        sys_prompt = load_agent_prompt(ctx.repo_root / roles["reviewer"]["prompt"], ctx)
        # tests_diff is what we committed at tests_sha; code_diff is everything since
        tests_diff = (
            (sub_dir / "tests.diff").read_text() if (sub_dir / "tests.diff").exists() else ""
        )
        code_diff = (sub_dir / "code.diff").read_text() if (sub_dir / "code.diff").exists() else ""
        user_msg = (
            f"=== subtask_spec ===\n{self._render_spec(subtask)}\n\n"
            f"=== tests_diff ===\n{tests_diff}\n\n"
            f"=== code_diff ===\n{code_diff}\n"
        )
        result = runner.run(sys_prompt, user_msg)
        (sub_dir / "review.md").write_text(result.text)
        verdict = self._parse_reviewer_verdict(result.text)
        return result.cost_usd or 0, verdict

    def _run_validator(self, wt, sub_dir):
        """Run pytest + ruff in the worktree. Returns dict with logs and pass status."""
        val_dir = sub_dir / "validation"
        val_dir.mkdir(exist_ok=True)
        pytest_log = subprocess.run(
            ["uv", "run", "pytest", "-x", "--tb=short", "-q"],
            cwd=str(wt.path),
            capture_output=True,
            text=True,
            timeout=600,
        )
        (val_dir / "pytest.log").write_text(
            pytest_log.stdout + "\n--STDERR--\n" + pytest_log.stderr
        )
        ruff_log = subprocess.run(
            ["uv", "run", "ruff", "check", "."],
            cwd=str(wt.path),
            capture_output=True,
            text=True,
            timeout=120,
        )
        (val_dir / "ruff.log").write_text(ruff_log.stdout + "\n--STDERR--\n" + ruff_log.stderr)
        passed = pytest_log.returncode == 0 and ruff_log.returncode == 0
        result = {
            "passed": passed,
            "pytest_rc": pytest_log.returncode,
            "ruff_rc": ruff_log.returncode,
            "pytest_log": pytest_log.stdout[-3000:],  # tail
            "ruff_log": ruff_log.stdout[-1500:],
        }
        (val_dir / "result.yaml").write_text(
            yaml.safe_dump(
                {
                    "passed": passed,
                    "pytest_rc": pytest_log.returncode,
                    "ruff_rc": ruff_log.returncode,
                }
            )
        )
        return result

    def _run_fix_router(self, roles, subtask, failure_input, attempts, ctx):
        runner = self.factory.make(roles["fix_router"])
        sys_prompt = load_agent_prompt(ctx.repo_root / roles["fix_router"]["prompt"], ctx)
        user_msg = (
            f"=== subtask_spec ===\n{self._render_spec(subtask)}\n\n"
            f"=== failure_input ===\n{failure_input}\n\n"
            f"=== attempts_so_far ===\n{json.dumps(attempts)}\n"
        )
        result = runner.run(sys_prompt, user_msg)
        route = self._parse_router_verdict(result.text)
        return result.cost_usd or 0, route

    # ---- helpers ----

    def _build_user_msg(self, role: str, subtask, prior_files, wt, feedback):
        files_block = "\n".join(
            f"{action}: {p}" for action, paths in subtask.get("files", {}).items() for p in paths
        )
        prior_block = (
            "\n".join(f"- {sid}: {p}" for sid, p in prior_files)
            if prior_files
            else "(none — this is the first subtask)"
        )
        parts = [
            f"=== subtask_spec ===\n{self._render_spec(subtask)}",
            f"=== subtask_files ===\n{files_block}",
            f"=== prior_files ===\n{prior_block}",
            f"=== worktree ===\nYour CWD is the worktree root: {wt.path}",
        ]
        if feedback:
            parts.append(f"=== feedback_from_previous_attempt ===\n{feedback}")
        return "\n\n".join(parts)

    def _render_spec(self, subtask) -> str:
        return (
            f"# Subtask: {subtask['id']}\n\n"
            f"**Title:** {subtask['title']}\n\n"
            f"## Files\n```yaml\n{yaml.safe_dump(subtask.get('files', {}), allow_unicode=True)}```\n\n"
            f"## Spec\n{subtask['spec_md']}\n"
        )

    def _build_failure_input(self, validator_result, review_verdict, disagreement):
        parts = []
        if not validator_result["passed"]:
            parts.append(
                f"PYTEST (rc={validator_result['pytest_rc']}):\n{validator_result['pytest_log']}\n"
            )
            parts.append(
                f"RUFF (rc={validator_result['ruff_rc']}):\n{validator_result['ruff_log']}\n"
            )
        if review_verdict["verdict"] != "approved":
            parts.append(
                f"REVIEWER concerns:\n{json.dumps(review_verdict.get('concerns', []), indent=2, ensure_ascii=False)}"
            )
        if disagreement:
            parts.append(f"CODER disagreement:\n{disagreement}")
        return "\n\n".join(parts)

    def _parse_reviewer_verdict(self, text: str) -> dict:
        m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
        if m:
            try:
                v = json.loads(m.group(1))
                if isinstance(v, dict) and "verdict" in v:
                    verdict = str(v["verdict"]).strip().lower()
                    if verdict not in {"approved", "needs_fix"}:
                        verdict = "needs_fix"
                    return {"verdict": verdict, "concerns": v.get("concerns", []) or []}
            except json.JSONDecodeError:
                pass
        return {
            "verdict": "needs_fix",
            "concerns": [{"severity": "blocker", "what": "could not parse reviewer verdict"}],
        }

    def _parse_router_verdict(self, text: str) -> dict:
        m = re.search(r"```(?:json)?\s*\n(\{.*?\})\s*\n```", text, re.DOTALL)
        if m:
            try:
                v = json.loads(m.group(1))
                if isinstance(v, dict) and "target" in v:
                    target = str(v["target"]).strip().lower()
                    if target not in VALID_FIX_TARGETS:
                        target = "coder"  # safest fallback
                    return {
                        "target": target,
                        "reason": str(v.get("reason", "")),
                        "feedback_for_target": str(v.get("feedback_for_target", "")),
                    }
            except json.JSONDecodeError:
                pass
        return {
            "target": "coder",
            "reason": "(could not parse router verdict — defaulting to coder)",
            "feedback_for_target": text[-2000:],
        }
