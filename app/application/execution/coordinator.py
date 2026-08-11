from __future__ import annotations

from app.application.execution.contracts import (
    ExecutionFailurePolicy,
    ExecutionMode,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from app.application.execution.strategy import ExecutionStrategyResolver


class ExecutionCoordinator:
    """Sequential composite coordinator for no-write execution foundation strategies."""

    def __init__(self, *, strategy_resolver: ExecutionStrategyResolver) -> None:
        self._strategy_resolver = strategy_resolver

    def execute(self, plan: ExecutionPlan) -> ExecutionResult:
        failure_policy = _failure_policy(plan.mode)
        results: list[ExecutionStepResult] = []
        for step in plan.steps:
            strategy = self._strategy_resolver.resolve(step.step_type)
            result = strategy.execute(
                ExecutionStepRequest(
                    execution_id=plan.execution_id,
                    review_id=plan.review_id,
                    company_id=plan.company_id,
                    decision_version=plan.decision_version,
                    mode=plan.mode,
                    step=step,
                    decision_id=plan.decision_id,
                )
            )
            results.append(result)
            if failure_policy is ExecutionFailurePolicy.FAIL_FAST and _is_failure(result):
                break
        return ExecutionResult(
            execution_id=plan.execution_id,
            status=_aggregate_status(plan.mode, tuple(results)),
            step_results=tuple(results),
            warnings=plan.warnings,
        )


def _failure_policy(mode: ExecutionMode) -> ExecutionFailurePolicy:
    if mode is ExecutionMode.DRY_RUN:
        return ExecutionFailurePolicy.COLLECT_ALL
    return ExecutionFailurePolicy.FAIL_FAST


def _is_failure(result: ExecutionStepResult) -> bool:
    return result.status in {ExecutionStepStatus.FAILED, ExecutionStepStatus.UNSUPPORTED}


def _aggregate_status(mode: ExecutionMode, results: tuple[ExecutionStepResult, ...]) -> ExecutionStatus:
    if any(result.status is ExecutionStepStatus.FAILED for result in results):
        return ExecutionStatus.FAILED
    if any(result.status is ExecutionStepStatus.UNSUPPORTED for result in results):
        return ExecutionStatus.FAILED
    if mode is ExecutionMode.DRY_RUN:
        return ExecutionStatus.DRY_RUN_COMPLETED
    if all(result.status is ExecutionStepStatus.EXECUTED for result in results):
        return ExecutionStatus.EXECUTED
    return ExecutionStatus.READY_TO_EXECUTE
