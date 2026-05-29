"""Shared test fixtures."""

from __future__ import annotations

from dotenv import load_dotenv

# Load .env при тестовом startup'е — integration тесты (gated за
# RUN_INTEGRATION=1) нуждаются в OPENAI_API_KEY. Unit-тесты mock'аются
# через monkeypatch.setenv и не зависят от .env. Loading idempotent +
# override=False — shell exports выигрывают.
load_dotenv()

from code_loops.runner import RunnerResult  # noqa: E402


class FakeFactory:
    """RunnerFactory stand-in: always returns the wrapped runner, ignoring overrides."""

    def __init__(self, runner):
        self.runner = runner

    def make(self, spec: dict | None = None):
        return self.runner


class MockClaudeRunner:
    """Mock runner для unit/integration тестов pipeline без real subprocess.

    Configurable per-role responses via `responses` dict (role_name → list of
    RunnerResult or string). Each `run()` call pops next response для that role.
    If role detection fails, falls back к `default_responses` queue.

    Captures all invocations в `calls` list для assertions.
    """

    def __init__(
        self,
        responses: dict[str, list] | None = None,
        default_responses: list | None = None,
        default_cost: float = 0.01,
    ):
        self.responses = {k: list(v) for k, v in (responses or {}).items()}
        self.default_responses = list(default_responses or [])
        self.default_cost = default_cost
        self.calls: list[dict] = []

    def _detect_role(self, system_prompt: str) -> str:
        """Heuristic: agent prompts contain `# RoleName` или `You are the **X**`."""
        import re

        m = re.search(r"You are the \*\*([A-Za-z _-]+)\*\*", system_prompt[:300])
        if m:
            return m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        m = re.search(r"^#\s+([A-Za-z _-]+)", system_prompt[:300], re.MULTILINE)
        if m:
            return m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        return "_default"

    def run(
        self,
        system_prompt: str,
        user_message: str,
        *,
        cwd: str | None = None,
        output_schema: dict | None = None,
        allowed_tools: list[str] | None = None,
    ) -> RunnerResult:
        role = self._detect_role(system_prompt)
        self.calls.append(
            {
                "role": role,
                "system_prompt": system_prompt,
                "user_message": user_message,
                "cwd": cwd,
                "output_schema": output_schema,
                "allowed_tools": allowed_tools,
            }
        )
        queue = self.responses.get(role) or self.default_responses
        if not queue:
            return RunnerResult(text="(mock: no response queued)", cost_usd=self.default_cost)
        nxt = queue.pop(0)
        if isinstance(nxt, str):
            return RunnerResult(text=nxt, cost_usd=self.default_cost)
        return nxt


__all__ = ["FakeFactory", "MockClaudeRunner", "RunnerResult"]
