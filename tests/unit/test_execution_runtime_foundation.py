from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionIdempotencyConflictError,
    ExecutionMode,
    ExecutionPlanner,
    ExecutionRequest,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeService,
    ExecutionRuntimeStepState,
    ExecutionState,
    ExecutionStateError,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategyResolver,
    FoundationExecutionStrategy,
    StaticRetryPolicyResolver,
    assert_legal_transition,
)
from app.db.base import Base
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository


def test_runtime_contracts_are_immutable_and_state_machine_rejects_illegal_transition() -> None:
    plan = _plan()
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=plan, retry_policy=ExecutionRetryPolicy.never())

    assert snapshot.state is ExecutionState.PLANNED
    assert snapshot.checkpoint.current_step_key == plan.steps[0].step_key
    with pytest.raises(FrozenInstanceError):
        snapshot.state = ExecutionState.RUNNING  # type: ignore[misc]
    with pytest.raises(ExecutionStateError):
        assert_legal_transition(ExecutionState.PLANNED, ExecutionState.COMPLETED)


def test_runtime_creation_is_idempotent_for_same_plan_and_rejects_conflict() -> None:
    session = _session()
    repository = _repository(session)
    first = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    same = repository.create_from_plan(
        plan=_plan(execution_id="exec-replayed"),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    assert same.execution_id == first.execution_id
    assert session.query(WorkflowExecution).count() == 1

    conflicting = _plan(decision_version=5)
    object.__setattr__(conflicting, "idempotency_key", first.idempotency_key)
    with pytest.raises(ExecutionIdempotencyConflictError):
        repository.create_from_plan(plan=conflicting, retry_policy=ExecutionRetryPolicy.never())


def test_runtime_service_appends_creation_and_planning_events_once() -> None:
    session = _session()
    repository = _repository(session)
    service = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository)

    runtime = service.create_or_load(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    replayed = service.create_or_load(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())

    assert [event.event_type for event in runtime.history.events] == [
        ExecutionEventType.EXECUTION_CREATED,
        ExecutionEventType.PLANNING_COMPLETED,
    ]
    assert len(replayed.history.events) == 2
    assert session.query(WorkflowExecutionEvent).count() == 2
    assert repository.get_checkpoint(execution_id=runtime.snapshot.execution_id).last_event_id is not None


def test_checkpoint_save_and_restore() -> None:
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    checkpoint = snapshot.checkpoint
    restored = repository.save_checkpoint(
        type(checkpoint)(
            execution_id=checkpoint.execution_id,
            completed_step_keys=(snapshot.steps[0].step_key,),
            failed_step_key=None,
            current_step_key=None,
            retry_count=0,
            last_event_id="event-1",
        )
    )

    assert restored.completed_step_keys == (snapshot.steps[0].step_key,)
    assert repository.get_checkpoint(execution_id=snapshot.execution_id) == restored


def test_runtime_coordinator_persists_events_snapshot_and_resumes_from_crash_cursor() -> None:
    session = _session()
    repository = _repository(session)
    plan = _plan_with_two_steps()
    runtime = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository).create_or_load(
        plan=plan,
        retry_policy=ExecutionRetryPolicy.never(),
    )
    first_step = runtime.snapshot.steps[0]
    snapshot = repository.save_snapshot(
        _snapshot_with_completed_first_step(runtime.snapshot, result=_dry_run_result(first_step))
    )

    result = ExecutionRuntimeCoordinator(
        runtime_repository=repository,
        event_repository=repository,
        strategy_resolver=ExecutionStrategyResolver(
            (FoundationExecutionStrategy(supported_step_types=tuple(step.step_type for step in plan.steps)),)
        ),
    ).execute(snapshot)

    restored = repository.get_snapshot(execution_id=plan.execution_id)
    assert result.status.value == "dry_run_completed"
    assert restored is not None
    assert restored.state is ExecutionState.COMPLETED
    assert restored.checkpoint.completed_step_keys == tuple(step.step_key for step in restored.steps)
    assert [step.state for step in restored.steps] == [
        ExecutionRuntimeStepState.COMPLETED,
        ExecutionRuntimeStepState.COMPLETED,
    ]
    assert session.query(WorkflowExecutionStep).count() == 2
    event_types = [event.event_type for event in repository.history(execution_id=plan.execution_id).events]
    assert ExecutionEventType.STEP_STARTED in event_types
    assert ExecutionEventType.EXECUTION_COMPLETED in event_types


def test_retry_policy_schedules_retry_without_background_work() -> None:
    repository = _repository(_session())
    plan = _plan()
    runtime = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository).create_or_load(
        plan=plan,
        retry_policy=ExecutionRetryPolicy.immediate(max_attempts=2),
    )

    result = ExecutionRuntimeCoordinator(
        runtime_repository=repository,
        event_repository=repository,
        strategy_resolver=ExecutionStrategyResolver(
            (FailingStrategy(supported_step_types=(ExecutionStepType.INTERNAL_COST,)),)
        ),
    ).execute(runtime.snapshot)

    restored = repository.get_snapshot(execution_id=plan.execution_id)
    event_types = [event.event_type for event in repository.history(execution_id=plan.execution_id).events]
    assert result.status.value == "planned"
    assert restored is not None
    assert restored.state is ExecutionState.WAITING_RETRY
    assert restored.steps[0].retry_count == 1
    assert ExecutionEventType.RETRY_SCHEDULED in event_types


def test_never_retry_policy_fails_execution_safely() -> None:
    repository = _repository(_session())
    plan = _plan()
    runtime = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository).create_or_load(
        plan=plan,
        retry_policy=StaticRetryPolicyResolver().resolve(plan),
    )

    result = ExecutionRuntimeCoordinator(
        runtime_repository=repository,
        event_repository=repository,
        strategy_resolver=ExecutionStrategyResolver(
            (FailingStrategy(supported_step_types=(ExecutionStepType.INTERNAL_COST,)),)
        ),
    ).execute(runtime.snapshot)

    restored = repository.get_snapshot(execution_id=plan.execution_id)
    assert result.status.value == "failed"
    assert restored is not None
    assert restored.state is ExecutionState.FAILED
    assert restored.failure is not None
    assert restored.failure.error_code == "SAFE_FAILURE"


def test_event_store_is_append_only_from_repository_surface() -> None:
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    event = ExecutionEvent(
        event_id="event-manual-1",
        execution_id=snapshot.execution_id,
        event_type=ExecutionEventType.EXECUTION_STARTED,
        sequence=1,
        state=ExecutionState.RUNNING,
    )

    repository.append(event)

    assert repository.history(execution_id=snapshot.execution_id).events == (event,)
    assert not hasattr(repository, "update_event")
    assert not hasattr(repository, "delete_event")


def test_sqlite_schema_contains_runtime_tables() -> None:
    engine = _engine()
    Base.metadata.create_all(
        engine,
        tables=[
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    inspector = inspect(engine)

    assert {
        "workflow_executions",
        "workflow_execution_steps",
        "workflow_execution_events",
    }.issubset(inspector.get_table_names())
    execution_columns = {column["name"] for column in inspector.get_columns("workflow_executions")}
    assert {
        "execution_id",
        "review_id",
        "decision_version",
        "company_id",
        "state",
        "mode",
        "idempotency_key",
        "checkpoint",
        "retry_policy",
    }.issubset(execution_columns)
    assert "workflow_execution_events" in inspector.get_table_names()


@pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL runtime validation.",
)
def test_postgresql_runtime_tables_can_be_created() -> None:
    engine = create_engine(os.environ["POSTGRES_TEST_DATABASE_URL"])
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_execution_events")
        connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_execution_steps")
        connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_executions")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    try:
        inspector = inspect(engine)
        assert "workflow_executions" in inspector.get_table_names()
        assert "workflow_execution_steps" in inspector.get_table_names()
        assert "workflow_execution_events" in inspector.get_table_names()
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_execution_events")
            connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_execution_steps")
            connection.exec_driver_sql("DROP TABLE IF EXISTS workflow_executions")
        engine.dispose()


def test_execution_runtime_application_layer_has_no_provider_writes_or_workers() -> None:
    source = "\n".join(
        Path(path).read_text(encoding="utf-8").lower()
        for path in (
            "app/application/execution/runtime.py",
            "app/application/execution/runtime_service.py",
            "app/application/execution/ports.py",
        )
    )
    forbidden = (
        "app.erp",
        "app.connectors",
        "sqlalchemy",
        "fastapi",
        "vendorbillwriter",
        "account.move",
        "action_post",
        "purchase order",
        "rfq",
        "http",
        "requests",
        "scheduler",
        "background",
        "threading",
        "ai_advisor",
        "fuzzy",
        "embedding",
    )

    for token in forbidden:
        assert token not in source


class FailingStrategy:
    name = "failing"

    def __init__(self, *, supported_step_types: tuple[ExecutionStepType, ...]) -> None:
        self.supported_step_types = supported_step_types

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.FAILED,
            dry_run=request.mode is ExecutionMode.DRY_RUN,
            message="Step failed safely.",
            error_code="SAFE_FAILURE",
        )


def _snapshot_with_completed_first_step(snapshot, *, result: ExecutionStepResult):
    from app.application.execution.runtime import (
        ExecutionCheckpoint,
        ExecutionRuntimeStep,
        ExecutionSnapshot,
    )

    completed = ExecutionRuntimeStep(
        step_key=snapshot.steps[0].step_key,
        step_type=snapshot.steps[0].step_type,
        sequence=snapshot.steps[0].sequence,
        state=ExecutionRuntimeStepState.COMPLETED,
        allocation_keys=snapshot.steps[0].allocation_keys,
        last_result=result,
    )
    checkpoint = ExecutionCheckpoint(
        execution_id=snapshot.execution_id,
        completed_step_keys=(completed.step_key,),
        failed_step_key=None,
        current_step_key=snapshot.steps[1].step_key,
        retry_count=0,
        last_event_id=snapshot.checkpoint.last_event_id,
    )
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=ExecutionState.RUNNING,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=(completed, snapshot.steps[1]),
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
    )


def _dry_run_result(step) -> ExecutionStepResult:
    return ExecutionStepResult(
        step_key=step.step_key,
        step_type=step.step_type,
        status=ExecutionStepStatus.DRY_RUN_OK,
        dry_run=True,
    )


def _plan(*, execution_id: str = "exec-1", decision_version: int = 4):
    from decimal import Decimal

    from app.application.workbench import (
        AllocationCompleteness,
        BusinessContextAllocation,
        BusinessContextAllocationSet,
        BusinessContextAllocationType,
    )

    return ExecutionPlanner().plan(
        ExecutionRequest(
            execution_id=execution_id,
            review_id="review-1",
            company_id=7,
            decision_version=decision_version,
            decision_id="decision-1",
            idempotency_key="request-key",
            mode=ExecutionMode.DRY_RUN,
            selected_workflow=None,
            business_context_allocations=BusinessContextAllocationSet(
                allocations=(
                    BusinessContextAllocation(
                        allocation_key="A",
                        allocation_type=BusinessContextAllocationType.INTERNAL_COST,
                        amount=Decimal("10.00"),
                        currency="TRY",
                    ),
                ),
                completeness=AllocationCompleteness.PARTIAL,
                invoice_total=Decimal("10.00"),
                currency="TRY",
            ),
        )
    )


def _plan_with_two_steps():
    from decimal import Decimal

    from app.application.workbench import (
        AllocationCompleteness,
        BusinessContextAllocation,
        BusinessContextAllocationSet,
        BusinessContextAllocationType,
    )

    return ExecutionPlanner().plan(
        ExecutionRequest(
            execution_id="exec-2",
            review_id="review-2",
            company_id=7,
            decision_version=4,
            decision_id="decision-2",
            idempotency_key="request-key-2",
            mode=ExecutionMode.DRY_RUN,
            selected_workflow=None,
            business_context_allocations=BusinessContextAllocationSet(
                allocations=(
                    BusinessContextAllocation(
                        allocation_key="A",
                        allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
                        amount=Decimal("10.00"),
                        currency="TRY",
                        purchase_order_id=501,
                    ),
                    BusinessContextAllocation(
                        allocation_key="B",
                        allocation_type=BusinessContextAllocationType.INTERNAL_COST,
                        amount=Decimal("10.00"),
                        currency="TRY",
                    ),
                ),
                completeness=AllocationCompleteness.PARTIAL,
                invoice_total=Decimal("20.00"),
                currency="TRY",
            ),
        )
    )


def _repository(session: Session) -> SqlAlchemyExecutionRuntimeRepository:
    return SqlAlchemyExecutionRuntimeRepository(session)


def _session() -> Session:
    factory = sessionmaker(bind=_engine())
    return factory()


def _engine() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    return engine
