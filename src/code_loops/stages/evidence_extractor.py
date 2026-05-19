"""EvidenceExtractorStage — Phase 1 архитектор-цепочки.

Архитектор-агент исторически фабрикует ссылки на код в RFC (см. RCA в
run #4: 13 фабрикаций за 6 проходов, recurring темы
unverified_api_references_in_spec / tamper_prone_narrative_verification).
Корневая причина по research (Kalai/Nachum, Chen/Reasoning Trap, Goldberg):
LLM это plausibility maximizer, narrative markdown — это поверхность
которую модель структурно способна фабриковать, и никакие правила в
промпте этого не закрывают.

Решение (industry consensus, Anthropic-recommended): evidence-first /
compose-after split. Этот stage — фаза evidence:

  1. Архитектор-evidence получает PRD + research + tool access
     (Bash, Read, Grep, Glob), НЕ имеет права писать RFC.
  2. Output через --json-schema (constrained decoding) — JSON
     {verified_files, verified_symbols, file_quotes, summary}.
  3. Constrained decoding гарантирует валидную структуру; tool calls
     дают ground truth.

Stage `design` (debate_writer) в фазе compose использует evidence.json
для второго constraint — file_path enum строится из verified_files,
архитектор-compose физически не может ссылаться на файлы вне evidence.
"""

from __future__ import annotations

import json
import time

from rich.console import Console

from ..runner import RunnerFactory
from .prompt import StageContext, load_agent_prompt

console = Console()

# JSON Schema для evidence output. Phase 1 — нет enum constraint на file
# (Phase 1 сама определяет что верифицировать). enum появится только в
# Phase 2 (compose), где verified_files из evidence станут enum для
# file_path в RFC.
EVIDENCE_SCHEMA = {
    "type": "object",
    "required": ["verified_files", "verified_symbols", "summary"],
    "properties": {
        "verified_files": {
            "type": "array",
            "description": (
                "Список путей к файлам которые ты реально открыл/прогрепал. "
                "Только файлы которые ты сам видел через Read/Grep. "
                "Не угадывай — пустой список лучше выдумки."
            ),
            "items": {"type": "string"},
        },
        "verified_symbols": {
            "type": "array",
            "description": (
                "Каждый символ (функция/класс/метод/свойство/константа) "
                "который ты подтвердил через grep или read. "
                "Cite только то что реально нашёл."
            ),
            "items": {
                "type": "object",
                "required": ["name", "file"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": (
                            "Точное имя символа как в коде. Например "
                            "'_collect_surnames', 'Person.confidence_level'."
                        ),
                    },
                    "file": {
                        "type": "string",
                        "description": "Путь относительно base_repo.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "Номер строки где символ определён.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": [
                            "function",
                            "method",
                            "class",
                            "property",
                            "constant",
                            "variable",
                            "module",
                            "decorator",
                        ],
                    },
                },
            },
        },
        "file_quotes": {
            "type": "array",
            "description": (
                "Verbatim фрагменты кода которые ты прочитал через Read. "
                "Полезны для compose-фазы как контекст для решений."
            ),
            "items": {
                "type": "object",
                "required": ["file", "lines", "content"],
                "properties": {
                    "file": {"type": "string"},
                    "lines": {
                        "type": "string",
                        "description": "Например '109-140' или '371'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Точный текст из файла, без редактирования.",
                    },
                },
            },
        },
        "summary": {
            "type": "string",
            "description": (
                "Краткое (3-6 предложений) описание того что ты понял "
                "о структуре релевантного кода: где живёт проблемная "
                "функциональность, какие entry points, какие callers. "
                "Это саммари читает архитектор-compose как контекст. "
                "Не дублируй verified_symbols — пиши высокоуровневое."
            ),
        },
    },
}

# Tools которые архитектор-evidence может использовать. Edit/Write
# намеренно исключены — Phase 1 read-only.
EVIDENCE_ALLOWED_TOOLS = ["Bash", "Read", "Grep", "Glob"]


class EvidenceExtractorStage:
    def __init__(self, factory: RunnerFactory):
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        out_dir = ctx.task_dir / "design"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Per-pass scoping: при redesign loop evidence фаза прогоняется заново
        # с обновлённым redesign_signal в контексте. Сохраняем по pass'ам.
        if ctx.artifact_writer is not None:
            pass_n = ctx.artifact_writer.manifest.data.get("redesign_loop_count", 0) + 1
            work_dir = out_dir / f"pass_{pass_n}"
        else:
            pass_n = 1
            work_dir = out_dir
        work_dir.mkdir(parents=True, exist_ok=True)

        # Готовим input: PRD + research, опционально redesign_signal.
        task_md = (ctx.task_dir / "task.md").read_text()
        prd_md = (ctx.task_dir / "prd" / "prd.md").read_text()
        plan_md = (ctx.task_dir / "research_plan" / "plan.md").read_text()
        research_dir = ctx.task_dir / "research"
        research_blocks = "\n\n".join(
            f"=== research/{p.name} ===\n{p.read_text()}" for p in sorted(research_dir.glob("*.md"))
        )

        redesign_signal_path = ctx.task_dir / "design" / "redesign_signal.md"
        redesign_block = ""
        if redesign_signal_path.exists():
            redesign_block = (
                f"=== redesign_signal.md (PREVIOUS ATTEMPT WAS REJECTED) ===\n"
                f"{redesign_signal_path.read_text()}\n\n"
                f"This is a redesign pass — focus evidence collection on the "
                f"areas the redesign_signal indicates need a different shape.\n\n"
            )

        sys_prompt = load_agent_prompt(ctx.repo_root / stage_def["prompt"], ctx)
        user_msg = (
            f"=== task.md ===\n{task_md}\n\n"
            f"=== prd/prd.md ===\n{prd_md}\n\n"
            f"=== research_plan/plan.md ===\n{plan_md}\n\n"
            f"{research_blocks}\n\n"
            f"{redesign_block}"
            "Use your tools (Bash/Read/Grep/Glob) to verify every symbol / file / "
            "behaviour you intend to cite. Then emit the evidence JSON. Constrained "
            "decoding will reject malformed output — but it cannot reject "
            "factually wrong claims. Verify everything you put in verified_symbols "
            "and verified_files; an empty list is correct when you cannot confirm."
        )

        runner = self.factory.make(stage_def)
        wall_start = time.monotonic()
        console.print("  [dim]evidence:[/dim] архитектор грепает кодовую базу...")
        result = runner.run(
            sys_prompt,
            user_msg,
            output_schema=EVIDENCE_SCHEMA,
            allowed_tools=EVIDENCE_ALLOWED_TOOLS,
        )
        wall_duration = time.monotonic() - wall_start

        if result.parsed_json is None:
            # Constrained decoding должен был гарантировать валидный JSON.
            # Если parsed_json пуст — что-то пошло не так (refusal? CLI bug?).
            # Сохраняем сырой text для отладки.
            (work_dir / "evidence_raw.txt").write_text(result.text)
            raise RuntimeError(
                f"Evidence extractor returned non-JSON despite output_schema. "
                f"Raw output saved to {work_dir / 'evidence_raw.txt'}. "
                f"text head: {result.text[:200]}"
            )

        evidence_path = work_dir / "evidence.json"
        evidence_path.write_text(json.dumps(result.parsed_json, indent=2, ensure_ascii=False))

        # Также сохраняем tool transcript для аудита и для critic'ов
        # (дополнительный ground truth поверх evidence.json).
        if result.tool_events:
            transcript_lines = ["# Architect evidence tool transcript", ""]
            for i, ev in enumerate(result.tool_events, 1):
                transcript_lines.append(f"## Tool call #{i}: {ev['name']}")
                transcript_lines.append("### Input")
                transcript_lines.append("```json")
                transcript_lines.append(json.dumps(ev["input"], indent=2, ensure_ascii=False))
                transcript_lines.append("```")
                transcript_lines.append("### Output")
                transcript_lines.append("```")
                transcript_lines.append(str(ev.get("output", "(no output captured)")))
                transcript_lines.append("```")
                transcript_lines.append("")
            (work_dir / "evidence_tool_transcript.md").write_text("\n".join(transcript_lines))

        n_files = len(result.parsed_json.get("verified_files", []))
        n_symbols = len(result.parsed_json.get("verified_symbols", []))
        n_quotes = len(result.parsed_json.get("file_quotes", []))
        n_tools = len(result.tool_events)
        console.print(
            f"  [green]evidence:[/green] {n_files} файлов / {n_symbols} символов / "
            f"{n_quotes} цитат / {n_tools} tool calls "
            f"({wall_duration:.0f}s, ${result.cost_usd or 0:.2f})"
        )

        return {
            "outputs": {
                f"design/pass_{pass_n}/evidence.json": evidence_path.read_text(),
            },
            "cost_usd": result.cost_usd,
            "duration_s": wall_duration,
            "in_tokens": result.in_tokens,
            "out_tokens": result.out_tokens,
            "evidence": result.parsed_json,
            "tool_events_count": n_tools,
        }
