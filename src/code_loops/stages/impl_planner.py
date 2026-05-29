"""ImplPlannerStage — produces narrative plan.md + structured subtasks.yaml.

The LLM emits a single markdown document with an embedded YAML code block
holding the subtasks list. We split the two and validate the YAML against a
strict schema before persisting. On validation failure we retry once with
the parser/schema error as explicit feedback.
"""

from __future__ import annotations

import re
import time

import yaml
from rich.console import Console

from ..runner import RunnerFactory
from .prompt import StageContext, load_agent_prompt

console = Console()

MAX_VALIDATION_RETRIES = 1


class ImplPlannerError(RuntimeError):
    pass


class ImplPlannerStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        prompt_path = ctx.repo_root / stage_def["prompt"]
        sys_prompt = load_agent_prompt(prompt_path, ctx)
        rfc = (ctx.task_dir / "design" / "final.md").read_text()
        # Read followups.md if design_review shipped approved_with_followups
        # — tech-lead must surface these in the subtask plan as tracked work
        # so non-blocking concerns don't get lost on the floor.
        followups_path = ctx.task_dir / "design_review" / "followups.md"
        followups_block = ""
        if followups_path.exists():
            followups_block = (
                f"\n=== design_review/followups.md ===\n{followups_path.read_text()}\n"
                "The RFC above shipped with `approved_with_followups` verdict. The "
                "followups above are real concerns flagged by the review board but "
                "deemed ship-safe (FOLLOWUP severity — bounded blast radius). You "
                "MUST surface each followup in your implementation plan: add one "
                "tracked subtask per followup category (id prefix `followup_`) "
                "scoped to capture the concern as either (a) a deferred work item "
                "with explicit ticket-style description, or (b) a thin "
                "implementation extension if the followup is small enough to handle "
                "in this RFC's scope. Do NOT silently drop them.\n"
            )
        user_msg = (
            f"=== design/final.md ===\n{rfc}\n{followups_block}\n"
            "Produce the Implementation Plan as specified by your system prompt: "
            "narrative sections plus a strictly-schema'd `subtasks` YAML block."
        )
        runner = self.factory.make(stage_def)

        wall_start = time.monotonic()
        cost_total = 0.0
        attempt = 0
        last_error: str | None = None
        plan_md: str | None = None
        subtasks_data: dict | None = None
        previous_text: str | None = None

        while attempt <= MAX_VALIDATION_RETRIES:
            attempt += 1
            if last_error is None:
                msg = user_msg
            else:
                msg = (
                    f"=== design/final.md ===\n{rfc}\n\n"
                    f"=== previous_attempt.md ===\n{previous_text}\n\n"
                    f"=== schema_error.md ===\n"
                    f"Your previous output failed strict schema validation:\n\n"
                    f"{last_error}\n\n"
                    f"Re-emit the FULL plan (narrative + yaml block) with the schema "
                    f"error fixed. Do not output a diff."
                )
            console.print(
                f"  [dim]impl_plan:[/dim] attempt {attempt}"
                + (f" (retrying after: {last_error[:80]}...)" if last_error else "")
            )
            result = runner.run(sys_prompt, msg)
            cost_total += result.cost_usd or 0
            previous_text = result.text

            try:
                plan_md, subtasks_data = _split_plan_and_subtasks(result.text)
                _validate_subtasks(subtasks_data)
                break  # success
            except ImplPlannerError as e:
                last_error = str(e)
                # Persist failed attempt for forensics — both the raw response
                # AND the validation error that rejected it.
                if ctx.artifact_writer is not None:
                    ctx.artifact_writer.write_attempt(
                        "impl_plan", attempt, "raw_response.md", result.text
                    )
                    ctx.artifact_writer.write_attempt(
                        "impl_plan", attempt, "validation_error.txt", last_error
                    )
                    ctx.artifact_writer.manifest.record_attempt(
                        "impl_plan",
                        attempt,
                        outcome="schema_failed",
                        cost_usd=result.cost_usd or 0,
                        duration_s=result.duration_s or 0,
                        reason=last_error,
                    )
                if attempt > MAX_VALIDATION_RETRIES:
                    raise ImplPlannerError(
                        f"impl_plan failed validation after {attempt} attempts: {e}"
                    ) from e

        assert plan_md is not None and subtasks_data is not None
        subtasks_yaml = yaml.safe_dump(subtasks_data, sort_keys=False, allow_unicode=True)

        # Successful attempt: write to attempts/attempt_<N>/ AND copy to flat
        # impl_plan/{plan.md, subtasks.yaml} for downstream stages.
        if ctx.artifact_writer is not None:
            ctx.artifact_writer.write_attempt("impl_plan", attempt, "plan.md", plan_md)
            ctx.artifact_writer.write_attempt("impl_plan", attempt, "subtasks.yaml", subtasks_yaml)
            ctx.artifact_writer.manifest.record_attempt(
                "impl_plan",
                attempt,
                outcome="ok",
                cost_usd=result.cost_usd or 0,
                duration_s=result.duration_s or 0,
            )
        else:
            out_dir = ctx.task_dir / "impl_plan"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "plan.md").write_text(plan_md)
            (out_dir / "subtasks.yaml").write_text(subtasks_yaml)

        wall_duration = time.monotonic() - wall_start
        subtasks_count = len(subtasks_data["subtasks"])
        console.print(
            f"  [dim]impl_plan:[/dim] ✓ {subtasks_count} subtasks "
            f"({wall_duration:.0f}s, ${cost_total:.2f})"
        )

        return {
            "outputs": {
                "impl_plan/plan.md": plan_md,
                "impl_plan/subtasks.yaml": subtasks_yaml,
            },
            "cost_usd": cost_total,
            "duration_s": wall_duration,
            "subtask_count": subtasks_count,
            "summary": _build_summary(subtasks_data),
        }


# ---- parsing & validation ----

YAML_BLOCK_RE = re.compile(
    r"```(?:yaml|yml)?\s*\n(subtasks:\s*\n.*?)\n\s*```",
    re.DOTALL,
)


def _split_plan_and_subtasks(text: str) -> tuple[str, dict]:
    """Extract the yaml block; everything else (with the block stripped) is the plan."""
    m = YAML_BLOCK_RE.search(text)
    if not m:
        raise ImplPlannerError(
            "No `subtasks:` YAML code block found. Wrap the subtasks in a "
            "```yaml ... ``` fence with `subtasks:` as the top-level key."
        )
    raw_yaml = m.group(1)
    try:
        data = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as e:
        raise ImplPlannerError(f"YAML parse error in subtasks block: {e}") from e
    if not isinstance(data, dict):
        raise ImplPlannerError("subtasks block must parse to a YAML mapping at top level")
    # plan.md is everything except the yaml block
    plan_md = (text[: m.start()] + text[m.end() :]).rstrip() + "\n"
    return plan_md, data


SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Subtask modes — drive validator semantics (см. subtask_iterator._run_validator
# или subtask_executor mode dispatch). Default = "tdd" preserves prior behavior.
VALID_SUBTASK_MODES: frozenset[str] = frozenset({"tdd", "baseline", "refactor", "hotfix"})

# Acceptance criterion types — machine-verifiable post-conditions tech-lead
# pre-declares per subtask. Validator проверяет каждый детерминированно;
# violations = subtask not shipped.
VALID_ACCEPTANCE_TYPES: frozenset[str] = frozenset({
    "pytest_collected_count",     # pytest --collect-only count == target
    "pytest_outcome_count",       # pytest counts: {passed, failed, xfailed, ...}
    "file_contains_pattern",      # regex match required
    "file_not_contains_pattern",  # regex must NOT match
    "file_size_min",              # min bytes
    "ruff_clean",                 # ruff exits 0 for given file/dir
    "json_path_exists",           # json file has structural path
})


def _validate_acceptance(prefix: str, acceptance) -> None:
    if not isinstance(acceptance, list):
        raise ImplPlannerError(f"{prefix}.acceptance must be a list of check objects.")
    for j, check in enumerate(acceptance):
        cp = f"{prefix}.acceptance[{j}]"
        if not isinstance(check, dict):
            raise ImplPlannerError(f"{cp} must be a mapping.")
        ctype = check.get("type")
        if ctype not in VALID_ACCEPTANCE_TYPES:
            raise ImplPlannerError(
                f"{cp}.type must be one of {sorted(VALID_ACCEPTANCE_TYPES)}; got {ctype!r}"
            )
        # Per-type required fields
        if ctype in {"file_contains_pattern", "file_not_contains_pattern"}:
            if not check.get("file") or not check.get("pattern"):
                raise ImplPlannerError(f"{cp} ({ctype}) requires `file` + `pattern`.")
        elif ctype == "pytest_collected_count":
            if "target" not in check or not isinstance(check["target"], int):
                raise ImplPlannerError(f"{cp} ({ctype}) requires integer `target`.")
        elif ctype == "pytest_outcome_count":
            if not isinstance(check.get("outcomes"), dict):
                raise ImplPlannerError(
                    f"{cp} ({ctype}) requires `outcomes` dict (e.g. {{passed: 2, xfailed: 16}})."
                )
        elif ctype == "file_size_min":
            if not check.get("file") or not isinstance(check.get("bytes"), int):
                raise ImplPlannerError(f"{cp} ({ctype}) requires `file` + integer `bytes`.")
        elif ctype == "ruff_clean" and not check.get("file"):
            raise ImplPlannerError(f"{cp} ({ctype}) requires `file` (path or dir).")
        elif ctype == "json_path_exists" and (
            not check.get("file") or not isinstance(check.get("path"), list)
        ):
            raise ImplPlannerError(
                f"{cp} ({ctype}) requires `file` + `path` (list of str/int keys)."
            )


def _validate_subtasks(data: dict) -> None:
    if "subtasks" not in data:
        raise ImplPlannerError("Top-level key `subtasks` is required.")
    subtasks = data["subtasks"]
    if not isinstance(subtasks, list) or not subtasks:
        raise ImplPlannerError("`subtasks` must be a non-empty list.")

    seen_ids: set[str] = set()
    for i, st in enumerate(subtasks):
        prefix = f"subtasks[{i}]"
        if not isinstance(st, dict):
            raise ImplPlannerError(f"{prefix} must be a mapping, got {type(st).__name__}.")

        for key in ("id", "title", "files", "spec_md"):
            if key not in st:
                raise ImplPlannerError(f"{prefix} missing required key `{key}`.")

        sid = st["id"]
        if not isinstance(sid, str) or not SNAKE_CASE_RE.match(sid):
            raise ImplPlannerError(
                f"{prefix}.id must be snake_case (lowercase letters, digits, underscores; "
                f"start with letter). Got: {sid!r}"
            )
        if sid in seen_ids:
            raise ImplPlannerError(f"{prefix}.id `{sid}` is duplicated.")
        seen_ids.add(sid)

        if not isinstance(st["title"], str) or not st["title"].strip():
            raise ImplPlannerError(f"{prefix}.title must be a non-empty string.")
        if not isinstance(st["spec_md"], str) or not st["spec_md"].strip():
            raise ImplPlannerError(f"{prefix}.spec_md must be a non-empty string.")

        files = st["files"]
        if not isinstance(files, dict) or not files:
            raise ImplPlannerError(
                f"{prefix}.files must be a non-empty dict with at least one of "
                "`create | modify | delete` keys."
            )
        valid_keys = {"create", "modify", "delete"}
        bad_keys = set(files.keys()) - valid_keys
        if bad_keys:
            raise ImplPlannerError(
                f"{prefix}.files has unknown keys: {sorted(bad_keys)}. "
                f"Allowed: {sorted(valid_keys)}."
            )
        for fk, paths in files.items():
            if not isinstance(paths, list) or not all(
                isinstance(p, str) and p.strip() for p in paths
            ):
                raise ImplPlannerError(
                    f"{prefix}.files.{fk} must be a list of non-empty path strings."
                )
            if not paths:
                raise ImplPlannerError(
                    f"{prefix}.files.{fk} is empty — omit the key entirely instead."
                )

        if "depends_on" in st:
            deps = st["depends_on"]
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                raise ImplPlannerError(f"{prefix}.depends_on must be a list of id strings.")
            for d in deps:
                if d not in seen_ids:
                    raise ImplPlannerError(
                        f"{prefix}.depends_on references `{d}` which is not a prior subtask id "
                        "(forward references not allowed; subtask order is execution order)."
                    )

        if "wave" in st:
            wave = st["wave"]
            if not isinstance(wave, int) or isinstance(wave, bool) or wave < 0:
                raise ImplPlannerError(
                    f"{prefix}.wave must be a non-negative integer. Got: {wave!r}"
                )

        if "needs" in st:
            needs = st["needs"]
            if not isinstance(needs, list) or not all(
                isinstance(n, str) and n.strip() for n in needs
            ):
                raise ImplPlannerError(
                    f"{prefix}.needs must be a list of non-empty role-name strings "
                    "(e.g. [prompt_engineer, eval_engineer])."
                )

        # mode + acceptance — optional, additive (Phase 1 pivot к editor model).
        # Default mode = tdd preserves prior behavior for legacy subtasks.yaml.
        if "mode" in st:
            mode = st["mode"]
            if mode not in VALID_SUBTASK_MODES:
                raise ImplPlannerError(
                    f"{prefix}.mode must be one of {sorted(VALID_SUBTASK_MODES)}; got {mode!r}"
                )
        if "acceptance" in st:
            _validate_acceptance(prefix, st["acceptance"])

    # Wave-level invariants: within a wave, no shared files and no inter-deps.
    _validate_waves(subtasks)


def _validate_waves(subtasks: list[dict]) -> None:
    """Enforce per-wave invariants: file-disjoint + no intra-wave dependencies.

    Wave grouping is OPT-IN — a subtask without an explicit `wave` field is
    treated as its own implicit wave (sequential, default behavior). Only
    subtasks that EXPLICITLY share a `wave` value go through the disjoint
    invariant. This way the wave field is purely additive — existing plans
    without it keep working as before.
    """
    by_wave: dict[int, list[dict]] = {}
    for st in subtasks:
        if "wave" not in st:
            continue
        by_wave.setdefault(st["wave"], []).append(st)

    for wave_n, members in by_wave.items():
        if len(members) < 2:
            continue
        # File overlap check
        all_files: dict[str, str] = {}  # path -> first subtask id that claims it
        for st in members:
            for fk in ("create", "modify", "delete"):
                for p in st.get("files", {}).get(fk, []):
                    if p in all_files and all_files[p] != st["id"]:
                        raise ImplPlannerError(
                            f"wave {wave_n}: subtasks `{all_files[p]}` and `{st['id']}` "
                            f"both touch `{p}` — wave-mates MUST share no files. "
                            "Move one to a later wave or merge them into one subtask."
                        )
                    all_files[p] = st["id"]
        # Intra-wave dependency check
        member_ids = {st["id"] for st in members}
        for st in members:
            for d in st.get("depends_on", []) or []:
                if d in member_ids:
                    raise ImplPlannerError(
                        f"wave {wave_n}: subtask `{st['id']}` depends_on `{d}` which is in "
                        "the same wave. Wave-mates MUST be independent — move one to an "
                        "earlier wave."
                    )


def _build_summary(data: dict) -> str:
    """Compact human-readable summary for human_review checkpoint."""
    lines = [f"Subtasks ({len(data['subtasks'])}):"]
    for st in data["subtasks"]:
        deps = f"  ← {', '.join(st['depends_on'])}" if st.get("depends_on") else ""
        files = st["files"]
        n_create = len(files.get("create", []))
        n_modify = len(files.get("modify", []))
        n_delete = len(files.get("delete", []))
        file_summary_parts = []
        if n_create:
            file_summary_parts.append(f"+{n_create}")
        if n_modify:
            file_summary_parts.append(f"~{n_modify}")
        if n_delete:
            file_summary_parts.append(f"-{n_delete}")
        file_summary = " ".join(file_summary_parts) or "(no files?)"
        lines.append(f"  • {st['id']}: {st['title']}  [{file_summary}]{deps}")
    return "\n".join(lines)
