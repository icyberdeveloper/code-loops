"""Тесты для OpenAIRunner — mocked SDK calls, без реального API hit.

Real cross-family per MACI (arxiv 2510.04488): different LLM family judge
breaks self-confirming bias loops от same-family critic+facilitator.
ClaudeRunner — critics; OpenAIRunner — facilitator/judge только.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from code_loops.openai_runner import (
    PRICING_USD_PER_M,
    OpenAIRunner,
    _estimate_cost,
    _is_reasoning_model,
    _map_effort_to_reasoning,
    is_openai_model,
)
from code_loops.runner import RunnerError, RunnerFactory

# ---- is_openai_model dispatch ----


def test_dispatch_gpt_family():
    assert is_openai_model("gpt-5")
    assert is_openai_model("gpt-5-mini")
    assert is_openai_model("gpt-4.1")


def test_dispatch_o_series():
    assert is_openai_model("o3")
    assert is_openai_model("o3-mini")
    assert is_openai_model("o1")


def test_dispatch_rejects_claude():
    assert not is_openai_model("claude-opus-4-7")
    assert not is_openai_model("claude-sonnet-4-6")


# ---- _estimate_cost ----


def test_estimate_cost_known_model():
    # gpt-5: $10/M input, $30/M output. 1000 in + 500 out = 0.01 + 0.015 = 0.025
    cost = _estimate_cost("gpt-5", 1000, 500)
    assert cost == pytest.approx(0.025)


def test_estimate_cost_unknown_model_returns_none():
    """Future-proofing: unknown model → None (caller treats as $0 в manifest)."""
    assert _estimate_cost("gpt-99-future", 1000, 500) is None


def test_estimate_cost_missing_tokens_returns_none():
    assert _estimate_cost("gpt-5", None, 500) is None
    assert _estimate_cost("gpt-5", 1000, None) is None


def test_pricing_table_has_no_negative_values():
    """Defensive: ensure pricing constants are sane."""
    for model, (in_price, out_price) in PRICING_USD_PER_M.items():
        assert in_price >= 0, f"{model} input price negative"
        assert out_price >= in_price, f"{model} output price < input (unusual)"


# ---- OpenAIRunner init ----


def test_init_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RunnerError, match="OPENAI_API_KEY"):
        OpenAIRunner(model="gpt-5")


# ---- OpenAIRunner.run with mocked SDK ----


def _make_mock_response(text: str, in_tok: int, out_tok: int):
    """Builds mock structure compatible с openai SDK response shape."""
    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    usage = MagicMock()
    usage.prompt_tokens = in_tok
    usage.completion_tokens = out_tok
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _runner_with_mocked_sdk(monkeypatch, response):
    """Helper: создаёт OpenAIRunner с mocked client."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    mock_openai_module = MagicMock()
    mock_client_instance = MagicMock()
    mock_client_instance.chat.completions.create = MagicMock(return_value=response)
    mock_openai_module.OpenAI = MagicMock(return_value=mock_client_instance)
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)
    return OpenAIRunner(model="gpt-5"), mock_client_instance


def test_run_returns_result_with_cost(monkeypatch):
    response = _make_mock_response("approved verdict", in_tok=100, out_tok=50)
    runner, _ = _runner_with_mocked_sdk(monkeypatch, response)
    result = runner.run("system", "user msg")
    assert result.text == "approved verdict"
    # gpt-5: 100×$10/M + 50×$30/M = 0.001 + 0.0015 = 0.0025
    assert result.cost_usd == pytest.approx(0.0025)
    assert result.in_tokens == 100
    assert result.out_tokens == 50


def test_run_passes_system_and_user_messages(monkeypatch):
    response = _make_mock_response("ok", in_tok=10, out_tok=5)
    runner, client = _runner_with_mocked_sdk(monkeypatch, response)
    runner.run("SYS_PROMPT_HERE", "USER_MSG_HERE")
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs["model"] == "gpt-5"
    msgs = call_kwargs["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS_PROMPT_HERE"}
    assert msgs[1] == {"role": "user", "content": "USER_MSG_HERE"}


# ---- Gap A: effort → reasoning_effort mapping ----


def test_is_reasoning_model_gpt5_o_series():
    """Reasoning models — те которые принимают `reasoning_effort` API param."""
    assert _is_reasoning_model("gpt-5")
    assert _is_reasoning_model("gpt-5-mini")
    assert _is_reasoning_model("o3")
    assert _is_reasoning_model("o1")


def test_is_reasoning_model_excludes_non_reasoning():
    """GPT-4.1, gpt-4o — non-reasoning models. API rejects reasoning_effort
    с 400 BadRequest если передан."""
    assert not _is_reasoning_model("gpt-4.1")
    assert not _is_reasoning_model("gpt-4o")
    assert not _is_reasoning_model("gpt-4.1-mini")


def test_effort_max_maps_to_high():
    """pipeline.yaml defaults effort:max — OpenAI enum: minimal/low/medium/high."""
    assert _map_effort_to_reasoning("max") == "high"
    assert _map_effort_to_reasoning("MAX") == "high"  # case-insensitive


def test_effort_maps_preserve_known_values():
    assert _map_effort_to_reasoning("low") == "low"
    assert _map_effort_to_reasoning("medium") == "medium"
    assert _map_effort_to_reasoning("high") == "high"
    assert _map_effort_to_reasoning("minimal") == "minimal"


def test_effort_unknown_defaults_to_medium():
    """Unknown effort value не должен сломать API call — silent default."""
    assert _map_effort_to_reasoning("garbage") == "medium"
    assert _map_effort_to_reasoning("") == "medium"


def test_run_passes_reasoning_effort_for_gpt5(monkeypatch):
    """GPT-5 (reasoning model) с effort=max → reasoning_effort=high в API call."""
    response = _make_mock_response("ok", in_tok=10, out_tok=5)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    mock_openai_module = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=response)
    mock_openai_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    runner = OpenAIRunner(model="gpt-5", effort="max")
    runner.run("sys", "user")
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("reasoning_effort") == "high"


def test_run_omits_reasoning_effort_for_non_reasoning_model(monkeypatch):
    """gpt-4.1 — non-reasoning. reasoning_effort НЕ должен передаваться
    (иначе API возвращает 400 BadRequest unknown param)."""
    response = _make_mock_response("ok", in_tok=10, out_tok=5)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    mock_openai_module = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=response)
    mock_openai_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    runner = OpenAIRunner(model="gpt-4.1", effort="max")
    runner.run("sys", "user")
    call_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in call_kwargs


# ---- Gap B: unknown model emits warning ----


def test_init_warns_on_unknown_model(monkeypatch, capsys):
    """Unknown model → one-time stderr warning о missing pricing."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    # Очищаем cache warned-models между тестами
    from code_loops.openai_runner import _PRICING_WARNED

    _PRICING_WARNED.clear()
    mock_openai_module = MagicMock()
    mock_openai_module.OpenAI = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    OpenAIRunner(model="gpt-99-future")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "gpt-99-future" in captured.err
    assert "pricing table" in captured.err

    # Second instantiation same model — НЕ должно warn повторно
    OpenAIRunner(model="gpt-99-future")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_init_no_warning_for_known_model(monkeypatch, capsys):
    """Known model (gpt-5 в pricing table) — без warning."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    from code_loops.openai_runner import _PRICING_WARNED

    _PRICING_WARNED.clear()
    mock_openai_module = MagicMock()
    mock_openai_module.OpenAI = MagicMock(return_value=MagicMock())
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    OpenAIRunner(model="gpt-5")
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err


def test_run_with_output_schema_adds_response_format(monkeypatch):
    response = _make_mock_response('{"x": 1}', in_tok=10, out_tok=5)
    runner, client = _runner_with_mocked_sdk(monkeypatch, response)
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    result = runner.run("sys", "user", output_schema=schema)
    call_kwargs = client.chat.completions.create.call_args.kwargs
    rf = call_kwargs["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] == schema
    assert rf["json_schema"]["strict"] is True
    # parsed_json должен быть populated из text
    assert result.parsed_json == {"x": 1}


def test_run_rejects_allowed_tools(monkeypatch):
    response = _make_mock_response("won't reach", in_tok=0, out_tok=0)
    runner, _ = _runner_with_mocked_sdk(monkeypatch, response)
    with pytest.raises(RunnerError, match="does not support allowed_tools"):
        runner.run("sys", "user", allowed_tools=["Bash"])


def test_run_normalizes_sdk_exception(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    mock_openai_module = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(side_effect=RuntimeError("kaboom"))
    mock_openai_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    runner = OpenAIRunner(model="gpt-5")
    with pytest.raises(RunnerError, match="OpenAI API call failed.*kaboom"):
        runner.run("sys", "user")


def test_run_unknown_model_cost_none(monkeypatch):
    response = _make_mock_response("ok", in_tok=100, out_tok=50)
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    mock_openai_module = MagicMock()
    mock_client = MagicMock()
    mock_client.chat.completions.create = MagicMock(return_value=response)
    mock_openai_module.OpenAI = MagicMock(return_value=mock_client)
    monkeypatch.setitem(sys.modules, "openai", mock_openai_module)

    runner = OpenAIRunner(model="gpt-99-future")
    result = runner.run("sys", "user")
    assert result.cost_usd is None  # Unknown model → no cost estimation


# ---- RunnerFactory dispatch ----


def test_factory_dispatches_gpt_model_to_openai_runner(monkeypatch):
    """End-to-end dispatch: yaml-config с model: gpt-5 → OpenAIRunner."""
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    factory = RunnerFactory()
    runner = factory.make({"model": "gpt-5"})
    assert isinstance(runner, OpenAIRunner)
    assert runner.model == "gpt-5"


def test_factory_dispatches_claude_to_claude_runner():
    """Default + explicit claude-* model → ClaudeRunner (no env required)."""
    from code_loops.runner import ClaudeRunner

    factory = RunnerFactory()
    default_runner = factory.make()
    assert isinstance(default_runner, ClaudeRunner)
    explicit = factory.make({"model": "claude-sonnet-4-6"})
    assert isinstance(explicit, ClaudeRunner)
