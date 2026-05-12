"""Shared test fixtures."""

from __future__ import annotations

from code_loops.runner import RunnerResult


class FakeFactory:
    """RunnerFactory stand-in: always returns the wrapped runner, ignoring overrides."""

    def __init__(self, runner):
        self.runner = runner

    def make(self, spec: dict | None = None):
        return self.runner


__all__ = ["FakeFactory", "RunnerResult"]
