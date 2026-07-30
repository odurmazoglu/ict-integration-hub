from __future__ import annotations

from collections.abc import Iterable

from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.decision.strategy import WorkflowStrategy
from app.application.workflow import WorkflowType


class WorkflowStrategyResolver:
    """Resolve canonical workflow types to executable strategies."""

    def __init__(self, strategies: Iterable[WorkflowStrategy]) -> None:
        self._strategies = {strategy.workflow: strategy for strategy in strategies}

    def resolve(self, workflow: WorkflowType) -> WorkflowStrategy:
        strategy = self._strategies.get(workflow)
        if strategy is None:
            raise UnsupportedWorkflowError(f"Unsupported workflow: {workflow.value}.")
        return strategy
