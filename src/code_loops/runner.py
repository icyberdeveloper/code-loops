"""ClaudeRunner — wraps `claude --print` subprocess.

Synchronous: blocks until the subprocess returns a result. Cost and token usage
are extracted from the stream-json `result` event.

Retries: transient failures (most commonly OOM-SIGKILL when running N parallel
agents on a small VM, signature = rc!=0 + empty stderr) are retried up to
MAX_RETRIES times with exponential backoff. Auth/quota failures (non-empty
stderr with recognized patterns) and timeouts are NOT retried.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field

# Retry policy for transient subprocess failures (OOM-SIGKILL, network blip).
# Total worst-case added latency: 5 + 15 = 20s before final failure.
MAX_RETRIES = 3  # total attempts including the initial one
RETRY_BACKOFF_S = (5, 15)  # delays between attempts (attempt 1->2, attempt 2->3)

# stderr substrings that indicate a NON-transient failure — don't retry these.
NON_RETRYABLE_STDERR_MARKERS = (
    "Invalid API key",
    "authentication",
    "Authentication",
    "rate limit",
    "Rate limit",
    "quota",
    "Quota",
    "permission",
    "403",
    "401",
)


@dataclass
class RunnerResult:
    text: str
    cost_usd: float | None = None
    duration_s: float = 0.0
    in_tokens: int | None = None
    out_tokens: int | None = None
    # Распарсенный JSON если вызов был с output_schema (constrained decoding).
    # Когда задан — `text` всё ещё содержит сырой output (для логов / отладки),
    # `parsed_json` — уже распарсенный dict готовый к использованию.
    parsed_json: dict | None = None
    # Список tool_use/tool_result событий из stream-json для аудита.
    # Каждый элемент: {"name": "Bash", "input": {...}, "output": "..."}.
    # Critic на этапе ревью может использовать это как ground truth
    # против фабрикации в финальном тексте модели.
    tool_events: list[dict] = field(default_factory=list)


class RunnerError(RuntimeError):
    pass


class RunnerFactory:
    """Builds ClaudeRunner instances honoring per-stage / per-role overrides.

    Defaults: Opus 4.7 with max effort. A stage or role spec may override
    `model` and/or `effort` — typically Sonnet for cheap critique/judgment
    roles in debate stages.
    """

    def __init__(
        self,
        default_model: str = "claude-opus-4-7",
        default_effort: str = "max",
    ):
        self.default_model = default_model
        self.default_effort = default_effort

    def make(self, spec: dict | None = None) -> ClaudeRunner:
        spec = spec or {}
        return ClaudeRunner(
            model=spec.get("model", self.default_model),
            effort=spec.get("effort", self.default_effort),
        )


class ClaudeRunner:
    def __init__(
        self,
        model: str = "claude-opus-4-7",
        effort: str = "max",
        # Раньше 1200s, поднято до 2400s после run #5 retry:
        # constrained decoding (--json-schema) добавляет grammar compilation
        # overhead ~5s per cold call + увеличивает latency финального
        # token sampling. Pass_2 design ~17 мин average per writer call,
        # один pass_3 call не уложился в 1200s. 2400s даёт headroom для
        # больших RFC drafts при sustained constrained decoding.
        timeout_s: int = 2400,
    ):
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s

    def run(
        self,
        system_prompt: str,
        user_message: str,
        *,
        cwd: str | None = None,
        output_schema: dict | None = None,
        allowed_tools: list[str] | None = None,
    ) -> RunnerResult:
        """Запускает claude CLI.

        output_schema: JSON Schema (dict). Когда задан — добавляется
        --json-schema, CLI делает constrained decoding на уровне токенов
        (grammar-constrained sampling, см. Anthropic docs про structured
        outputs). Финальный text гарантированно валидный JSON.

        allowed_tools: явный список разрешённых tools (например
        ["Bash", "Read", "Grep", "Glob"]). Когда задан — добавляется
        --allowed-tools, агент не сможет использовать tools вне списка.
        """
        cmd = [
            "claude",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",  # required by claude when output-format is stream-json
            "--model",
            self.model,
            "--effort",
            self.effort,
            # Controlled pipeline subprocess — agents need autonomous tool use
            # (Read/Grep for researchers; Edit/Write/Bash for coder later).
            "--dangerously-skip-permissions",
            "--append-system-prompt",
            system_prompt,
        ]
        if output_schema is not None:
            cmd.extend(["--json-schema", json.dumps(output_schema)])
        if allowed_tools is not None:
            # CLI принимает comma-separated либо space-separated; передаём
            # comma-separated одной строкой чтобы избежать неоднозначности
            # с дальнейшими аргументами.
            cmd.extend(["--allowed-tools", ",".join(allowed_tools)])
        last_err: RunnerError | None = None
        for attempt_idx in range(MAX_RETRIES):
            start = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    input=user_message,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_s,
                    cwd=str(cwd) if cwd else None,
                )
            except subprocess.TimeoutExpired as e:
                # Timeouts are operation-level — don't retry, the operator
                # likely needs to raise timeout_s or simplify the prompt.
                raise RunnerError(f"claude timed out after {self.timeout_s}s") from e
            duration = time.monotonic() - start
            if proc.returncode == 0:
                return _parse_stream_json(
                    proc.stdout, duration, expect_json=output_schema is not None
                )

            # Non-zero rc — decide retry vs raise.
            err_msg = f"claude exited rc={proc.returncode}\nstderr: {proc.stderr[:2000]}"
            if _is_non_retryable(proc.stderr):
                raise RunnerError(err_msg)
            last_err = RunnerError(err_msg)
            if attempt_idx < MAX_RETRIES - 1:
                delay = RETRY_BACKOFF_S[attempt_idx]
                # Brief signal to stdout so the operator sees retries happening
                # in the live run log. stderr is preserved in last_err.
                print(
                    f"  [runner] rc={proc.returncode} (likely transient OOM); "
                    f"retrying in {delay}s (attempt {attempt_idx + 2}/{MAX_RETRIES})"
                )
                time.sleep(delay)
        assert last_err is not None
        raise last_err


def _is_non_retryable(stderr: str) -> bool:
    """Return True if stderr indicates an auth/quota/permission failure that
    retrying won't fix. Empty stderr → retryable (typical OOM-SIGKILL signature).
    """
    if not stderr:
        return False
    return any(marker in stderr for marker in NON_RETRYABLE_STDERR_MARKERS)


def _parse_stream_json(stdout: str, duration: float, *, expect_json: bool = False) -> RunnerResult:
    """Парсит stream-json вывод claude CLI.

    Собирает:
      - text chunks (для финального result.text)
      - tool_use блоки и сопоставляет их с tool_result по id
        → RunnerResult.tool_events используется critic'ом для аудита

    expect_json: когда True (вызов был с output_schema), Claude CLI
    реализует --json-schema через специальный StructuredOutput tool.
    Реальный JSON output приходит в:
      1. event.result.structured_output (предпочтительно — final source)
      2. assistant.content[type=tool_use].name == "StructuredOutput" .input
         (backup — если result.structured_output отсутствует)
    Финальный result.result это **текстовый** ответ ассистента ПОСЛЕ
    StructuredOutput tool call, не сам JSON. Не парсить его как JSON.
    """
    text_chunks: list[str] = []
    cost: float | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    final_result: str | None = None
    structured_output_from_result: dict | None = None
    structured_output_from_tool: dict | None = None
    # Сопоставление tool_use_id → запись о tool call (для парного tool_result).
    tool_calls: dict[str, dict] = {}
    # Порядок вызовов — важен для аудита: critic видит хронологию.
    tool_call_order: list[str] = []

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        if etype == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                btype = block.get("type")
                if btype == "text":
                    text_chunks.append(block.get("text", ""))
                elif btype == "tool_use":
                    tu_id = block.get("id")
                    name = block.get("name", "")
                    block_input = block.get("input", {})
                    # StructuredOutput tool — это путь --json-schema в CLI.
                    # Его input и есть наш JSON output. Не считаем его как
                    # обычный tool_event (это не grep/bash который критик
                    # должен аудировать — это сама финальная структура).
                    if name == "StructuredOutput" and isinstance(block_input, dict):
                        structured_output_from_tool = block_input
                        continue
                    if tu_id:
                        tool_calls[tu_id] = {
                            "name": name,
                            "input": block_input,
                            "output": None,
                        }
                        tool_call_order.append(tu_id)
        elif etype == "user":
            # tool_result события приходят как user message content blocks
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "tool_result":
                    tu_id = block.get("tool_use_id")
                    content = block.get("content")
                    # content может быть строкой или списком {type: text, text: ...}
                    if isinstance(content, list):
                        content = "\n".join(
                            c.get("text", "") for c in content if c.get("type") == "text"
                        )
                    if tu_id and tu_id in tool_calls:
                        tool_calls[tu_id]["output"] = content
        elif etype == "result":
            cost = event.get("total_cost_usd") or event.get("cost_usd")
            usage = event.get("usage", {}) or {}
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            if isinstance(event.get("result"), str):
                final_result = event["result"]
            # structured_output — авторитетный источник JSON output (см.
            # docstring). Заполняется CLI'ем когда --json-schema задан.
            so = event.get("structured_output")
            if isinstance(so, dict):
                structured_output_from_result = so

    text = final_result if final_result is not None else "".join(text_chunks)
    parsed_json: dict | None = None
    if expect_json:
        # Приоритет: structured_output из result (definitive). Fallback:
        # StructuredOutput tool_use.input (если result поле почему-то
        # отсутствует — теоретически возможно при streaming артефактах).
        # НЕ парсим text — он содержит обычный ответ ассистента, не JSON.
        if structured_output_from_result is not None:
            parsed_json = structured_output_from_result
        elif structured_output_from_tool is not None:
            parsed_json = structured_output_from_tool

    return RunnerResult(
        text=text,
        cost_usd=cost,
        duration_s=duration,
        in_tokens=in_tok,
        out_tokens=out_tok,
        parsed_json=parsed_json,
        tool_events=[tool_calls[tid] for tid in tool_call_order],
    )
