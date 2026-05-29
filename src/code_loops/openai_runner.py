"""OpenAIRunner — wraps OpenAI Chat Completions API для cross-family judge.

Real cross-family per MACI (arxiv 2510.04488): different LLM family judge
breaks self-confirming bias loops от same-family critics. Anthropic Claude
critics + OpenAI GPT facilitator = actual cross-family calibration, не
same-family size delta (Sonnet vs Opus).

Cost: OpenAI usage возвращает только token counts (нет cost field как у
Claude CLI). Approximated via PRICING_USD_PER_M ниже — это **APPROXIMATE
estimates**, не verified против OpenAI billing. Real cost от dashboard
authoritative. Update таблицы когда OpenAI меняет tiers. Unknown model →
cost_usd=None; downstream treats как $0 (manifest accepts None).

Reasoning effort: для reasoning models (GPT-5, o3, o1) effort из spec
маппируется в OpenAI `reasoning_effort` parameter. Non-reasoning models
(gpt-4.1, gpt-4o) — effort ignored (API rejects unknown param).

Scope ограничен: для facilitator/judge roles только. Tools не поддержаны
(allowed_tools raises) — critics/researchers продолжают использовать
ClaudeRunner с full tool surface. Этот provider предназначен для
text-generation + structured JSON output, не для agent loops.
"""

from __future__ import annotations

import json
import os
import sys
import time

from .runner import RunnerError, RunnerResult

# APPROXIMATE pricing per 1M tokens (USD). НЕ verified против actual OpenAI
# billing — это reasonable estimates extrapolated from GPT-4 family. Update
# с https://openai.com/pricing когда нужны authoritative cost numbers. Если
# manifest cost критичен — использовать OpenAI dashboard как source of truth.
# Unknown model → _estimate_cost returns None, и OpenAIRunner.__init__ emits
# one-time stderr warning.
PRICING_USD_PER_M: dict[str, tuple[float, float]] = {
    # GPT-5 family (flagship reasoning, released 2026-Q1) — estimates
    "gpt-5": (10.0, 30.0),
    "gpt-5-mini": (2.0, 8.0),
    "gpt-5-nano": (0.5, 2.0),
    # GPT-4.1 (стабильный prior-gen)
    "gpt-4.1": (5.0, 15.0),
    "gpt-4.1-mini": (1.5, 6.0),
    # o-series reasoning
    "o3": (15.0, 60.0),
    "o3-mini": (3.0, 12.0),
}

# Models supporting OpenAI `reasoning_effort` parameter. Non-reasoning models
# (gpt-4.1, gpt-4o, ...) reject unknown params with 400 — поэтому мы передаём
# reasoning_effort только когда model в этом set.
_REASONING_MODEL_PREFIXES = ("gpt-5", "o3", "o1")

# Mapping pipeline.yaml `effort` field → OpenAI reasoning_effort enum.
# Claude использует "max" / "high" / "medium" / "low" / "none". OpenAI:
# "minimal" / "low" / "medium" / "high". "max" considered → "high".
_EFFORT_MAP = {
    "max": "high",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "minimal": "minimal",
    "none": "minimal",
}


def _is_reasoning_model(model: str) -> bool:
    return any(model.startswith(prefix) for prefix in _REASONING_MODEL_PREFIXES)


def _map_effort_to_reasoning(effort: str) -> str:
    return _EFFORT_MAP.get(effort.lower(), "medium")


# Set чтобы emit "unknown pricing" warning только один раз per model per
# process. Без cache — каждый OpenAIRunner instance в pipeline пишет дубль.
_PRICING_WARNED: set[str] = set()


class OpenAIRunner:
    """OpenAI provider с интерфейсом совместимым с ClaudeRunner.run()."""

    def __init__(
        self,
        model: str = "gpt-5",
        effort: str = "max",
        timeout_s: int = 3600,
    ):
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        if not os.environ.get("OPENAI_API_KEY"):
            raise RunnerError(
                "OPENAI_API_KEY environment variable not set. Required for OpenAI "
                "provider. Export it before running the pipeline."
            )
        # One-time warning per process если model unknown в PRICING_USD_PER_M —
        # cost будет None, manifest показывает $0 что искажает aggregate cost
        # reporting. Не fatal: provider работает, просто без cost tracking.
        if model not in PRICING_USD_PER_M and model not in _PRICING_WARNED:
            _PRICING_WARNED.add(model)
            print(
                f"  [openai_runner] WARNING: model '{model}' not in pricing table — "
                "cost_usd will be None for this provider. Add to PRICING_USD_PER_M "
                "or treat OpenAI dashboard as source of truth.",
                file=sys.stderr,
            )
        # Lazy import: openai SDK добавляет ~150ms startup overhead. Импорт
        # внутри __init__ значит он платится только теми pipelines которые
        # actually wire OpenAI role (cross-family facilitator).
        from openai import OpenAI

        self.client = OpenAI(timeout=timeout_s)

    def run(
        self,
        system_prompt: str,
        user_message: str,
        *,
        cwd: str | None = None,
        output_schema: dict | None = None,
        allowed_tools: list[str] | None = None,
    ) -> RunnerResult:
        """Generates response через OpenAI Chat Completions API.

        output_schema: когда задан — uses native OpenAI structured outputs
        (response_format=json_schema, strict mode). Возвращаемый text это
        валидный JSON, parsed_json заполнен.

        allowed_tools: НЕ поддержан — OpenAIRunner для tools-free roles
        (facilitator, judge). Raises если задан.

        cwd: игнорируется (OpenAI API не имеет concept of working directory).
        """
        if allowed_tools:
            raise RunnerError(
                f"OpenAIRunner does not support allowed_tools={allowed_tools}. "
                "Use Claude provider for roles that need tool use (evidence, "
                "researchers, coders)."
            )

        kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }
        # reasoning_effort только для models которые его поддерживают (GPT-5,
        # o3, o1). Non-reasoning models (gpt-4.1, gpt-4o) reject unknown
        # params с 400 BadRequest. Без этого dispatch `effort: max` в
        # pipeline.yaml silently дропался для GPT-5 — facilitator работал на
        # default reasoning effort вместо ожидаемого high.
        if _is_reasoning_model(self.model):
            kwargs["reasoning_effort"] = _map_effort_to_reasoning(self.effort)
        if output_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": output_schema,
                    "strict": True,
                },
            }

        start = time.monotonic()
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            # OpenAI SDK raises a variety of exceptions (APIError, RateLimitError,
            # AuthenticationError, etc.). Normalize all в RunnerError чтобы engine
            # обработал uniformly с Claude errors. Includes original exception
            # type в message для триаджа.
            raise RunnerError(
                f"OpenAI API call failed ({type(exc).__name__}): {exc}"
            ) from exc
        duration = time.monotonic() - start

        choice = response.choices[0]
        text = choice.message.content or ""
        usage = response.usage
        in_tokens = usage.prompt_tokens if usage else None
        out_tokens = usage.completion_tokens if usage else None
        cost = _estimate_cost(self.model, in_tokens, out_tokens)

        parsed_json: dict | None = None
        if output_schema is not None and text:
            try:
                value = json.loads(text)
                if isinstance(value, dict):
                    parsed_json = value
            except json.JSONDecodeError:
                # Strict mode гарантирует валидный JSON — но если provider
                # вернул что-то странное, оставляем None и caller обработает.
                parsed_json = None

        return RunnerResult(
            text=text,
            cost_usd=cost,
            duration_s=duration,
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            parsed_json=parsed_json,
            tool_events=[],
        )


def _estimate_cost(model: str, in_tok: int | None, out_tok: int | None) -> float | None:
    """Approximates cost из token usage. Unknown model → None."""
    if in_tok is None or out_tok is None:
        return None
    pricing = PRICING_USD_PER_M.get(model)
    if pricing is None:
        return None
    in_price, out_price = pricing
    return (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price


def is_openai_model(model: str) -> bool:
    """Дескрипция dispatcher для RunnerFactory."""
    return model.startswith("gpt-") or model.startswith("o3") or model.startswith("o1")
