from __future__ import annotations

from app.application.execution.contracts import ExecutionPlan, ExecutionResult, ExecutionStepResult
from app.application.execution.exceptions import ExecutionIdempotencyConflictError, ExecutionStateError
from app.application.execution.ports import ExecutionStateRepository


class InMemoryExecutionStateRepository(ExecutionStateRepository):
    """In-memory execution state repository for tests and future wiring examples."""

    def __init__(self) -> None:
        self._plans_by_execution_id: dict[str, ExecutionPlan] = {}
        self._execution_id_by_idempotency_key: dict[str, str] = {}
        self._step_results: dict[str, tuple[ExecutionStepResult, ...]] = {}
        self._completed_results: dict[str, ExecutionResult] = {}
        self._failed_results: dict[str, ExecutionResult] = {}

    def get_by_execution_id(self, execution_id: str) -> ExecutionPlan | None:
        return self._plans_by_execution_id.get(execution_id)

    def get_by_idempotency_key(self, idempotency_key: str) -> ExecutionPlan | None:
        execution_id = self._execution_id_by_idempotency_key.get(idempotency_key)
        if execution_id is None:
            return None
        return self._plans_by_execution_id.get(execution_id)

    def create_planned_execution(self, plan: ExecutionPlan) -> ExecutionPlan:
        if plan.idempotency_key is None:
            raise ExecutionStateError("Execution plan idempotency key is required.")
        existing_execution_id = self._execution_id_by_idempotency_key.get(plan.idempotency_key)
        if existing_execution_id is not None:
            existing = self._plans_by_execution_id[existing_execution_id]
            if existing != plan:
                raise ExecutionIdempotencyConflictError("Execution idempotency key conflicts with existing plan.")
            return existing
        if plan.execution_id in self._plans_by_execution_id:
            raise ExecutionStateError("Execution id already exists.")
        self._plans_by_execution_id[plan.execution_id] = plan
        self._execution_id_by_idempotency_key[plan.idempotency_key] = plan.execution_id
        self._step_results[plan.execution_id] = ()
        return plan

    def record_step_result(self, *, execution_id: str, result: ExecutionStepResult) -> ExecutionStepResult:
        self._require_active_execution(execution_id)
        self._step_results[execution_id] = (*self._step_results[execution_id], result)
        return result

    def mark_completed(self, *, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        self._require_active_execution(execution_id)
        if execution_id in self._failed_results:
            raise ExecutionStateError("Failed execution cannot be marked completed.")
        self._completed_results[execution_id] = result
        return result

    def mark_failed(self, *, execution_id: str, result: ExecutionResult) -> ExecutionResult:
        self._require_active_execution(execution_id)
        if execution_id in self._completed_results:
            raise ExecutionStateError("Completed execution cannot be marked failed.")
        self._failed_results[execution_id] = result
        return result

    def _require_active_execution(self, execution_id: str) -> None:
        if execution_id not in self._plans_by_execution_id:
            raise ExecutionStateError("Execution plan was not found.")
