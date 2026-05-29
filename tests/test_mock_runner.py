"""Tests для MockClaudeRunner — fast iteration primitive для pipeline tests без
real subprocess."""

from __future__ import annotations

from code_loops.runner import RunnerResult
from tests.conftest import MockClaudeRunner


def test_mock_runner_returns_default_when_no_queue():
    runner = MockClaudeRunner()
    result = runner.run("You are the **Editor**.", "do thing")
    assert isinstance(result, RunnerResult)
    assert "mock: no response" in result.text


def test_mock_runner_pops_per_role_queue():
    runner = MockClaudeRunner(
        responses={"editor": ["first response", "second response"]}
    )
    r1 = runner.run("You are the **Editor**.", "msg1")
    r2 = runner.run("You are the **Editor**.", "msg2")
    assert r1.text == "first response"
    assert r2.text == "second response"


def test_mock_runner_role_detection_from_bold():
    runner = MockClaudeRunner(responses={"triage_engineer": ["routed"]})
    result = runner.run("You are the **Triage Engineer** for the loop.", "x")
    assert result.text == "routed"


def test_mock_runner_role_detection_from_heading():
    runner = MockClaudeRunner(responses={"editor": ["edited"]})
    result = runner.run("# Editor\n\nYou execute a subtask.", "x")
    assert result.text == "edited"


def test_mock_runner_falls_back_к_default_when_role_unknown():
    runner = MockClaudeRunner(default_responses=["fallback"])
    result = runner.run("# UnknownRole\n\nblah", "x")
    assert result.text == "fallback"


def test_mock_runner_accepts_runnerresult_objects():
    custom = RunnerResult(text="precise", cost_usd=0.5, duration_s=2.3)
    runner = MockClaudeRunner(responses={"editor": [custom]})
    result = runner.run("# Editor", "x")
    assert result.text == "precise"
    assert result.cost_usd == 0.5
    assert result.duration_s == 2.3


def test_mock_runner_records_calls():
    runner = MockClaudeRunner(responses={"editor": ["ok"]})
    runner.run("# Editor", "first msg", cwd="/tmp/wt")
    runner.run("# Editor", "second msg", cwd="/tmp/wt2")
    assert len(runner.calls) == 2
    assert runner.calls[0]["user_message"] == "first msg"
    assert runner.calls[0]["cwd"] == "/tmp/wt"
    assert runner.calls[1]["user_message"] == "second msg"


def test_mock_runner_default_cost_applied():
    runner = MockClaudeRunner(responses={"editor": ["x"]}, default_cost=0.42)
    result = runner.run("# Editor", "msg")
    assert result.cost_usd == 0.42
