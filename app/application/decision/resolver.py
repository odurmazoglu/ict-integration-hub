from __future__ import annotations

from collections.abc import Iterable

from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.decision.strategy import WorkflowStrategy


class WorkflowStrategyResolver:
    """Resolve workflow names to executable strategies."""

    def __init__(self, strategies: Iterable[WorkflowStrategy]) -> None:
        self._strategies = {strategy.workflow: strategy for strategy in strategies}

    def resolve(self, workflow: str) -> WorkflowStrategy:
        strategy = self._strategies.get(workflow)
        if strategy is None:
            raise UnsupportedWorkflowError(f"Unsupported workflow: {workflow}.")
        return strategy
