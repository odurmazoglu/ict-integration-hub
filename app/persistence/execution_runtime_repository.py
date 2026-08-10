from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionPlan,
    ExecutionStep,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionIdempotencyConflictError,
    ExecutionPersistenceError,
    ExecutionStateError,
)
from app.application.execution.runtime import (
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionFailure,
    ExecutionHistory,
    ExecutionRetryPolicy,
    ExecutionRetryPolicyType,
    ExecutionRuntimeStep,
    ExecutionRuntimeStepState,
    ExecutionSnapshot,
    ExecutionState,
)
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep

SAFE_EXECUTION_PERSISTENCE_ERROR = "Execution runtime persistence operation failed."


class SqlAlchemyExecutionRuntimeRepository:
    """SQLAlchemy adapter for durable no-write execution runtime state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_from_plan(self, *, plan: ExecutionPlan, retry_policy: ExecutionRetryPolicy) -> ExecutionSnapshot:
        try:
            idempotency_key = _plan_idempotency_key(plan)
            existing = self.get_by_idempotency_key(company_id=plan.company_id, idempotency_key=idempotency_key)
            if existing is not None:
                if _plan_signature(existing.plan) != _plan_signature(plan):
                    raise ExecutionIdempotencyConflictError("Execution idempotency key conflicts with a plan.")
                return existing

            snapshot = _snapshot_from_plan(plan, retry_policy=retry_policy)
            record = _execution_model_from_snapshot(snapshot)
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
            return self.get_snapshot(execution_id=plan.execution_id) or snapshot
        except IntegrityError as exc:
            existing = self.get_by_idempotency_key(
                company_id=plan.company_id,
                idempotency_key=_plan_idempotency_key(plan),
            )
            if existing is not None and _plan_signature(existing.plan) == _plan_signature(plan):
                return existing
            raise ExecutionIdempotencyConflictError("Execution idempotency key conflicts with an execution.") from exc
        except (ExecutionIdempotencyConflictError, ExecutionStateError):
            raise
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def get_snapshot(self, *, execution_id: str) -> ExecutionSnapshot | None:
        try:
            record = self._execution_record(execution_id=execution_id)
            if record is None:
                return None
            return _snapshot_from_model(record)
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def get_by_idempotency_key(self, *, company_id: int, idempotency_key: str) -> ExecutionSnapshot | None:
        try:
            record = self._session.scalar(
                select(WorkflowExecution)
                .options(selectinload(WorkflowExecution.steps))
                .where(
                    WorkflowExecution.company_id == company_id,
                    WorkflowExecution.idempotency_key == idempotency_key,
                )
            )
            if record is None:
                return None
            return _snapshot_from_model(record)
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def save_snapshot(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        try:
            record = self._execution_record(execution_id=snapshot.execution_id)
            if record is None:
                raise ExecutionStateError("Execution runtime was not found.")
            with self._session.begin_nested():
                record.state = snapshot.state.value
                record.mode = snapshot.mode.value
                record.checkpoint = _checkpoint_to_data(snapshot.checkpoint)
                record.retry_policy = _retry_policy_to_data(snapshot.retry_policy)
                record.failure = _failure_to_data(snapshot.failure)
                record.current_step_key = snapshot.checkpoint.current_step_key
                existing_steps = {step.step_key: step for step in record.steps}
                for step in snapshot.steps:
                    model_step = existing_steps[step.step_key]
                    model_step.state = step.state.value
                    model_step.retry_count = step.retry_count
                    model_step.last_result = _step_result_to_data(step.last_result)
                self._session.flush()
            return self.get_snapshot(execution_id=snapshot.execution_id) or snapshot
        except (ExecutionIdempotencyConflictError, ExecutionStateError):
            raise
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        try:
            record = self._execution_record(execution_id=event.execution_id)
            if record is None:
                raise ExecutionStateError("Execution runtime was not found.")
            with self._session.begin_nested():
                self._session.add(_event_model_from_event(event))
                checkpoint = dict(record.checkpoint)
                checkpoint["last_event_id"] = event.event_id
                record.checkpoint = checkpoint
                self._session.flush()
            return event
        except IntegrityError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc
        except ExecutionStateError:
            raise
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def history(self, *, execution_id: str) -> ExecutionHistory:
        try:
            records = tuple(
                self._session.scalars(
                    select(WorkflowExecutionEvent)
                    .where(WorkflowExecutionEvent.execution_id == execution_id)
                    .order_by(WorkflowExecutionEvent.sequence.asc())
                )
            )
            return ExecutionHistory(
                execution_id=execution_id,
                events=tuple(_event_from_model(record) for record in records),
            )
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def save_checkpoint(self, checkpoint: ExecutionCheckpoint) -> ExecutionCheckpoint:
        try:
            record = self._execution_record(execution_id=checkpoint.execution_id)
            if record is None:
                raise ExecutionStateError("Execution runtime was not found.")
            with self._session.begin_nested():
                record.checkpoint = _checkpoint_to_data(checkpoint)
                record.current_step_key = checkpoint.current_step_key
                self._session.flush()
            return checkpoint
        except ExecutionStateError:
            raise
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def get_checkpoint(self, *, execution_id: str) -> ExecutionCheckpoint | None:
        try:
            record = self._execution_record(execution_id=execution_id)
            if record is None:
                return None
            return _checkpoint_from_data(record.checkpoint)
        except SQLAlchemyError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc

    def _execution_record(self, *, execution_id: str) -> WorkflowExecution | None:
        return self._session.scalar(
            select(WorkflowExecution)
            .options(selectinload(WorkflowExecution.steps))
            .where(WorkflowExecution.execution_id == execution_id)
        )


def _snapshot_from_plan(plan: ExecutionPlan, *, retry_policy: ExecutionRetryPolicy) -> ExecutionSnapshot:
    idempotency_key = _plan_idempotency_key(plan)
    runtime_steps = tuple(
        ExecutionRuntimeStep(
            step_key=step.step_key,
            step_type=step.step_type,
            sequence=step.sequence,
            state=ExecutionRuntimeStepState.PENDING,
            allocation_keys=step.allocation_keys,
        )
        for step in plan.steps
    )
    checkpoint = ExecutionCheckpoint(
        execution_id=plan.execution_id,
        completed_step_keys=(),
        failed_step_key=None,
        current_step_key=runtime_steps[0].step_key,
        retry_count=0,
        last_event_id=None,
    )
    return ExecutionSnapshot(
        execution_id=plan.execution_id,
        review_id=plan.review_id,
        company_id=plan.company_id,
        decision_version=plan.decision_version,
        mode=plan.mode,
        state=ExecutionState.PLANNED,
        idempotency_key=idempotency_key,
        plan=plan,
        steps=runtime_steps,
        checkpoint=checkpoint,
        retry_policy=retry_policy,
    )


def _execution_model_from_snapshot(snapshot: ExecutionSnapshot) -> WorkflowExecution:
    record = WorkflowExecution(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        decision_version=snapshot.decision_version,
        company_id=snapshot.company_id,
        state=snapshot.state.value,
        mode=snapshot.mode.value,
        idempotency_key=snapshot.idempotency_key,
        plan_signature=_plan_signature(snapshot.plan),
        plan=_plan_to_data(snapshot.plan),
        checkpoint=_checkpoint_to_data(snapshot.checkpoint),
        retry_policy=_retry_policy_to_data(snapshot.retry_policy),
        failure=_failure_to_data(snapshot.failure),
        current_step_key=snapshot.checkpoint.current_step_key,
    )
    record.steps = [_step_model_from_runtime_step(snapshot.execution_id, step) for step in snapshot.steps]
    return record


def _snapshot_from_model(record: WorkflowExecution) -> ExecutionSnapshot:
    plan = _plan_from_data(record.plan)
    return ExecutionSnapshot(
        execution_id=record.execution_id,
        review_id=record.review_id,
        company_id=record.company_id,
        decision_version=record.decision_version,
        mode=ExecutionMode(record.mode),
        state=ExecutionState(record.state),
        idempotency_key=record.idempotency_key,
        plan=plan,
        steps=tuple(_runtime_step_from_model(step) for step in sorted(record.steps, key=lambda item: item.sequence)),
        checkpoint=_checkpoint_from_data(record.checkpoint),
        retry_policy=_retry_policy_from_data(record.retry_policy),
        failure=_failure_from_data(record.failure),
    )


def _step_model_from_runtime_step(execution_id: str, step: ExecutionRuntimeStep) -> WorkflowExecutionStep:
    return WorkflowExecutionStep(
        execution_id=execution_id,
        step_key=step.step_key,
        step_type=step.step_type.value,
        sequence=step.sequence,
        state=step.state.value,
        allocation_keys=list(step.allocation_keys),
        retry_count=step.retry_count,
        last_result=_step_result_to_data(step.last_result),
    )


def _runtime_step_from_model(record: WorkflowExecutionStep) -> ExecutionRuntimeStep:
    return ExecutionRuntimeStep(
        step_key=record.step_key,
        step_type=ExecutionStepType(record.step_type),
        sequence=record.sequence,
        state=ExecutionRuntimeStepState(record.state),
        allocation_keys=tuple(record.allocation_keys),
        retry_count=record.retry_count,
        last_result=_step_result_from_data(record.last_result),
    )


def _event_model_from_event(event: ExecutionEvent) -> WorkflowExecutionEvent:
    return WorkflowExecutionEvent(
        event_id=event.event_id,
        execution_id=event.execution_id,
        sequence=event.sequence,
        event_type=event.event_type.value,
        state=event.state.value,
        step_key=event.step_key,
        step_type=event.step_type.value if event.step_type is not None else None,
        data=event.data,
    )


def _event_from_model(record: WorkflowExecutionEvent) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=record.event_id,
        execution_id=record.execution_id,
        event_type=ExecutionEventType(record.event_type),
        sequence=record.sequence,
        state=ExecutionState(record.state),
        step_key=record.step_key,
        step_type=ExecutionStepType(record.step_type) if record.step_type is not None else None,
        data=record.data,
    )


def _plan_to_data(plan: ExecutionPlan) -> dict[str, Any]:
    return {
        "execution_id": plan.execution_id,
        "review_id": plan.review_id,
        "company_id": plan.company_id,
        "decision_version": plan.decision_version,
        "mode": plan.mode.value,
        "idempotency_key": plan.idempotency_key,
        "warnings": list(plan.warnings),
        "steps": [
            {
                "step_key": step.step_key,
                "step_type": step.step_type.value,
                "allocation_keys": list(step.allocation_keys),
                "sequence": step.sequence,
                "dry_run_supported": step.dry_run_supported,
                "execute_supported": step.execute_supported,
            }
            for step in plan.steps
        ],
    }


def _plan_from_data(data: dict[str, Any]) -> ExecutionPlan:
    return ExecutionPlan(
        execution_id=str(data["execution_id"]),
        review_id=str(data["review_id"]),
        company_id=int(data["company_id"]),
        decision_version=int(data["decision_version"]),
        mode=ExecutionMode(str(data["mode"])),
        steps=tuple(
            ExecutionStep(
                step_key=str(step["step_key"]),
                step_type=ExecutionStepType(str(step["step_type"])),
                allocation_keys=tuple(str(key) for key in step["allocation_keys"]),
                sequence=int(step["sequence"]),
                dry_run_supported=bool(step["dry_run_supported"]),
                execute_supported=bool(step["execute_supported"]),
            )
            for step in data["steps"]
        ),
        warnings=tuple(str(warning) for warning in data.get("warnings", ())),
        idempotency_key=str(data["idempotency_key"]) if data.get("idempotency_key") is not None else None,
    )


def _checkpoint_to_data(checkpoint: ExecutionCheckpoint) -> dict[str, Any]:
    return {
        "execution_id": checkpoint.execution_id,
        "completed_step_keys": list(checkpoint.completed_step_keys),
        "failed_step_key": checkpoint.failed_step_key,
        "current_step_key": checkpoint.current_step_key,
        "retry_count": checkpoint.retry_count,
        "last_event_id": checkpoint.last_event_id,
    }


def _checkpoint_from_data(data: dict[str, Any]) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        execution_id=str(data["execution_id"]),
        completed_step_keys=tuple(str(step_key) for step_key in data.get("completed_step_keys", ())),
        failed_step_key=str(data["failed_step_key"]) if data.get("failed_step_key") is not None else None,
        current_step_key=str(data["current_step_key"]) if data.get("current_step_key") is not None else None,
        retry_count=int(data.get("retry_count", 0)),
        last_event_id=str(data["last_event_id"]) if data.get("last_event_id") is not None else None,
    )


def _retry_policy_to_data(policy: ExecutionRetryPolicy) -> dict[str, Any]:
    return {
        "policy_type": policy.policy_type.value,
        "max_attempts": policy.max_attempts,
        "delay_seconds": policy.delay_seconds,
        "backoff_multiplier": policy.backoff_multiplier,
    }


def _retry_policy_from_data(data: dict[str, Any]) -> ExecutionRetryPolicy:
    return ExecutionRetryPolicy(
        policy_type=ExecutionRetryPolicyType(str(data["policy_type"])),
        max_attempts=int(data["max_attempts"]),
        delay_seconds=int(data["delay_seconds"]),
        backoff_multiplier=int(data["backoff_multiplier"]),
    )


def _failure_to_data(failure: ExecutionFailure | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    return {
        "step_key": failure.step_key,
        "error_code": failure.error_code,
        "safe_message": failure.safe_message,
    }


def _failure_from_data(data: dict[str, Any] | None) -> ExecutionFailure | None:
    if data is None:
        return None
    return ExecutionFailure(
        step_key=str(data["step_key"]) if data.get("step_key") is not None else None,
        error_code=str(data["error_code"]),
        safe_message=str(data["safe_message"]),
    )


def _step_result_to_data(result: ExecutionStepResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "step_key": result.step_key,
        "step_type": result.step_type.value,
        "status": result.status.value,
        "dry_run": result.dry_run,
        "message": result.message,
        "warnings": list(result.warnings),
        "produced_reference_ids": list(result.produced_reference_ids),
        "error_code": result.error_code,
    }


def _step_result_from_data(data: dict[str, Any] | None) -> ExecutionStepResult | None:
    if data is None:
        return None
    return ExecutionStepResult(
        step_key=str(data["step_key"]),
        step_type=ExecutionStepType(str(data["step_type"])),
        status=ExecutionStepStatus(str(data["status"])),
        dry_run=bool(data["dry_run"]),
        message=str(data["message"]) if data.get("message") is not None else None,
        warnings=tuple(str(warning) for warning in data.get("warnings", ())),
        produced_reference_ids=tuple(str(ref) for ref in data.get("produced_reference_ids", ())),
        error_code=str(data["error_code"]) if data.get("error_code") is not None else None,
    )


def _plan_idempotency_key(plan: ExecutionPlan) -> str:
    if plan.idempotency_key is None:
        raise ExecutionStateError("Execution plan idempotency_key is required for persistence.")
    return plan.idempotency_key


def _plan_signature(plan: ExecutionPlan) -> str:
    identity = {
        "review_id": plan.review_id,
        "company_id": plan.company_id,
        "decision_version": plan.decision_version,
        "mode": plan.mode.value,
        "steps": [
            {
                "step_key": step.step_key,
                "step_type": step.step_type.value,
                "allocation_keys": list(step.allocation_keys),
                "sequence": step.sequence,
            }
            for step in plan.steps
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
