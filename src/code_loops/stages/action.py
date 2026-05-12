"""Generic action stage handler — runs a Python function (no LLM).

Pipeline.yaml stage:
    - name: final_validation
      type: action
      handler: final_validation
      ...

The handler name is dispatched via the `_HANDLERS` table below. To add a
new action: implement `(stage_def, ctx) -> dict` in a sibling module,
import it here, and add an entry. No registry indirection — at this
scale (1-2 actions) the table is the registry.
"""

from __future__ import annotations

from rich.console import Console

from ..runner import RunnerFactory
from .final_validation import run_final_validation
from .prompt import StageContext

console = Console()

_HANDLERS = {
    "final_validation": run_final_validation,
}


class ActionError(RuntimeError):
    pass


class ActionStage:
    def __init__(self, factory: RunnerFactory):
        # Some actions may want to call LLMs internally (e.g., smoke-test
        # generators); they get the factory via the StageContext-passed runner.
        # Currently unused but kept for symmetry with other stage handlers.
        self.factory = factory

    def run(self, stage_def: dict, ctx: StageContext) -> dict:
        handler_name = stage_def.get("handler")
        if not handler_name:
            raise ActionError(f"action stage `{stage_def.get('name')}` missing `handler` key")
        fn = _HANDLERS.get(handler_name)
        if fn is None:
            raise ActionError(
                f"unknown action handler: {handler_name!r}. Registered: {sorted(_HANDLERS)}"
            )
        console.print(f"  [dim]action:[/dim] running {handler_name}")
        return fn(stage_def, ctx)
