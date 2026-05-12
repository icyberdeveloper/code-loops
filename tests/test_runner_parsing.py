"""Tests for the stream-json parser in runner.py (no real subprocess calls)."""

from __future__ import annotations

import json

from code_loops.runner import _parse_stream_json


def test_parses_assistant_text_chunks():
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello "}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "world"}]}},
        {
            "type": "result",
            "usage": {"input_tokens": 100, "output_tokens": 5},
            "total_cost_usd": 0.01,
        },
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    r = _parse_stream_json(stdout, duration=1.5)
    assert r.text == "Hello world"
    assert r.cost_usd == 0.01
    assert r.in_tokens == 100
    assert r.out_tokens == 5
    assert r.duration_s == 1.5


def test_result_string_overrides_streamed_chunks():
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
        {"type": "result", "result": "FINAL ANSWER", "usage": {}, "total_cost_usd": 0.02},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    r = _parse_stream_json(stdout, duration=1.0)
    assert r.text == "FINAL ANSWER"


def test_handles_blank_and_invalid_json_lines():
    stdout = (
        '\n{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\nnot json\n\n'
    )
    r = _parse_stream_json(stdout, duration=0.1)
    assert r.text == "ok"
