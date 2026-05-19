"""Tests for ClaudeRunner.run retry behavior on transient failures.

Mocks subprocess.run so no actual `claude` CLI invocation happens. Verifies:
- rc=0 first try → no retry, returns result
- rc=1 + empty stderr → retried up to MAX_RETRIES, raises on final failure
- rc=1 + auth/quota stderr → no retry, raises immediately
- rc=1 transient then rc=0 → succeeds on retry
- timeout → no retry (re-raises immediately)
"""

from __future__ import annotations

import json
import subprocess

import pytest

from code_loops.runner import MAX_RETRIES, ClaudeRunner, RunnerError, _is_non_retryable


def _ok_stdout() -> str:
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
        {"type": "result", "result": "ok", "usage": {}, "total_cost_usd": 0.01},
    ]
    return "\n".join(json.dumps(e) for e in events)


def _make_completed(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


# ---- _is_non_retryable ----


def test_is_non_retryable_empty_stderr_is_retryable():
    assert _is_non_retryable("") is False


def test_is_non_retryable_auth_failure():
    assert _is_non_retryable("Error: Invalid API key provided") is True


def test_is_non_retryable_rate_limit():
    assert _is_non_retryable("rate limit exceeded for org_xyz") is True


def test_is_non_retryable_quota():
    assert _is_non_retryable("Quota exhausted, please contact support") is True


def test_is_non_retryable_unknown_error_is_retryable():
    """Unknown non-empty stderr defaults to retryable — better to retry once
    than to give up on a recoverable transient. Operator sees stderr in error."""
    assert _is_non_retryable("some weird internal error nobody recognized") is False


# ---- ClaudeRunner.run retry flow ----


def test_run_succeeds_on_first_attempt(monkeypatch):
    runner = ClaudeRunner()
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(1)
        return _make_completed(rc=0, stdout=_ok_stdout())

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)  # no-op sleeps
    result = runner.run("sys", "user")
    assert result.text == "ok"
    assert len(calls) == 1


def test_run_retries_on_empty_stderr_then_succeeds(monkeypatch):
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return _make_completed(rc=1, stdout="", stderr="")  # transient
        return _make_completed(rc=0, stdout=_ok_stdout())

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    result = runner.run("sys", "user")
    assert result.text == "ok"
    assert len(attempts) == 2


def test_run_exhausts_retries_and_raises(monkeypatch):
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        return _make_completed(rc=1, stdout="", stderr="")  # always transient-looking

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    with pytest.raises(RunnerError, match="rc=1"):
        runner.run("sys", "user")
    assert len(attempts) == MAX_RETRIES


def test_run_does_not_retry_auth_failure(monkeypatch):
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        return _make_completed(rc=1, stdout="", stderr="Error: Invalid API key")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    with pytest.raises(RunnerError, match="Invalid API key"):
        runner.run("sys", "user")
    assert len(attempts) == 1  # immediate fail, no retry


def test_run_does_not_retry_rate_limit(monkeypatch):
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        return _make_completed(rc=1, stdout="", stderr="429 rate limit hit")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    with pytest.raises(RunnerError, match="rate limit"):
        runner.run("sys", "user")
    assert len(attempts) == 1


def test_run_does_not_retry_timeout(monkeypatch):
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1200)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    with pytest.raises(RunnerError, match="timed out"):
        runner.run("sys", "user")
    assert len(attempts) == 1


def test_run_retries_unknown_stderr(monkeypatch):
    """Conservative: unrecognized stderr is treated as retryable, in case it's a
    transient internal error not yet pattern-matched."""
    runner = ClaudeRunner()
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 2:
            return _make_completed(rc=1, stdout="", stderr="weird internal blip")
        return _make_completed(rc=0, stdout=_ok_stdout())

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: None)
    result = runner.run("sys", "user")
    assert result.text == "ok"
    assert len(attempts) == 2


def test_run_backoff_delays_called(monkeypatch):
    """Verify exponential backoff: sleep called with 5s then 15s."""
    from code_loops.runner import RETRY_BACKOFF_S

    runner = ClaudeRunner()
    sleeps: list[float] = []

    def fake_run(*args, **kwargs):
        return _make_completed(rc=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("code_loops.runner.time.sleep", lambda s: sleeps.append(s))
    with pytest.raises(RunnerError):
        runner.run("sys", "user")
    # After attempt 1 fails: sleep(5). After attempt 2 fails: sleep(15). Attempt 3 fails, no more sleep.
    assert sleeps == list(RETRY_BACKOFF_S)
