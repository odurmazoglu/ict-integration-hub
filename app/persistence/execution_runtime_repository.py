from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from app.application.execution.contracts import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionPlan,
    ExecutionStep,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionConcurrencyConflictError,
    ExecutionIdempotencyConflictError,
    ExecutionPersistenceError,
    ExecutionStateError,
)
from app.application.execution.runtime import (
    ExecutionCheckpoint,
    ExecutionEvent,
    ExecutionEventDraft,
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
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationType
from app.billing.dto import CustomerInvoiceBillingInstruction, CustomerInvoiceBillingLine
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
            created_event = _event_draft(
                execution_id=snapshot.execution_id,
                event_type=ExecutionEventType.EXECUTION_CREATED,
                state=ExecutionState.NEW,
            )
            planned_event = _event_draft(
                execution_id=snapshot.execution_id,
                event_type=ExecutionEventType.PLANNING_COMPLETED,
                state=ExecutionState.PLANNED,
            )
            with self._session.begin_nested():
                self._session.add(record)
                events = _assign_event_sequences(
                    execution_id=snapshot.execution_id,
                    first_sequence=record.next_event_sequence,
                    drafts=(created_event, planned_event),
                )
                record.next_event_sequence += len(events)
                record.checkpoint = _checkpoint_to_data(
                    _checkpoint_with_last_event_id(snapshot.checkpoint, events[-1].event_id)
                )
                self._session.add_all(self._event_model_from_event(event) for event in events)
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

    def persist_transition(
        self,
        *,
        snapshot: ExecutionSnapshot,
        events: tuple[ExecutionEventDraft, ...],
        expected_runtime_version: int,
    ) -> ExecutionSnapshot:
        if not events:
            raise ExecutionStateError("Execution transition requires at least one event.")
        try:
            record = self._execution_record(execution_id=snapshot.execution_id)
            if record is None:
                raise ExecutionStateError("Execution runtime was not found.")
            if record.runtime_version != expected_runtime_version:
                raise ExecutionConcurrencyConflictError("Execution runtime snapshot is stale.")
            assigned_events = _assign_event_sequences(
                execution_id=snapshot.execution_id,
                first_sequence=record.next_event_sequence,
                drafts=events,
            )
            final_event_id = assigned_events[-1].event_id
            checkpoint = _checkpoint_with_last_event_id(snapshot.checkpoint, final_event_id)
            with self._session.begin_nested():
                result = self._session.execute(
                    update(WorkflowExecution)
                    .where(
                        WorkflowExecution.execution_id == snapshot.execution_id,
                        WorkflowExecution.runtime_version == expected_runtime_version,
                    )
                    .values(
                        state=snapshot.state.value,
                        mode=snapshot.mode.value,
                        checkpoint=_checkpoint_to_data(checkpoint),
                        retry_policy=_retry_policy_to_data(snapshot.retry_policy),
                        failure=_failure_to_data(snapshot.failure),
                        current_step_key=checkpoint.current_step_key,
                        runtime_version=expected_runtime_version + 1,
                        next_event_sequence=record.next_event_sequence + len(assigned_events),
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(result.rowcount or 0) != 1:
                    raise ExecutionConcurrencyConflictError("Execution runtime snapshot is stale.")
                existing_steps = {step.step_key: step for step in record.steps}
                for step in snapshot.steps:
                    model_step = existing_steps[step.step_key]
                    model_step.state = step.state.value
                    model_step.retry_count = step.retry_count
                    model_step.last_result = _step_result_to_data(step.last_result)
                self._session.add_all(self._event_model_from_event(event) for event in assigned_events)
                self._session.flush()
            return self.get_snapshot(execution_id=snapshot.execution_id) or snapshot
        except (ExecutionConcurrencyConflictError, ExecutionStateError):
            raise
        except IntegrityError as exc:
            raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR) from exc
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
            .execution_options(populate_existing=True)
        )

    def _event_model_from_event(self, event: ExecutionEvent) -> WorkflowExecutionEvent:
        return _event_model_from_event(event)


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
        runtime_version=1,
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
        runtime_version=snapshot.runtime_version,
        next_event_sequence=1,
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
        runtime_version=record.runtime_version,
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


def _assign_event_sequences(
    *,
    execution_id: str,
    first_sequence: int,
    drafts: tuple[ExecutionEventDraft, ...],
) -> tuple[ExecutionEvent, ...]:
    return tuple(
        ExecutionEvent(
            event_id=draft.event_id,
            execution_id=execution_id,
            event_type=draft.event_type,
            sequence=first_sequence + index,
            state=draft.state,
            step_key=draft.step_key,
            step_type=draft.step_type,
            data=draft.data,
        )
        for index, draft in enumerate(drafts)
    )


def _event_draft(
    *,
    execution_id: str,
    event_type: ExecutionEventType,
    state: ExecutionState,
) -> ExecutionEventDraft:
    return ExecutionEventDraft(
        event_id=f"execution-event:{uuid4()}",
        execution_id=execution_id,
        event_type=event_type,
        state=state,
    )


def _checkpoint_with_last_event_id(
    checkpoint: ExecutionCheckpoint,
    last_event_id: str,
) -> ExecutionCheckpoint:
    return ExecutionCheckpoint(
        execution_id=checkpoint.execution_id,
        completed_step_keys=checkpoint.completed_step_keys,
        failed_step_key=checkpoint.failed_step_key,
        current_step_key=checkpoint.current_step_key,
        retry_count=checkpoint.retry_count,
        last_event_id=last_event_id,
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
        "decision_id": plan.decision_id,
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
                "writer_required": step.writer_required,
                "allocations": [_allocation_to_data(allocation) for allocation in step.allocations],
                "customer_invoice_billing_instruction": _billing_instruction_to_data(
                    step.customer_invoice_billing_instruction
                ),
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
        decision_id=str(data["decision_id"]) if data.get("decision_id") is not None else None,
        steps=tuple(
            ExecutionStep(
                step_key=str(step["step_key"]),
                step_type=ExecutionStepType(str(step["step_type"])),
                allocation_keys=tuple(str(key) for key in step["allocation_keys"]),
                sequence=int(step["sequence"]),
                dry_run_supported=bool(step["dry_run_supported"]),
                execute_supported=bool(step["execute_supported"]),
                writer_required=bool(step.get("writer_required", False)),
                allocations=tuple(_allocation_from_data(allocation) for allocation in step.get("allocations", ())),
                customer_invoice_billing_instruction=_billing_instruction_from_data(
                    step.get("customer_invoice_billing_instruction")
                ),
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


def _allocation_to_data(allocation: BusinessContextAllocation) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "allocation_key": allocation.allocation_key,
        "allocation_type": allocation.allocation_type.value,
    }
    optional: dict[str, Any] = {
        "source_line_number": allocation.source_line_number,
        "description": allocation.description,
        "amount": str(allocation.amount) if allocation.amount is not None else None,
        "percentage": str(allocation.percentage) if allocation.percentage is not None else None,
        "currency": allocation.currency,
        "customer_id": allocation.customer_id,
        "recharge_partner_id": allocation.recharge_partner_id,
        "customer_invoice_id": allocation.customer_invoice_id,
        "target_company_id": allocation.target_company_id,
        "opportunity_id": allocation.opportunity_id,
        "sales_order_id": allocation.sales_order_id,
        "sales_order_line_id": allocation.sales_order_line_id,
        "proposal_scenario_id": allocation.proposal_scenario_id,
        "purchase_order_id": allocation.purchase_order_id,
        "project_id": allocation.project_id,
        "analytic_account_id": allocation.analytic_account_id,
        "subscription_id": allocation.subscription_id,
        "internal_note": allocation.internal_note,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _allocation_from_data(data: dict[str, Any]) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=str(data["allocation_key"]),
        allocation_type=BusinessContextAllocationType(str(data["allocation_type"])),
        source_line_number=_optional_string(data.get("source_line_number")),
        description=_optional_string(data.get("description")),
        amount=_optional_decimal(data.get("amount")),
        percentage=_optional_decimal(data.get("percentage")),
        currency=_optional_string(data.get("currency")),
        customer_id=_optional_int(data.get("customer_id")),
        recharge_partner_id=_optional_int(data.get("recharge_partner_id")),
        customer_invoice_id=_optional_int(data.get("customer_invoice_id")),
        target_company_id=_optional_int(data.get("target_company_id")),
        opportunity_id=_optional_int(data.get("opportunity_id")),
        sales_order_id=_optional_int(data.get("sales_order_id")),
        sales_order_line_id=_optional_int(data.get("sales_order_line_id")),
        proposal_scenario_id=_optional_int(data.get("proposal_scenario_id")),
        purchase_order_id=_optional_int(data.get("purchase_order_id")),
        project_id=_optional_int(data.get("project_id")),
        analytic_account_id=_optional_int(data.get("analytic_account_id")),
        subscription_id=_optional_int(data.get("subscription_id")),
        internal_note=_optional_string(data.get("internal_note")),
    )


def _billing_instruction_to_data(instruction: CustomerInvoiceBillingInstruction | None) -> dict[str, Any] | None:
    if instruction is None:
        return None
    return {
        "billing_key": instruction.billing_key,
        "customer_id": instruction.customer_id,
        "currency": instruction.currency,
        "lines": [
            {
                "allocation_key": line.allocation_key,
                "product_id": line.product_id,
                "description": line.description,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "sales_tax_ids": list(line.sales_tax_ids),
            }
            for line in instruction.lines
        ],
    }


def _billing_instruction_from_data(data: Any) -> CustomerInvoiceBillingInstruction | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ExecutionPersistenceError(SAFE_EXECUTION_PERSISTENCE_ERROR)
    return CustomerInvoiceBillingInstruction(
        billing_key=str(data["billing_key"]),
        customer_id=int(data["customer_id"]),
        currency=str(data["currency"]),
        lines=tuple(
            CustomerInvoiceBillingLine(
                allocation_key=str(line["allocation_key"]),
                product_id=int(line["product_id"]),
                description=str(line["description"]),
                quantity=Decimal(str(line["quantity"])),
                unit_price=Decimal(str(line["unit_price"])),
                sales_tax_ids=tuple(int(tax_id) for tax_id in line["sales_tax_ids"]),
            )
            for line in data["lines"]
        ),
    )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


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
        "produced_artifacts": [_artifact_to_data(artifact) for artifact in result.produced_artifacts],
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
        produced_artifacts=_artifacts_from_step_result_data(data),
        error_code=str(data["error_code"]) if data.get("error_code") is not None else None,
    )


def _artifact_to_data(artifact: ExecutionArtifact) -> dict[str, Any]:
    return {
        "artifact_type": artifact.artifact_type.value,
        "artifact_id": artifact.artifact_id,
        "external_identity": artifact.external_identity,
        "created": artifact.created,
    }


def _artifact_from_data(data: dict[str, Any]) -> ExecutionArtifact:
    return ExecutionArtifact(
        artifact_type=ExecutionArtifactType(str(data["artifact_type"])),
        artifact_id=str(data["artifact_id"]),
        external_identity=str(data["external_identity"]),
        created=bool(data["created"]),
    )


def _artifacts_from_step_result_data(data: dict[str, Any]) -> tuple[ExecutionArtifact, ...]:
    if "produced_artifacts" in data:
        return tuple(_artifact_from_data(artifact) for artifact in (data.get("produced_artifacts") or ()))
    if str(data.get("step_type")) == ExecutionStepType.VENDOR_BILL.value:
        return tuple(
            ExecutionArtifact(
                artifact_type=ExecutionArtifactType.VENDOR_BILL,
                artifact_id=str(ref),
                external_identity=str(ref),
                created=False,
            )
            for ref in data.get("produced_reference_ids", ())
        )
    return ()


def _plan_idempotency_key(plan: ExecutionPlan) -> str:
    if plan.idempotency_key is None:
        raise ExecutionStateError("Execution plan idempotency_key is required for persistence.")
    return plan.idempotency_key


def _plan_signature(plan: ExecutionPlan) -> str:
    identity = {
        "review_id": plan.review_id,
        "company_id": plan.company_id,
        "decision_version": plan.decision_version,
        "decision_id": plan.decision_id,
        "mode": plan.mode.value,
        "steps": [
            {
                "step_key": step.step_key,
                "step_type": step.step_type.value,
                "allocation_keys": list(step.allocation_keys),
                "sequence": step.sequence,
                "execute_supported": step.execute_supported,
                "writer_required": step.writer_required,
                "allocations": [_allocation_to_data(allocation) for allocation in step.allocations],
                "customer_invoice_billing_instruction": _billing_instruction_to_data(
                    step.customer_invoice_billing_instruction
                ),
            }
            for step in plan.steps
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
