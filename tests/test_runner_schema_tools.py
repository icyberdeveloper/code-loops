"""Тесты для runner.py: output_schema, allowed_tools, tool_events parsing.

Не вызывают реальный claude CLI — мокают subprocess.run чтобы убедиться:
- Корректно передаются --json-schema и --allowed-tools в CLI command
- _parse_stream_json собирает tool_use/tool_result события
- expect_json парсит финальный текст в parsed_json
"""

from __future__ import annotations

import json
import subprocess

from code_loops.runner import ClaudeRunner, _parse_stream_json


def _ok_stdout_with_text(text: str) -> str:
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "result": text, "usage": {}, "total_cost_usd": 0.01},
    ]
    return "\n".join(json.dumps(e) for e in events)


def _make_completed(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---- CLI command construction ----


def test_run_without_kwargs_does_not_add_schema_or_tools_flags(monkeypatch):
    """Backward compat: вызов без output_schema/allowed_tools не должен
    добавлять флаги в command."""
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return _make_completed(rc=0, stdout=_ok_stdout_with_text("ok"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)

    ClaudeRunner().run("sys", "user")
    cmd = captured_cmds[0]
    assert "--json-schema" not in cmd
    assert "--allowed-tools" not in cmd


def test_run_with_output_schema_adds_json_schema_flag(monkeypatch):
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return _make_completed(rc=0, stdout=_ok_stdout_with_text('{"x": 1}'))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)

    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    ClaudeRunner().run("sys", "user", output_schema=schema)
    cmd = captured_cmds[0]
    assert "--json-schema" in cmd
    schema_idx = cmd.index("--json-schema")
    assert json.loads(cmd[schema_idx + 1]) == schema


def test_run_with_allowed_tools_adds_flag_with_comma_separated_list(monkeypatch):
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return _make_completed(rc=0, stdout=_ok_stdout_with_text("ok"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)

    ClaudeRunner().run("sys", "user", allowed_tools=["Bash", "Read", "Grep"])
    cmd = captured_cmds[0]
    assert "--allowed-tools" in cmd
    tools_idx = cmd.index("--allowed-tools")
    assert cmd[tools_idx + 1] == "Bash,Read,Grep"


# ---- expect_json parsing ----


def test_parse_expect_json_takes_structured_output_from_result():
    """Реальное поведение CLI: --json-schema даёт structured_output поле
    в result event. parsed_json должен взять оттуда, не парсить text."""
    payload = {"verified_files": ["a.py", "b.py"]}
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_x",
                        "name": "StructuredOutput",
                        "input": payload,
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_x",
                        "content": "Structured output provided successfully",
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Evidence document submitted."}]},
        },
        {
            "type": "result",
            "result": "Evidence document submitted.",
            "structured_output": payload,
            "usage": {},
            "total_cost_usd": 0.05,
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0, expect_json=True)
    assert result.parsed_json == payload
    # text — это assistant prose response, не JSON
    assert result.text == "Evidence document submitted."


def test_parse_expect_json_fallback_to_structured_output_tool_use():
    """Если result.structured_output отсутствует но StructuredOutput tool
    был вызван — берём из tool_use.input как fallback."""
    payload = {"x": 1}
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "StructuredOutput",
                        "input": payload,
                    }
                ]
            },
        },
        {
            "type": "result",
            "result": "done",
            "usage": {},
            "total_cost_usd": 0.01,
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0, expect_json=True)
    assert result.parsed_json == payload


def test_structured_output_tool_use_not_in_tool_events():
    """StructuredOutput это путь --json-schema, не обычный tool. Не должен
    появляться в tool_events (там только real Bash/Read/Grep calls для
    аудита critic'ом)."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "tool_use", "id": "t2", "name": "StructuredOutput", "input": {"x": 1}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "file.py"}]
            },
        },
        {
            "type": "result",
            "result": "done",
            "structured_output": {"x": 1},
            "usage": {},
            "total_cost_usd": 0.01,
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0, expect_json=True)
    # Только Bash в tool_events, StructuredOutput — отдельно через parsed_json
    assert len(result.tool_events) == 1
    assert result.tool_events[0]["name"] == "Bash"
    assert result.parsed_json == {"x": 1}


def test_parse_without_expect_json_leaves_parsed_json_none():
    stdout = _ok_stdout_with_text('{"x": 1}')
    result = _parse_stream_json(stdout, duration=1.0, expect_json=False)
    assert result.parsed_json is None
    assert result.text == '{"x": 1}'


def test_parse_expect_json_no_structured_output_returns_none():
    """Когда --json-schema задан но CLI почему-то не вернул StructuredOutput
    (refusal, CLI bug) — parsed_json остаётся None, caller увидит и решит."""
    stdout = _ok_stdout_with_text("just plain text no schema")
    result = _parse_stream_json(stdout, duration=1.0, expect_json=True)
    assert result.parsed_json is None
    assert result.text == "just plain text no schema"


# ---- tool_events parsing ----


def test_parse_collects_tool_use_and_pairs_with_tool_result():
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"command": "grep -n foo bar.py"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": [{"type": "text", "text": "42: def foo(): ..."}],
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "found it"}]},
        },
        {"type": "result", "result": "found it", "usage": {}, "total_cost_usd": 0.01},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0)
    assert len(result.tool_events) == 1
    ev = result.tool_events[0]
    assert ev["name"] == "Bash"
    assert ev["input"] == {"command": "grep -n foo bar.py"}
    assert ev["output"] == "42: def foo(): ..."


def test_parse_preserves_tool_call_order():
    """Два tool calls с разными id — порядок в result.tool_events должен
    соответствовать порядку в потоке."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "first", "name": "Grep", "input": {"q": "1"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "first", "content": "1 result"}]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "second", "name": "Read", "input": {"q": "2"}},
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "second", "content": "2 result"}]
            },
        },
        {"type": "result", "result": "done", "usage": {}, "total_cost_usd": 0.01},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0)
    assert [ev["name"] for ev in result.tool_events] == ["Grep", "Read"]
    assert [ev["output"] for ev in result.tool_events] == ["1 result", "2 result"]


def test_parse_tool_result_can_be_string_not_list():
    """tool_result.content может быть либо строкой либо списком text-блоков."""
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]},
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "x", "content": "plain string"}]
            },
        },
        {"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0.0},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0)
    assert result.tool_events[0]["output"] == "plain string"


def test_parse_orphan_tool_use_without_result_kept_with_none_output():
    """Если tool_result не пришёл (rare), tool_use всё равно в transcript
    но с output=None — это сигнал для критика 'архитектор начал tool call
    но что-то прервалось'."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "lonely", "name": "Bash", "input": {"cmd": "x"}}
                ]
            },
        },
        {"type": "result", "result": "done", "usage": {}, "total_cost_usd": 0.0},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    result = _parse_stream_json(stdout, duration=1.0)
    assert len(result.tool_events) == 1
    assert result.tool_events[0]["output"] is None
