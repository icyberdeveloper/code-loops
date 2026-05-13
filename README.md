# code-loops

[![CI](https://github.com/icyberdeveloper/code-loops/actions/workflows/ci.yml/badge.svg)](https://github.com/icyberdeveloper/code-loops/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

> Multi-agent development pipeline orchestrator. Turns a one-line task
> description into shipped, reviewed, documented code via 26 specialized
> AI agents running through a deterministic Python pipeline.

**Status:** Pre-1.0. Battle-tested on a personal Python project
(Claude CLI integration). Schema designed to be project-agnostic —
should work on any git repo with appropriate config; cross-project
validation is in progress.

---

## What it does

You hand `code-loops` a task (free-text or markdown file). It runs that
task through a 12-stage pipeline of specialized agents:

```
PRD          → Business Analyst writes a structured product brief with NFR gate
Research     → 5 specialists scan codebase / prompts / incidents / data / AI surface in parallel
Design       → Software Architect drafts an RFC; perspective lenses critique;
               debate-arbiter judges convergence (theme-based, not bug-count)
Design Review→ Safety + Elegance + Hallucination + AI critics review;
               Architect responds; Review-Arbiter emits verdict
               (approved / needs_revision / redesign_needed)
Impl Plan    → Tech Lead decomposes design into atomic, file-disjoint subtasks
               (TDD-ordered, dependency-aware, optional wave grouping)
Implementation
             → For each subtask: optional Prompt Engineer / Eval Engineer →
               QA Engineer writes failing tests (locked chmod 444) →
               Software Engineer implements →
               Code Reviewer audits diff →
               Triage Engineer routes failures (max 3 attempts/target)
Validation   → Programmatic gate: pytest, ruff, file-coverage check (no LLM)
Regression   → Conditional eval-bench gate (no LLM). Off by default; opt-in
               via project.yaml. Runs the project's bench, compares each
               metric vs saved baseline, fails if any drops > threshold_pct.
               First run captures baseline.
Release Review → Release Manager gates semantic compliance vs PRD/RFC.
                 Can issue corrective_subtasks → engine re-enters Implementation
Release Docs → Tech Writer produces changelog + ADR + maintenance notes
               (flags brief.md staleness)
Auto Resurvey → If maintenance notes flagged staleness, project-surveyor
                regenerates projects/<name>/brief.md automatically (else skip)
```

At 4 stages you get a **human-review checkpoint** (approve / abort /
revise with comment). Auto-loops handle two failure modes: critique
detecting a *patching anti-pattern* bubbles back to design with a
redesign signal; release-review detecting *missing implementation*
appends corrective subtasks and re-enters Implementation.

> **⚠️  Python-only today.** The validation stage and several agent
> prompts hardcode `pytest` + `ruff`. The orchestrator itself, project
> config, worktree management, and TDD loop are language-agnostic in
> shape — but a NodeJS / Go / Rust user would hit failures in the
> validation gate. Multi-language support (configurable
> `test_command` / `lint_command` per project) is the top roadmap
> item; see [Roadmap](#roadmap).

---

## Install

### Prerequisites
- **Python ≥ 3.11**
- **[uv](https://docs.astral.sh/uv/)** — Python package manager
- **Anthropic [Claude CLI](https://docs.claude.com/en/docs/claude-code)**
  on your `$PATH`, authenticated. The orchestrator shells out to
  `claude --print` for every agent invocation.
- **git** (worktrees require ≥ 2.5)

### Option 1: install as a tool (recommended)
```bash
uv tool install git+https://github.com/icyberdeveloper/code-loops.git
code-loops --help
```
Now `code-loops` is on your `$PATH` everywhere. `pipeline.yaml` and the
26 agent prompts ship inside the wheel as package data.

### Option 2: clone for development
```bash
git clone https://github.com/icyberdeveloper/code-loops.git
cd code-loops
uv sync
uv run code-loops --help
```
Use this if you want to modify agent prompts or pipeline.yaml.

### Workspace
`code-loops` creates `tasks/` and `projects/` subdirectories in your
**current working directory**. Run it from a directory you have write
access to (e.g. `~/code-loops-workspace`). Override via
`$CODE_LOOPS_WORKSPACE` env var.

That's it for the orchestrator itself. Real cost lives in Anthropic API
usage during pipeline runs (see [Costs](#costs) below).

---

## Setup — bootstrap your project

`code-loops` operates on a **target project** — the codebase you want
the pipeline to evolve. You bootstrap it once:

```bash
uv run code-loops init /absolute/path/to/your/project
```

This:
1. Creates `projects/<name>/project.yaml` with `name` + `base_repo`.
2. Invokes the **`project-surveyor`** agent against your repo
   (~$0.30–3.00, 1–6 min — depends on project size). Surveyor scans
   README / CLAUDE.md / source tree and writes
   `projects/<name>/brief.md` documenting architecture, layout, key
   modules, storage layer, RAG/vector search (if any), conventions,
   domain glossary, external integrations, and rules every downstream
   agent should follow.

The brief is auto-loaded into every code-loops agent via the
`{PROJECT_BRIEF}` placeholder — agents work with full project context
without you having to feed it manually.

If you have multiple projects, pass `--project <name>` on subsequent
commands; with one project it's auto-selected.

To skip the surveyor LLM call (dev/cheap path):
```bash
uv run code-loops init /path/to/project --no-survey
```
Brief gets a placeholder — you can edit it manually or run
`uv run code-loops resurvey <name>` later.

---

## Usage

### Create a task
Pass either a free-text description or a path to a `.md` file. The CLI
auto-detects:
```bash
uv run code-loops new "Add /export-data command for weekly meeting export"
uv run code-loops new path/to/postmortems/2026-05-06_timeout_incident.md
uv run code-loops new ~/notes/feature_idea.md
```
Mode (`feature` vs `from_problem`) is auto-detected from path keywords
(`problem`, `postmortem`, `incident`) and content markers (`## Postmortem`,
`## Incident`, `## Problem`, `## Symptoms`, etc — Russian equivalents
also recognized for bilingual input).

Output: `tasks/<NNNN>_<slug>/` with `task.md` and `meta.yaml`.

### Run the pipeline
```bash
uv run code-loops run <task_id>
```
Or with explicit project: `--project <name>`. Pipeline streams progress
to your terminal and pauses at human-review checkpoints.

### Other commands
```bash
uv run code-loops projects              # list configured projects
uv run code-loops list                  # list all tasks (status + cost)
uv run code-loops status <task_id>      # single-task progress + cost
uv run code-loops commit <task_id>      # print branch + push instructions for a completed task
uv run code-loops cancel <task_id>      # mark task cancelled (artifacts preserved)
uv run code-loops resurvey <name>       # refresh brief.md after material project changes
uv run code-loops eval                  # pipeline-evaluator: meta-analysis over recent runs
```

---

## Architecture

### Layout

```
code-loops/
├── pyproject.toml          # package metadata + `code-loops` entry point
├── src/code_loops/         # the package (ships in the wheel as installed data)
│   ├── pipeline.yaml       #   ⭐ stage definitions (12 stages, types, role bindings)
│   ├── agents/             #   26 agent prompts in 6 family folders:
│   │   ├── strategy/       #     business-analyst, tech-lead
│   │   ├── research/       #     research-lead + 5 researchers
│   │   ├── architects/     #     software-architect + 7 architect-* (perspective, arbiters, 4 critics)
│   │   ├── engineering/    #     qa, software, code-reviewer, triage, prompt, eval engineers
│   │   ├── release/        #     release-manager, tech-writer
│   │   └── meta/           #     pipeline-evaluator, project-surveyor
│   ├── engine.py           #   orchestrator: loads pipeline, dispatches by stage type,
│   │                       #   handles auto-loops (redesign_needed, needs_more_work)
│   ├── runner.py           #   ClaudeRunner — claude --print subprocess wrapper
│   ├── project_loader.py   #   project.yaml loader + brief injection
│   ├── meta.py             #   per-task meta.yaml (status, cost, durations)
│   ├── worktree.py         #   git worktree mgmt + configurable test-file protection
│   ├── isolation.py        #   research-question slicing (one researcher = one tag)
│   ├── human_review.py     #   checkpoint UI (approve/abort/revise)
│   ├── eval_aggregator.py  #   cross-run aggregation for pipeline-evaluator
│   ├── cli.py              #   typer entry point (exposed as `code-loops` command)
│   └── stages/             #   stage handlers (one per `type:` in pipeline.yaml):
│       ├── prompt.py, parallel.py, debate_writer.py, debate_critique.py,
│       ├── impl_planner.py, subtask_iterator.py, action.py,
│       ├── final_validation.py, regression_check.py, final_review.py,
│       ├── tech_writer.py,
│       └── auto_resurvey.py  # Stage 12 — conditional brief.md refresh
├── examples/               # starter templates (project.yaml)
├── scripts/                # CI helpers (e.g. check_no_leakage.sh)
└── tests/                  # 214 pytest tests (orchestrator only)

# WORKSPACE (created in user's CWD when running code-loops):
<workspace>/
├── projects/<name>/        # per-project config + auto-generated brief
│   ├── project.yaml        #   name + base_repo + optional test_infrastructure
│   └── brief.md            #   auto-generated project knowledge
├── tasks/<NNNN>_<slug>/    # per-task workspaces
│   ├── task.md, meta.yaml
│   ├── prd/, research_plan/, research/, design/, design_review/,
│   │   impl_plan/, implementation/, validation/, release_review/, docs/
│   └── worktree/wt/        # git worktree off base_repo
└── _eval/                  # pipeline-evaluator reports
```

### Pipeline definition (`pipeline.yaml`)

Each stage declares `name` (semantic id), `type` (engine handler),
prompts, inputs, outputs, and optional `human_review: true`,
`max_rounds: N`. See `pipeline.yaml` for the full 12-stage definition;
top of file documents the schema. A `defaults:` block sets
`model + effort` for all stages (override per-role if needed).

### Project profile (`projects/<name>/project.yaml`)

Minimal schema (defaults preserve sensible behavior):

```yaml
project:
  name: my-project
  base_repo: /absolute/path/to/your/project

# Optional — markdown file with project-specific architecture/conventions.
# Auto-generated by `code-loops resurvey` (project-surveyor agent).
brief_file: brief.md

# Optional — test infrastructure config (defaults below preserve prior behavior).
test_infrastructure:
  enabled: true                    # false → skip test_writer entirely (manual-QA projects)
  test_paths: [tests]              # dirs the coder MUST NOT touch
  lock_strategy: chmod_444_dir     # | none (no chmod, only git-diff guard)
```

See `examples/project.yaml` for full annotated template.

### Agent prompts (`agents/`)

25 markdown files, one per agent. Each has:
- Role identity opening line
- `## Project context` block with `{PROJECT_BRIEF}` placeholder
  (auto-substituted at load time)
- Domain-specific scan plan / output schema / rules

Customize freely — agents are markdown, not code. Pipeline.yaml binds
agents to roles by file path.

---

## Customize for your project

### Different test infrastructure

Edit `projects/<name>/project.yaml`:

```yaml
test_infrastructure:
  enabled: true
  test_paths: [src/test, e2e]      # multiple test dirs
  lock_strategy: chmod_444_dir     # | none
```

For projects with embedded tests (Rust `#[cfg(test)]`, Go `*_test.go`
colocated): set `lock_strategy: none` — git-diff guard remains active
as a safety net even without chmod. Glob-based locking
(`chmod_444_glob`) and embedded-test detection are deferred until real
non-Python projects exercise the pipeline.

### Refresh project brief
After material project changes (new modules, new dependencies, new
conventions, renamed dirs), regenerate the brief:
```bash
uv run code-loops resurvey <name>
```
The `tech-writer` stage automatically flags resurvey need in
`tasks/<id>/docs/maintenance_notes.md` after each task ships.

### Tune agents
Every agent prompt is a markdown file under `agents/`. To change agent
behavior: edit the file, run a task, observe. No engine restart needed.
For systematic A/B comparison run `code-loops eval` — `pipeline-evaluator`
detects prompt diffs in git and computes Cohen's d / p-values across
recent runs.

### Add a new project
```bash
uv run code-loops init /path/to/another/project --name backend-api
```
Each project lives in its own `projects/<name>/` dir. Use
`--project backend-api` on subsequent commands to disambiguate.

---

## Costs

Typical per-task spend (Opus-4.7 at max effort, 2026 pricing):

| Stage | Cost (typical) | Notes |
|---|---|---|
| PRD | $0.05–0.15 | Single Opus call |
| Research plan | $0.05–0.10 | Single Opus call |
| Research (5 parallel) | $0.30–1.00 | 5 specialists, deep scans |
| Design (RFC debate) | $1–4 | 2–5 rounds × (writer + perspectives + arbiter) |
| Design Review (critique) | $0.60–2.50 | 1–3 rounds × (4 critics + responder + arbiter) |
| Impl Plan | $0.20–0.50 | Tech-lead decomposition |
| Implementation | $1–5 | per subtask × subtask count, depends on fix-loop iterations |
| Validation | $0.00 | Programmatic only |
| Regression | $0.00 | Programmatic; off by default. When on, runs project's eval bench. |
| Release Review | $0.30–1 | Single release-manager call |
| Release Docs | $0.05–0.20 | tech-writer |
| Auto Resurvey | $0–3 | $0 if brief stays accurate (typical); $0.30–3 if tech-writer flagged staleness |
| **Total per task** | **$3–18** | Varies hugely with task complexity |

Project-surveyor on init: $0.30–3.00 once per project (re-runs only
when Stage 11 auto-fires or you call `code-loops resurvey` manually).

To reduce cost: override `model` to `claude-sonnet-4-6` per-stage in
`pipeline.yaml` for stages where Opus is overkill (research / debate
critics / facilitator). The `pipeline-evaluator` (Mode B) helps
identify which stages tolerate downgrade.

---

## Observability & quality monitoring

Every run writes `tasks/<id>/meta.yaml` with per-stage cost, duration,
attempts, and verdicts. Run:

```bash
uv run code-loops eval --last 20
```

…to invoke the `pipeline-evaluator` agent (Mode B). It aggregates
recent runs and produces a report covering:

- **Convergence rate** — % runs reaching `release_review.approved` first-pass
- **Per-stage retry rate** — debate rounds, fix-router bounces
- **Code-quality scorecard trend** — 5-axis weighted (correctness /
  maintainability / performance / security / best-practices)
- **A/B prompt comparison** — when `agents/<role>.md` changed in git,
  computes χ² + Welch's t-test + Cohen's d across before/after runs
- **Hallucination rate per agent** — scans cited file paths and grep-
  verifies they exist in the target project
- **Context-length × degradation tracking** — flags stages exceeding 70%
  of model's safe context range (RULER thresholds for Claude Opus 4.5
  ~100K, Sonnet 4.5 ~80K)
- **Pass@k tracking** — for AI-touching subtasks with golden eval files

Reports land at `_eval/report_<timestamp>.md`.

---

## Roadmap

**Done**:
- Full 12-stage pipeline with auto-loops (redesign_needed, needs_more_work, auto-resurvey) and conditional regression gate
- 26 agents in 6 families with `{PROJECT_BRIEF}` injection
- Configurable test infrastructure (Python `tests/` default; pluggable)
- `init` / `resurvey` / `projects` / `eval` CLI commands
- 195 pytest tests covering orchestrator + worktree + agents

**Next**:
- **Multi-language support** — today the validation stage and several
  agent prompts hardcode `pytest` + `ruff`. Move test/lint/typecheck
  commands into `project.yaml` so NodeJS / Go / Rust / etc projects can
  configure their own. ([#1](#))
- `chmod_444_glob` (Go-style `*_test.go`), `git_diff_only` (Rust
  embedded tests), surveyor auto-detect of test paths into
  `project.yaml`
- Real parallel subtask execution (currently sequential even for
  wave-marked subtasks; deferred until measured wall-clock pain)
- Shared prompt blocks (language rule, Iron Law, revision mode) —
  deferred until first production runs measure real drift

**Not planned**:
- Built-in support for non-Anthropic LLM providers — we use Claude CLI
  exclusively. PRs welcome if there's interest.

---

## Contributing

This is a personal tool that grew into something potentially useful.
PRs welcome but expect:
- Strict pre-commit gate: `uv run pytest && uv run ruff check .`
- New stage handlers / agent prompts must include tests + a
  rationale in the PR description (why this addition vs simpler
  alternatives).
- Backward-compat preserved by default — additive changes only unless
  there's a clear migration path.

---

## License

MIT — see [`LICENSE`](LICENSE).

---

## Acknowledgments

Pipeline shape inspired by patterns from
[obra/superpowers](https://github.com/obra/superpowers),
[awesome-ai-dev-prompts](https://github.com/piyushrajyadav/awesome-ai-dev-prompts),
[xfstudio/skills](https://github.com/xfstudio/skills),
[Fandry96/k3-agentic-skills](https://github.com/Fandry96/k3-agentic-skills),
and [Agent Skills for Context Engineering](https://github.com/muratcankoylan/Agent-Skills-for-Context-Engineering).
