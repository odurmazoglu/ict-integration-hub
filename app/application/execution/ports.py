from __future__ import annotations

from typing import Protocol

from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStepResult,
)


class AcceptedReviewDecisionReader(Protocol):
    """Read one accepted authoritative Hub decision for execution planning."""

    def get_accepted_decision(self, *, review_id: str, company_id: int) -> AcceptedReviewDecision:
        pass


class ExecutionStateRepository(Protocol):
    """Future persistence port for execution state and idempotency."""

    def get_by_execution_id(self, execution_id: str) -> ExecutionPlan | None:
        pass

    def get_by_idempotency_key(self, idempotency_key: str) -> ExecutionPlan | None:
        pass

    def create_planned_execution(self, plan: ExecutionPlan) -> ExecutionPlan:
        pass

    def record_step_result(self, *, execution_id: str, result: ExecutionStepResult) -> ExecutionStepResult:
        pass

    def mark_completed(self, *, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        pass

    def mark_failed(self, *, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        pass
