from __future__ import annotations

from collections.abc import Iterable

from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionUnsupportedStepError
from app.application.execution.strategy import ExecutionStrategy


class CustomerRechargeExecutionRouter:
    """Route CUSTOMER_RECHARGE steps to existing-invoice or creation strategies."""

    name = "customer_recharge_router"
    supported_step_types = (ExecutionStepType.CUSTOMER_RECHARGE,)

    def __init__(self, strategies: Iterable[ExecutionStrategy]) -> None:
        self._strategies = tuple(strategies)
        if not self._strategies:
            raise ExecutionUnsupportedStepError("Customer Recharge router requires at least one strategy.")

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return any(strategy.supports_mode(mode) for strategy in self._strategies)

    def supports_step(self, *, step: object, mode: ExecutionMode) -> bool:
        return any(
            callable(getattr(strategy, "supports_step", None)) and strategy.supports_step(step=step, mode=mode)
            for strategy in self._strategies
        )

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        for strategy in self._strategies:
            supports_step = getattr(strategy, "supports_step", None)
            if callable(supports_step) and supports_step(step=request.step, mode=request.mode):
                return strategy.execute(request)
        raise ExecutionUnsupportedStepError("No Customer Recharge strategy supports this execution step.")
