"""P0-PROD-12A: SqlAlchemyExecutionRuntimeRepository.find_latest_snapshot_for_review.

Proves the new narrow read (added for the operator execution-status endpoint) picks
the correct real (EXECUTE-mode) execution when a review has accumulated rows across
decision versions and modes, returns None when only DRY_RUN rows exist (a plain trial
POST /execute defaults to DRY_RUN and is itself persisted, unlike the separate
zero-write preview endpoint), and never touches any other review's rows.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution.contracts import ExecutionMode, ExecutionPlan, ExecutionStep, ExecutionStepType
from app.application.execution.runtime import ExecutionRetryPolicy
from app.db.base import Base
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository

COMPANY_ID = 7
REVIEW_ID = "review-1"
OTHER_REVIEW_ID = "review-2"


def _plan(
    *, execution_id: str, decision_version: int, mode: ExecutionMode, review_id: str = REVIEW_ID
) -> ExecutionPlan:
    return ExecutionPlan(
        execution_id=execution_id,
        review_id=review_id,
        company_id=COMPANY_ID,
        decision_version=decision_version,
        mode=mode,
        idempotency_key=f"idem-{execution_id}",
        steps=(
            ExecutionStep(
                step_key="vendor-bill",
                step_type=ExecutionStepType.VENDOR_BILL,
                allocation_keys=(),
                sequence=1,
                execute_supported=True,
                writer_required=True,
            ),
        ),
    )


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


def test_no_execution_for_review_returns_none() -> None:
    repository = SqlAlchemyExecutionRuntimeRepository(_session())
    assert repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID) is None


def test_dry_run_only_review_returns_none_execute_mode_is_required() -> None:
    session = _session()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    repository.create_from_plan(
        plan=_plan(execution_id="exec-dry-run", decision_version=1, mode=ExecutionMode.DRY_RUN),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    assert repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID) is None


def test_picks_the_latest_decision_version_execute_execution() -> None:
    session = _session()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    repository.create_from_plan(
        plan=_plan(execution_id="exec-v2", decision_version=2, mode=ExecutionMode.EXECUTE),
        retry_policy=ExecutionRetryPolicy.never(),
    )
    repository.create_from_plan(
        plan=_plan(execution_id="exec-v3", decision_version=3, mode=ExecutionMode.EXECUTE),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    snapshot = repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert snapshot is not None
    assert snapshot.execution_id == "exec-v3"
    assert snapshot.decision_version == 3


def test_ignores_a_dry_run_row_created_after_the_real_execute_row() -> None:
    """A trial POST /execute (mode defaults to DRY_RUN) issued after the real EXECUTE
    attempt must not mask the real execution an operator is diagnosing."""

    session = _session()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    repository.create_from_plan(
        plan=_plan(execution_id="exec-real", decision_version=3, mode=ExecutionMode.EXECUTE),
        retry_policy=ExecutionRetryPolicy.never(),
    )
    repository.create_from_plan(
        plan=_plan(execution_id="exec-trial-dry-run", decision_version=3, mode=ExecutionMode.DRY_RUN),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    snapshot = repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID)

    assert snapshot is not None
    assert snapshot.execution_id == "exec-real"
    assert snapshot.mode is ExecutionMode.EXECUTE


def test_never_returns_another_reviews_execution() -> None:
    session = _session()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    repository.create_from_plan(
        plan=_plan(
            execution_id="exec-other-review", decision_version=1, mode=ExecutionMode.EXECUTE, review_id=OTHER_REVIEW_ID
        ),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    assert repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID) is None


def test_never_returns_another_companys_execution() -> None:
    session = _session()
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    repository.create_from_plan(
        plan=ExecutionPlan(
            execution_id="exec-other-company",
            review_id=REVIEW_ID,
            company_id=COMPANY_ID + 1,
            decision_version=1,
            mode=ExecutionMode.EXECUTE,
            idempotency_key="idem-exec-other-company",
            steps=(
                ExecutionStep(
                    step_key="vendor-bill",
                    step_type=ExecutionStepType.VENDOR_BILL,
                    allocation_keys=(),
                    sequence=1,
                    execute_supported=True,
                    writer_required=True,
                ),
            ),
        ),
        retry_policy=ExecutionRetryPolicy.never(),
    )

    assert repository.find_latest_snapshot_for_review(review_id=REVIEW_ID, company_id=COMPANY_ID) is None
