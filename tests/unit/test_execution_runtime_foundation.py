from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import app.application.execution as execution_exports
from app.application.execution import (
    ExecutionConcurrencyConflictError,
    ExecutionEvent,
    ExecutionEventDraft,
    ExecutionEventRepository,
    ExecutionEventType,
    ExecutionIdempotencyConflictError,
    ExecutionMode,
    ExecutionPersistenceError,
    ExecutionPlanner,
    ExecutionRequest,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeRepository,
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
    history = repository.history(execution_id=runtime.snapshot.execution_id)
    checkpoint = repository.get_checkpoint(execution_id=runtime.snapshot.execution_id)
    assert checkpoint.last_event_id == history.events[-1].event_id
    assert [event.sequence for event in history.events] == [1, 2]
    assert runtime.snapshot.runtime_version == 1


def test_failed_creation_event_persistence_leaves_no_partial_runtime() -> None:
    session = _session()
    repository = FailingEventModelRepository(session)

    with pytest.raises(ExecutionPersistenceError):
        repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())

    session.rollback()
    assert session.query(WorkflowExecution).count() == 0
    assert session.query(WorkflowExecutionStep).count() == 0
    assert session.query(WorkflowExecutionEvent).count() == 0


def test_checkpoint_is_readable_but_not_independently_writable() -> None:
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())

    assert repository.get_checkpoint(execution_id=snapshot.execution_id) == snapshot.checkpoint
    assert not hasattr(repository, "save_checkpoint")
    assert not hasattr(execution_exports, "ExecutionCheckpointRepository")


def test_runtime_coordinator_persists_events_snapshot_and_resumes_from_crash_cursor() -> None:
    session = _session()
    repository = _repository(session)
    plan = _plan_with_two_steps()
    runtime = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository).create_or_load(
        plan=plan,
        retry_policy=ExecutionRetryPolicy.never(),
    )
    first_step = runtime.snapshot.steps[0]
    snapshot = _persist_completed_first_step(repository, runtime.snapshot, result=_dry_run_result(first_step))

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
    history = repository.history(execution_id=plan.execution_id)
    assert [event.sequence for event in history.events] == list(range(1, len(history.events) + 1))
    assert restored.checkpoint.last_event_id == history.events[-1].event_id


def test_state_transition_atomically_persists_snapshot_and_event() -> None:
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())

    transitioned = repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(
            _draft(
                snapshot,
                event_type=ExecutionEventType.EXECUTION_STARTED,
                state=ExecutionState.RUNNING,
            ),
        ),
        expected_runtime_version=snapshot.runtime_version,
    )

    history = repository.history(execution_id=snapshot.execution_id)
    assert transitioned.state is ExecutionState.RUNNING
    assert transitioned.runtime_version == snapshot.runtime_version + 1
    assert history.events[-1].event_type is ExecutionEventType.EXECUTION_STARTED
    assert transitioned.checkpoint.last_event_id == history.events[-1].event_id


def test_event_insert_failure_rolls_back_snapshot_transition() -> None:
    session = _session()
    repository = FailingEventModelRepository(session)
    snapshot = SqlAlchemyExecutionRuntimeRepository(session).create_from_plan(
        plan=_plan(),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    with pytest.raises(ExecutionPersistenceError):
        repository.persist_transition(
            snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
            events=(
                _draft(
                    snapshot,
                    event_type=ExecutionEventType.EXECUTION_STARTED,
                    state=ExecutionState.RUNNING,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    session.rollback()
    restored = SqlAlchemyExecutionRuntimeRepository(session).get_snapshot(execution_id=snapshot.execution_id)
    history = SqlAlchemyExecutionRuntimeRepository(session).history(execution_id=snapshot.execution_id)
    assert restored.state is ExecutionState.PLANNED
    assert restored.runtime_version == snapshot.runtime_version
    assert [event.event_type for event in history.events] == [
        ExecutionEventType.EXECUTION_CREATED,
        ExecutionEventType.PLANNING_COMPLETED,
    ]


def test_step_completion_event_failure_rolls_back_step_and_checkpoint_change() -> None:
    session = _session()
    setup_repository = SqlAlchemyExecutionRuntimeRepository(session)
    snapshot = setup_repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    running = setup_repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
        expected_runtime_version=snapshot.runtime_version,
    )
    repository = FailingEventModelRepository(session)
    step = running.steps[0]

    with pytest.raises(ExecutionPersistenceError):
        repository.persist_transition(
            snapshot=_completed_step_snapshot(running, step, result=_dry_run_result(step)),
            events=(
                _draft(
                    running,
                    event_type=ExecutionEventType.STEP_COMPLETED,
                    state=ExecutionState.RUNNING,
                    step=step,
                    result=_dry_run_result(step),
                ),
            ),
            expected_runtime_version=running.runtime_version,
        )

    session.rollback()
    restored = setup_repository.get_snapshot(execution_id=running.execution_id)
    assert restored.steps[0].state is ExecutionRuntimeStepState.PENDING
    assert restored.checkpoint.completed_step_keys == ()
    assert restored.runtime_version == running.runtime_version


def test_retry_event_failure_rolls_back_waiting_retry_transition() -> None:
    session = _session()
    setup_repository = SqlAlchemyExecutionRuntimeRepository(session)
    snapshot = setup_repository.create_from_plan(
        plan=_plan(),
        retry_policy=ExecutionRetryPolicy.immediate(max_attempts=2),
    )
    running = setup_repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
        expected_runtime_version=snapshot.runtime_version,
    )
    step = running.steps[0]
    failed = _failed_step_snapshot(
        running,
        step,
        step_state=ExecutionRuntimeStepState.WAITING_RETRY,
        runtime_state=ExecutionState.WAITING_RETRY,
        result=_failed_result(step),
    )

    with pytest.raises(ExecutionPersistenceError):
        FailingEventModelRepository(session).persist_transition(
            snapshot=failed,
            events=(
                _draft(running, event_type=ExecutionEventType.STEP_FAILED, state=ExecutionState.RUNNING, step=step),
                _draft(
                    running,
                    event_type=ExecutionEventType.RETRY_SCHEDULED,
                    state=ExecutionState.WAITING_RETRY,
                    step=step,
                ),
            ),
            expected_runtime_version=running.runtime_version,
        )

    session.rollback()
    restored = setup_repository.get_snapshot(execution_id=running.execution_id)
    assert restored.state is ExecutionState.RUNNING
    assert restored.steps[0].state is ExecutionRuntimeStepState.PENDING
    assert restored.steps[0].retry_count == 0


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
    assert event_types[-2:] == [ExecutionEventType.STEP_FAILED, ExecutionEventType.RETRY_SCHEDULED]
    events = repository.history(execution_id=plan.execution_id).events
    assert events[-1].sequence == events[-2].sequence + 1
    assert restored.checkpoint.last_event_id == events[-1].event_id


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
    events = repository.history(execution_id=plan.execution_id).events
    assert [event.event_type for event in events[-2:]] == [
        ExecutionEventType.STEP_FAILED,
        ExecutionEventType.EXECUTION_FAILED,
    ]
    assert events[-1].sequence == events[-2].sequence + 1


def test_event_store_is_append_only_readable_and_has_no_public_append_escape_hatch() -> None:
    repository = _repository(_session())
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
        expected_runtime_version=snapshot.runtime_version,
    )

    history = repository.history(execution_id=snapshot.execution_id)
    assert [event.sequence for event in history.events] == [1, 2, 3]
    assert history.events[-1].event_type is ExecutionEventType.EXECUTION_STARTED
    assert not hasattr(repository, "append")
    assert not hasattr(repository, "update_event")
    assert not hasattr(repository, "delete_event")


def test_runtime_ports_expose_only_atomic_mutation_surface() -> None:
    assert not hasattr(ExecutionRuntimeRepository, "save_snapshot")
    assert not hasattr(ExecutionEventRepository, "append")
    assert not hasattr(SqlAlchemyExecutionRuntimeRepository, "save_snapshot")
    assert not hasattr(SqlAlchemyExecutionRuntimeRepository, "append")
    assert not hasattr(SqlAlchemyExecutionRuntimeRepository, "save_checkpoint")
    assert hasattr(ExecutionRuntimeRepository, "persist_transition")


def test_no_len_history_event_sequencing_remains_in_application_coordinator() -> None:
    source = Path("app/application/execution/runtime_service.py").read_text(encoding="utf-8")

    assert "len(history.events)" not in source
    assert "ExecutionEvent(" not in source
    assert "sequence=len" not in source


def test_stale_transition_is_rejected_and_creates_no_event() -> None:
    session = _session()
    repository = _repository(session)
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    transitioned = repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
        expected_runtime_version=snapshot.runtime_version,
    )

    with pytest.raises(ExecutionConcurrencyConflictError):
        repository.persist_transition(
            snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
            events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
            expected_runtime_version=snapshot.runtime_version,
        )

    session.rollback()
    restored = repository.get_snapshot(execution_id=snapshot.execution_id)
    history = repository.history(execution_id=snapshot.execution_id)
    assert restored.runtime_version == transitioned.runtime_version
    assert restored.state is ExecutionState.RUNNING
    assert [event.event_type for event in history.events].count(ExecutionEventType.EXECUTION_STARTED) == 1


def test_retrying_after_concurrency_conflict_does_not_overwrite_state() -> None:
    session = _session()
    repository = _repository(session)
    snapshot = repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
    repository.persist_transition(
        snapshot=_snapshot_state(snapshot, ExecutionState.RUNNING),
        events=(_draft(snapshot, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
        expected_runtime_version=snapshot.runtime_version,
    )
    stale_completed = _snapshot_state(snapshot, ExecutionState.COMPLETED)

    with pytest.raises(ExecutionConcurrencyConflictError):
        repository.persist_transition(
            snapshot=stale_completed,
            events=(
                _draft(
                    snapshot,
                    event_type=ExecutionEventType.EXECUTION_COMPLETED,
                    state=ExecutionState.COMPLETED,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    session.rollback()
    restored = repository.get_snapshot(execution_id=snapshot.execution_id)
    assert restored.state is ExecutionState.RUNNING


def test_history_sequence_is_strictly_increasing_unique_and_checkpoint_tracks_final_event() -> None:
    repository = _repository(_session())
    runtime = ExecutionRuntimeService(runtime_repository=repository, event_repository=repository).create_or_load(
        plan=_plan(),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    ExecutionRuntimeCoordinator(
        runtime_repository=repository,
        event_repository=repository,
        strategy_resolver=ExecutionStrategyResolver(
            (FoundationExecutionStrategy(supported_step_types=(ExecutionStepType.INTERNAL_COST,)),)
        ),
    ).execute(runtime.snapshot)

    restored = repository.get_snapshot(execution_id=runtime.snapshot.execution_id)
    history = repository.history(execution_id=runtime.snapshot.execution_id)
    sequences = [event.sequence for event in history.events]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert restored.checkpoint.last_event_id == history.events[-1].event_id


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
        "runtime_version",
        "next_event_sequence",
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


@pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL runtime concurrency validation.",
)
def test_postgresql_stale_runtime_transition_is_rejected() -> None:
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
    factory = sessionmaker(bind=engine)
    try:
        with factory() as first_session:
            first_repository = _repository(first_session)
            snapshot = first_repository.create_from_plan(plan=_plan(), retry_policy=ExecutionRetryPolicy.never())
            first_session.commit()

        with factory() as first_session, factory() as second_session:
            first_repository = _repository(first_session)
            second_repository = _repository(second_session)
            fresh = first_repository.get_snapshot(execution_id=snapshot.execution_id)
            stale = second_repository.get_snapshot(execution_id=snapshot.execution_id)
            first_repository.persist_transition(
                snapshot=_snapshot_state(fresh, ExecutionState.RUNNING),
                events=(_draft(fresh, event_type=ExecutionEventType.EXECUTION_STARTED, state=ExecutionState.RUNNING),),
                expected_runtime_version=fresh.runtime_version,
            )
            first_session.commit()

            with pytest.raises(ExecutionConcurrencyConflictError):
                second_repository.persist_transition(
                    snapshot=_snapshot_state(stale, ExecutionState.RUNNING),
                    events=(
                        _draft(
                            stale,
                            event_type=ExecutionEventType.EXECUTION_STARTED,
                            state=ExecutionState.RUNNING,
                        ),
                    ),
                    expected_runtime_version=stale.runtime_version,
                )
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


class FailingEventModelRepository(SqlAlchemyExecutionRuntimeRepository):
    def _event_model_from_event(self, event: ExecutionEvent) -> WorkflowExecutionEvent:
        raise SQLAlchemyError(f"simulated event failure for {event.event_id}")


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
        runtime_version=snapshot.runtime_version,
    )


def _persist_completed_first_step(
    repository: SqlAlchemyExecutionRuntimeRepository,
    snapshot,
    *,
    result: ExecutionStepResult,
):
    next_snapshot = _snapshot_with_completed_first_step(snapshot, result=result)
    return repository.persist_transition(
        snapshot=next_snapshot,
        events=(
            _draft(
                snapshot,
                event_type=ExecutionEventType.STEP_COMPLETED,
                state=ExecutionState.RUNNING,
                step=snapshot.steps[0],
                result=result,
            ),
        ),
        expected_runtime_version=snapshot.runtime_version,
    )


def _snapshot_state(snapshot, state: ExecutionState):
    from app.application.execution.runtime import ExecutionCheckpoint, ExecutionSnapshot

    checkpoint = ExecutionCheckpoint(
        execution_id=snapshot.execution_id,
        completed_step_keys=snapshot.checkpoint.completed_step_keys,
        failed_step_key=snapshot.checkpoint.failed_step_key,
        current_step_key=None if state is ExecutionState.COMPLETED else snapshot.checkpoint.current_step_key,
        retry_count=snapshot.checkpoint.retry_count,
        last_event_id=snapshot.checkpoint.last_event_id,
    )
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=state,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=snapshot.steps,
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=snapshot.failure,
    )


def _completed_step_snapshot(snapshot, step, *, result: ExecutionStepResult):
    from app.application.execution.runtime import ExecutionCheckpoint, ExecutionRuntimeStep, ExecutionSnapshot

    steps = tuple(
        ExecutionRuntimeStep(
            step_key=item.step_key,
            step_type=item.step_type,
            sequence=item.sequence,
            state=ExecutionRuntimeStepState.COMPLETED if item.step_key == step.step_key else item.state,
            allocation_keys=item.allocation_keys,
            retry_count=item.retry_count,
            last_result=result if item.step_key == step.step_key else item.last_result,
        )
        for item in snapshot.steps
    )
    checkpoint = ExecutionCheckpoint(
        execution_id=snapshot.execution_id,
        completed_step_keys=(*snapshot.checkpoint.completed_step_keys, step.step_key),
        failed_step_key=None,
        current_step_key=None,
        retry_count=snapshot.checkpoint.retry_count,
        last_event_id=snapshot.checkpoint.last_event_id,
    )
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=snapshot.state,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=steps,
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=snapshot.failure,
    )


def _failed_step_snapshot(
    snapshot,
    step,
    *,
    step_state: ExecutionRuntimeStepState,
    runtime_state: ExecutionState,
    result: ExecutionStepResult,
):
    from app.application.execution.runtime import (
        ExecutionCheckpoint,
        ExecutionFailure,
        ExecutionRuntimeStep,
        ExecutionSnapshot,
    )

    steps = tuple(
        ExecutionRuntimeStep(
            step_key=item.step_key,
            step_type=item.step_type,
            sequence=item.sequence,
            state=step_state if item.step_key == step.step_key else item.state,
            allocation_keys=item.allocation_keys,
            retry_count=item.retry_count + 1 if item.step_key == step.step_key else item.retry_count,
            last_result=result if item.step_key == step.step_key else item.last_result,
        )
        for item in snapshot.steps
    )
    checkpoint = ExecutionCheckpoint(
        execution_id=snapshot.execution_id,
        completed_step_keys=snapshot.checkpoint.completed_step_keys,
        failed_step_key=step.step_key,
        current_step_key=step.step_key,
        retry_count=step.retry_count + 1,
        last_event_id=snapshot.checkpoint.last_event_id,
    )
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=runtime_state,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=steps,
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=ExecutionFailure(
            step_key=step.step_key,
            error_code=result.error_code or result.status.value,
            safe_message=result.message or "Step failed safely.",
        ),
    )


def _draft(
    snapshot,
    *,
    event_type: ExecutionEventType,
    state: ExecutionState,
    step=None,
    result: ExecutionStepResult | None = None,
) -> ExecutionEventDraft:
    data: dict[str, str | int | bool | None] = {}
    if result is not None:
        data = {
            "status": result.status.value,
            "dry_run": result.dry_run,
            "error_code": result.error_code,
            "message": result.message,
        }
    return ExecutionEventDraft(
        event_id=f"event-{event_type.value}-{len(snapshot.checkpoint.completed_step_keys)}",
        execution_id=snapshot.execution_id,
        event_type=event_type,
        state=state,
        step_key=step.step_key if step is not None else None,
        step_type=step.step_type if step is not None else None,
        data=data,
    )


def _dry_run_result(step) -> ExecutionStepResult:
    return ExecutionStepResult(
        step_key=step.step_key,
        step_type=step.step_type,
        status=ExecutionStepStatus.DRY_RUN_OK,
        dry_run=True,
    )


def _failed_result(step) -> ExecutionStepResult:
    return ExecutionStepResult(
        step_key=step.step_key,
        step_type=step.step_type,
        status=ExecutionStepStatus.FAILED,
        dry_run=True,
        message="Step failed safely.",
        error_code="SAFE_FAILURE",
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
