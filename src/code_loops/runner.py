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
from dataclasses import dataclass

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
        timeout_s: int = 1200,
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
    ) -> RunnerResult:
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
                return _parse_stream_json(proc.stdout, duration)

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


def _parse_stream_json(stdout: str, duration: float) -> RunnerResult:
    text_chunks: list[str] = []
    cost: float | None = None
    in_tok: int | None = None
    out_tok: int | None = None
    final_result: str | None = None

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
                if block.get("type") == "text":
                    text_chunks.append(block.get("text", ""))
        elif etype == "result":
            cost = event.get("total_cost_usd") or event.get("cost_usd")
            usage = event.get("usage", {}) or {}
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            if isinstance(event.get("result"), str):
                final_result = event["result"]

    text = final_result if final_result is not None else "".join(text_chunks)
    return RunnerResult(
        text=text,
        cost_usd=cost,
        duration_s=duration,
        in_tokens=in_tok,
        out_tokens=out_tok,
    )
