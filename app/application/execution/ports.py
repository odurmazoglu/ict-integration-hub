from __future__ import annotations

from typing import Protocol

from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStepResult,
)
from app.application.execution.runtime import (
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionEventDraft,
    ExecutionHistory,
    ExecutionRetryPolicy,
    ExecutionSnapshot,
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


class ExecutionRuntimeRepository(Protocol):
    """Durable execution runtime snapshot repository."""

    def create_from_plan(self, *, plan: ExecutionPlan, retry_policy: ExecutionRetryPolicy) -> ExecutionSnapshot:
        pass

    def get_snapshot(self, *, execution_id: str) -> ExecutionSnapshot | None:
        pass

    def get_by_idempotency_key(self, *, company_id: int, idempotency_key: str) -> ExecutionSnapshot | None:
        pass

    def save_snapshot(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        pass

    def persist_transition(
        self,
        *,
        snapshot: ExecutionSnapshot,
        events: tuple[ExecutionEventDraft, ...],
        expected_runtime_version: int,
    ) -> ExecutionSnapshot:
        pass


class ExecutionEventRepository(Protocol):
    """Append-only execution event repository."""

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        pass

    def history(self, *, execution_id: str) -> ExecutionHistory:
        pass


class ExecutionCheckpointRepository(Protocol):
    """Execution checkpoint repository for crash recovery."""

    def save_checkpoint(self, checkpoint: ExecutionCheckpoint) -> ExecutionCheckpoint:
        pass

    def get_checkpoint(self, *, execution_id: str) -> ExecutionCheckpoint | None:
        pass


class RetryPolicyResolver(Protocol):
    """Resolve retry policy for a runtime execution."""

    def resolve(self, plan: ExecutionPlan) -> ExecutionRetryPolicy:
        pass
