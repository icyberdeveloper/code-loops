"""ClaudeRunner — wraps `claude --print` subprocess.

Synchronous: blocks until the subprocess returns a result. Cost and token usage
are extracted from the stream-json `result` event.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


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
            raise RunnerError(f"claude timed out after {self.timeout_s}s") from e
        duration = time.monotonic() - start
        if proc.returncode != 0:
            raise RunnerError(f"claude exited rc={proc.returncode}\nstderr: {proc.stderr[:2000]}")
        return _parse_stream_json(proc.stdout, duration)


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
