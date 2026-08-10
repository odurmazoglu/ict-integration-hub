from __future__ import annotations

from typing import Protocol

from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionPlan,
    ExecutionResult,
    ExecutionSourceInvoice,
    ExecutionStepResult,
)
from app.application.execution.runtime import (
    ExecutionEventDraft,
    ExecutionHistory,
    ExecutionRetryPolicy,
    ExecutionSnapshot,
)


class AcceptedReviewDecisionReader(Protocol):
    """Read one accepted authoritative Hub decision for execution planning."""

    def get_accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> AcceptedReviewDecision:
        pass


class ExecutionSourceInvoiceReader(Protocol):
    """Read authoritative source invoice and deterministic match evidence for execution."""

    def get_source_invoice(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> ExecutionSourceInvoice:
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

    def history(self, *, execution_id: str) -> ExecutionHistory:
        pass


class RetryPolicyResolver(Protocol):
    """Resolve retry policy for a runtime execution."""

    def resolve(self, plan: ExecutionPlan) -> ExecutionRetryPolicy:
        pass
