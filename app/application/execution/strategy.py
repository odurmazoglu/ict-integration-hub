from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionStrategyResolutionError,
    ExecutionUnsupportedStepError,
)


class ExecutionStrategy(Protocol):
    supported_step_types: tuple[ExecutionStepType, ...]
    name: str

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        pass


class ExecutionStrategyResolver:
    """Resolve execution step types to exactly one registered strategy."""

    def __init__(self, strategies: Iterable[ExecutionStrategy]) -> None:
        by_type: dict[ExecutionStepType, ExecutionStrategy] = {}
        for strategy in strategies:
            for step_type in strategy.supported_step_types:
                if step_type in by_type:
                    raise ExecutionStrategyResolutionError("Duplicate execution strategy registration.")
                by_type[step_type] = strategy
        self._strategies = by_type

    def resolve(self, step_type: ExecutionStepType) -> ExecutionStrategy:
        strategy = self._strategies.get(step_type)
        if strategy is None:
            raise ExecutionUnsupportedStepError("Execution step strategy is not supported.")
        return strategy


class FoundationExecutionStrategy:
    """No-write execution foundation strategy for dry-run planning and unsupported execute mode."""

    name = "foundation_no_write"

    def __init__(self, *, supported_step_types: tuple[ExecutionStepType, ...]) -> None:
        self.supported_step_types = supported_step_types

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.mode is ExecutionMode.DRY_RUN:
            return ExecutionStepResult(
                step_key=request.step.step_key,
                step_type=request.step.step_type,
                status=ExecutionStepStatus.DRY_RUN_OK,
                dry_run=True,
                message="Dry run completed. No ERP write was performed.",
            )
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.UNSUPPORTED,
            dry_run=False,
            message="Execution strategy is not enabled in this foundation PR.",
            error_code="EXECUTION_NOT_ENABLED",
        )
