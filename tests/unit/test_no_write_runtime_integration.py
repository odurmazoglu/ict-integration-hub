from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.application.execution as execution_exports
import app.application.execution.accepted_decision_use_cases as accepted_decision_use_cases
from app.application.execution import (
    AcceptedDecisionExecutionStatus,
    AcceptedReviewDecision,
    AcceptedReviewDecisionReader,
    ExecutionApprovalError,
    ExecutionCheckpoint,
    ExecutionEventDraft,
    ExecutionEventRepository,
    ExecutionEventType,
    ExecutionMode,
    ExecutionPlan,
    ExecutionPlanner,
    ExecutionPlanningError,
    ExecutionRequest,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeRepository,
    ExecutionRuntimeService,
    ExecutionRuntimeStep,
    ExecutionRuntimeStepState,
    ExecutionSnapshot,
    ExecutionSourceInvoice,
    ExecutionState,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategy,
    ExecutionStrategyResolver,
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    foundation_no_write_strategy_resolver,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewItem,
    ReviewStatus,
)
from app.application.workbench.exceptions import ReviewDecisionDataIntegrityError, ReviewNotFoundError
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, MonetaryTotals, Party
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence import SqlAlchemyReviewRepository
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository
from app.tax_mapping import InvoiceTaxMappingResult


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=_engine())
    with factory() as db_session:
        yield db_session


def test_runtime_ports_expose_no_independent_mutation_escape_hatches() -> None:
    assert not hasattr(ExecutionRuntimeRepository, "save_snapshot")
    assert not hasattr(ExecutionEventRepository, "append")
    assert not hasattr(ExecutionRuntimeRepository, "save_checkpoint")
    assert "create_from_plan" in ExecutionRuntimeRepository.__dict__
    assert "persist_transition" in ExecutionRuntimeRepository.__dict__
    assert "history" in ExecutionEventRepository.__dict__
    assert "decision_version" in inspect.signature(AcceptedReviewDecisionReader.get_accepted_decision).parameters
    assert not hasattr(execution_exports, "ExecutionCheckpointRepository")


def test_runtime_coordinator_has_no_non_atomic_mutation_calls() -> None:
    source = inspect.getsource(ExecutionRuntimeCoordinator)

    assert ".persist_transition(" in source
    assert "save_snapshot" not in source
    assert "save_checkpoint" not in source
    assert "_event_repository.append" not in source


def test_canonical_execution_idempotency_is_owned_by_planner() -> None:
    planner_source = inspect.getsource(ExecutionPlanner)
    orchestration_source = inspect.getsource(accepted_decision_use_cases)

    assert "execution_idempotency_key(plan_without_key)" in planner_source
    assert "execution_idempotency_key(" not in orchestration_source
    assert "hashlib" not in orchestration_source
    assert "sha256" not in orchestration_source


def test_run_command_is_immutable_and_rejects_invalid_identity_values() -> None:
    command = RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=ExecutionMode.DRY_RUN,
    )

    assert command.review_id == "review-1"
    with pytest.raises(FrozenInstanceError):
        command.company_id = 8  # type: ignore[misc]
    with pytest.raises(ExecutionPlanningError):
        RunAcceptedDecisionExecutionCommand(review_id=" ", company_id=7, decision_version=2)
    with pytest.raises(ExecutionPlanningError):
        RunAcceptedDecisionExecutionCommand(review_id="review-1", company_id=True, decision_version=2)
    with pytest.raises(ExecutionPlanningError):
        RunAcceptedDecisionExecutionCommand(review_id="review-1", company_id=7, decision_version=True)
    with pytest.raises(ExecutionPlanningError):
        RunAcceptedDecisionExecutionCommand(
            review_id="review-1",
            company_id=7,
            decision_version=2,
            mode="dry_run",  # type: ignore[arg-type]
        )


def test_accepted_decision_reader_queries_exact_company_and_version(session: Session) -> None:
    review_repository = SqlAlchemyReviewRepository(session)
    _submit_select_workflow(review_repository, review_id="review-1", company_id=7)

    accepted = review_repository.get_accepted_decision(review_id="review-1", company_id=7, decision_version=2)

    assert accepted.review_id == "review-1"
    assert accepted.company_id == 7
    assert accepted.decision_version == 2
    assert accepted.decision_type is ReviewDecisionType.SELECT_WORKFLOW
    assert accepted.selected_workflow is WorkflowType.VENDOR_BILL
    assert accepted.business_context_allocations is not None
    with pytest.raises(ReviewNotFoundError):
        review_repository.get_accepted_decision(review_id="review-1", company_id=8, decision_version=2)
    with pytest.raises(ReviewNotFoundError):
        review_repository.get_accepted_decision(review_id="review-1", company_id=7, decision_version=1)


def test_accepted_decision_reader_rejects_malformed_persisted_decision(session: Session) -> None:
    _insert_review_item(session, review_id="review-1", company_id=7)
    session.add(
        WorkbenchReviewDecision(
            decision_id="decision-bad",
            review_id="review-1",
            company_id=7,
            review_version_before=1,
            review_version_after=2,
            decision_type="bogus",
            selected_workflow=WorkflowType.VENDOR_BILL.value,
            selected_partner_id=None,
            line_resolutions=[],
            tax_resolutions=[],
            business_context=None,
            business_context_allocations=None,
            comment=None,
            decided_by="finance.user",
            idempotency_key="decision-bad",
        )
    )
    session.commit()

    with pytest.raises(ReviewDecisionDataIntegrityError):
        SqlAlchemyReviewRepository(session).get_accepted_decision(
            review_id="review-1",
            company_id=7,
            decision_version=2,
        )


def test_dismissed_decision_is_not_executable_and_creates_no_runtime(session: Session) -> None:
    review_repository = SqlAlchemyReviewRepository(session)
    _submit_dismiss(review_repository, review_id="review-1", company_id=7)
    use_case = _use_case(session)

    result = use_case.execute(_command())

    assert result.status is AcceptedDecisionExecutionStatus.NOT_EXECUTABLE
    assert result.execution_id is None
    assert session.query(WorkflowExecution).count() == 0


def test_dry_run_creates_runtime_and_completes_without_provider_or_erp_calls(session: Session) -> None:
    review_repository = SqlAlchemyReviewRepository(session)
    _submit_select_workflow(review_repository, review_id="review-1", company_id=7)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    use_case = _use_case(session, runtime_repository=runtime_repository)

    result = use_case.execute(_command())

    assert result.status is AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert result.execution_id is not None
    snapshot = runtime_repository.get_snapshot(execution_id=result.execution_id)
    assert snapshot is not None
    assert snapshot.execution_id != snapshot.idempotency_key
    assert snapshot.idempotency_key == snapshot.plan.idempotency_key
    assert snapshot.state is ExecutionState.COMPLETED
    assert snapshot.mode is ExecutionMode.DRY_RUN
    assert [step.step_type for step in snapshot.plan.steps] == [
        ExecutionStepType.EXISTING_PURCHASE_ORDER,
        ExecutionStepType.VENDOR_BILL,
        ExecutionStepType.CUSTOMER_RECHARGE,
        ExecutionStepType.INTERNAL_COST,
    ]
    assert {step.last_result.status for step in snapshot.steps if step.last_result is not None} == {
        ExecutionStepStatus.DRY_RUN_OK
    }
    history = runtime_repository.history(execution_id=result.execution_id)
    assert [event.sequence for event in history.events] == list(range(1, len(history.events) + 1))
    assert history.events[0].event_type is ExecutionEventType.EXECUTION_CREATED
    assert history.events[-1].event_type is ExecutionEventType.EXECUTION_COMPLETED


def test_same_accepted_decision_produces_same_execution_id(session: Session) -> None:
    _submit_select_workflow(SqlAlchemyReviewRepository(session), review_id="review-1", company_id=7)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    use_case = _use_case(session, runtime_repository=runtime_repository)

    first = use_case.execute(_command())
    second = use_case.execute(_command())

    assert first.execution_id == second.execution_id


def test_execution_id_fallback_is_deterministic_when_decision_id_is_absent() -> None:
    decision = _accepted_decision(decision_id=None)
    command = _command()

    first = accepted_decision_use_cases._execution_request(command, decision=decision)  # noqa: SLF001
    second = accepted_decision_use_cases._execution_request(command, decision=decision)  # noqa: SLF001

    assert first.execution_id == second.execution_id
    assert first.idempotency_key is None


def test_same_canonical_plan_produces_same_plan_idempotency_key() -> None:
    planner = ExecutionPlanner()
    first = planner.plan(_request_from_decision(_accepted_decision()))
    second = planner.plan(_request_from_decision(_accepted_decision(), execution_id="different-execution-id"))

    assert first.execution_id != second.execution_id
    assert first.idempotency_key == second.idempotency_key


def test_changed_allocation_plan_changes_canonical_execution_idempotency() -> None:
    planner = ExecutionPlanner()
    first = planner.plan(_request_from_decision(_accepted_decision(allocations=_allocation_set())))
    changed = planner.plan(_request_from_decision(_accepted_decision(allocations=_allocation_set(changed=True))))

    assert _plan_step_identity(first) != _plan_step_identity(changed)
    assert first.idempotency_key != changed.idempotency_key


def test_repeated_dry_run_loads_completed_runtime_and_does_not_reexecute_steps(session: Session) -> None:
    _submit_select_workflow(SqlAlchemyReviewRepository(session), review_id="review-1", company_id=7)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    strategy = RecordingStrategy(supported_step_types=tuple(ExecutionStepType))
    use_case = _use_case(session, runtime_repository=runtime_repository, strategies=(strategy,))

    first = use_case.execute(_command())
    first_calls = strategy.calls
    second = use_case.execute(_command())

    assert second.execution_id == first.execution_id
    assert strategy.calls == first_calls
    assert session.query(WorkflowExecution).count() == 1
    history = runtime_repository.history(execution_id=first.execution_id or "")
    assert session.query(WorkflowExecutionEvent).count() == len(history.events)
    assert [event.event_type for event in history.events].count(ExecutionEventType.EXECUTION_CREATED) == 1
    assert [event.event_type for event in history.events].count(ExecutionEventType.PLANNING_COMPLETED) == 1


def test_partial_runtime_recovers_and_does_not_replay_completed_steps(session: Session) -> None:
    _submit_select_workflow(SqlAlchemyReviewRepository(session), review_id="review-1", company_id=7)
    runtime_repository = SqlAlchemyExecutionRuntimeRepository(session)
    review_repository = SqlAlchemyReviewRepository(session)
    decision = review_repository.get_accepted_decision(review_id="review-1", company_id=7, decision_version=2)
    request = _request_from_decision(decision)
    plan = ExecutionPlanner().plan(request)
    runtime = ExecutionRuntimeService(
        runtime_repository=runtime_repository,
        event_repository=runtime_repository,
    ).create_or_load(plan=plan, retry_policy=ExecutionRetryPolicy.never())
    partial = _persist_first_step_completed(runtime_repository, runtime.snapshot)
    strategy = RecordingStrategy(supported_step_types=tuple(ExecutionStepType))
    use_case = _use_case(session, runtime_repository=runtime_repository, strategies=(strategy,))

    result = use_case.execute(_command())

    assert result.execution_id == partial.execution_id
    assert result.runtime_state is ExecutionState.COMPLETED
    assert partial.steps[0].step_key not in strategy.calls
    assert tuple(strategy.calls) == tuple(step.step_key for step in partial.steps[1:])
    restored = runtime_repository.get_snapshot(execution_id=partial.execution_id)
    assert restored is not None
    assert restored.checkpoint.completed_step_keys == tuple(step.step_key for step in restored.steps)
    history = runtime_repository.history(execution_id=partial.execution_id)
    assert [event.sequence for event in history.events] == list(range(1, len(history.events) + 1))


def test_execute_without_approval_is_rejected_before_runtime_creation(session: Session) -> None:
    _submit_select_workflow(SqlAlchemyReviewRepository(session), review_id="review-1", company_id=7)

    with pytest.raises(ExecutionApprovalError):
        _use_case(session).execute(
            RunAcceptedDecisionExecutionCommand(
                review_id="review-1",
                company_id=7,
                decision_version=2,
                mode=ExecutionMode.EXECUTE,
            )
        )

    assert session.query(WorkflowExecution).count() == 0
    assert session.query(WorkflowExecutionEvent).count() == 0


def test_not_found_decision_returns_not_found_without_runtime(session: Session) -> None:
    result = _use_case(session).execute(_command())

    assert result.status is AcceptedDecisionExecutionStatus.NOT_FOUND
    assert result.execution_id is None
    assert session.query(WorkflowExecution).count() == 0


def test_no_sqlalchemy_or_provider_leaks_into_application_execution_layer() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application/execution").rglob("*.py"))

    assert "sqlalchemy" not in source.lower()
    assert "app.models" not in source
    assert "app.db" not in source
    assert "connectors" not in source
    assert "OdooVendorBillWriter" not in source
    assert "AccountMoveRepository" not in source
    assert "app.erp" not in source
    assert "requests." not in source
    assert "httpx" not in source


class RecordingStrategy:
    name = "recording_no_write"

    def __init__(self, *, supported_step_types: tuple[ExecutionStepType, ...]) -> None:
        self.supported_step_types = supported_step_types
        self.calls: tuple[str, ...] = ()

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        self.calls = (*self.calls, request.step.step_key)
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.DRY_RUN_OK,
            dry_run=True,
            message="Dry run completed. No ERP write was performed.",
        )


def _use_case(
    session: Session,
    *,
    runtime_repository: SqlAlchemyExecutionRuntimeRepository | None = None,
    strategies: tuple[ExecutionStrategy, ...] | None = None,
) -> RunAcceptedDecisionExecutionUseCase:
    repository = runtime_repository or SqlAlchemyExecutionRuntimeRepository(session)
    return RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=SqlAlchemyReviewRepository(session),
        execution_planner=ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(runtime_repository=repository, event_repository=repository),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=repository,
            event_repository=repository,
            strategy_resolver=foundation_no_write_strategy_resolver()
            if strategies is None
            else ExecutionStrategyResolver(strategies),
        ),
        runtime_repository=repository,
        retry_policy_resolver=StaticRetryPolicyResolver(),
    )


def _command() -> RunAcceptedDecisionExecutionCommand:
    return RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=ExecutionMode.DRY_RUN,
    )


def _submit_select_workflow(repository: SqlAlchemyReviewRepository, *, review_id: str, company_id: int) -> None:
    repository.create_review_item(_review_item(review_id), company_id=company_id, idempotency_key=f"item:{review_id}")
    repository.submit_review_decision_with_execution_evidence(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=company_id,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            business_context_allocations=_allocation_set(),
            decided_by="finance.user",
            idempotency_key=f"decision:{review_id}",
        ),
        _source_evidence(review_id=review_id, company_id=company_id),
    )


def _submit_dismiss(repository: SqlAlchemyReviewRepository, *, review_id: str, company_id: int) -> None:
    repository.create_review_item(_review_item(review_id), company_id=company_id, idempotency_key=f"item:{review_id}")
    repository.submit_review_decision(
        ReviewDecisionCommand(
            review_id=review_id,
            company_id=company_id,
            expected_version=1,
            decision=ReviewDecisionType.DISMISS,
            decided_by="finance.user",
            idempotency_key=f"decision:{review_id}",
        )
    )


def _review_item(review_id: str) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=None,
        currency="TRY",
        total_amount=Decimal("100.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
        warnings=(),
        version=1,
    )


def _source_evidence(*, review_id: str, company_id: int) -> ExecutionSourceInvoice:
    source_invoice_id = f"invoice-{review_id}"
    return ExecutionSourceInvoice(
        review_id=review_id,
        company_id=company_id,
        decision_version=2,
        source_invoice_id=source_invoice_id,
        invoice=InternalInvoice(
            header=Header(invoice_number="INV-1", invoice_uuid=source_invoice_id, ettn=source_invoice_id),
            supplier=Party(name="Supplier Display", tax_number="1234567890"),
            customer=Party(name="Customer", tax_number="0987654321"),
            totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        ),
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=50,
            matched_by="tax_number",
            reason="Matched by supplier tax number.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(),
        tax_match=InvoiceTaxMappingResult(),
    )


def _insert_review_item(session: Session, *, review_id: str, company_id: int) -> None:
    session.add(
        WorkbenchReviewItem(
            review_id=review_id,
            company_id=company_id,
            invoice_id=f"invoice-{review_id}",
            invoice_number="INV-1",
            supplier_tax_number="1234567890",
            supplier_name="Supplier Display",
            invoice_date=None,
            currency="TRY",
            total_amount=Decimal("100.00"),
            workflow=WorkflowType.MANUAL_REVIEW.value,
            status=ReviewStatus.DECISION_SUBMITTED.value,
            review_reasons=[],
            warnings=[],
            version=2,
            idempotency_key=f"item:{review_id}",
        )
    )
    session.flush()


def _allocation_set(*, changed: bool = False) -> BusinessContextAllocationSet:
    allocations = (
        BusinessContextAllocation(
            allocation_key="PO-CHANGED" if changed else "PO",
            allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
            amount=Decimal("25.00"),
            currency="TRY",
            purchase_order_id=501,
        ),
        BusinessContextAllocation(
            allocation_key="RECHARGE",
            allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
            amount=Decimal("25.00"),
            currency="TRY",
            recharge_partner_id=701,
        ),
        BusinessContextAllocation(
            allocation_key="INTERNAL",
            allocation_type=BusinessContextAllocationType.INTERNAL_COST,
            amount=Decimal("50.00"),
            currency="TRY",
        ),
    )
    return BusinessContextAllocationSet(
        allocations=allocations,
        completeness=AllocationCompleteness.COMPLETE,
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )


def _accepted_decision(
    *,
    decision_id: str | None = "decision-1",
    allocations: BusinessContextAllocationSet | None = None,
) -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id=decision_id,
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=allocations or _allocation_set(),
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _request_from_decision(
    decision: AcceptedReviewDecision,
    *,
    execution_id: str = "partial-runtime",
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        review_id=decision.review_id,
        company_id=decision.company_id,
        decision_version=decision.decision_version,
        decision_id=decision.decision_id,
        idempotency_key=None,
        mode=ExecutionMode.DRY_RUN,
        selected_workflow=decision.selected_workflow,
        business_context_allocations=decision.business_context_allocations,
    )


def _plan_step_identity(plan: ExecutionPlan) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((step.step_type.value, step.allocation_keys) for step in plan.steps)


def _persist_first_step_completed(
    repository: SqlAlchemyExecutionRuntimeRepository,
    snapshot: ExecutionSnapshot,
) -> ExecutionSnapshot:
    running = repository.persist_transition(
        snapshot=_replace_snapshot(snapshot, state=ExecutionState.RUNNING),
        events=(
            ExecutionEventDraft(
                event_id="event-started",
                execution_id=snapshot.execution_id,
                event_type=ExecutionEventType.EXECUTION_STARTED,
                state=ExecutionState.RUNNING,
            ),
        ),
        expected_runtime_version=snapshot.runtime_version,
    )
    first_step = running.steps[0]
    started = repository.persist_transition(
        snapshot=_replace_step(running, first_step, state=ExecutionRuntimeStepState.RUNNING),
        events=(
            ExecutionEventDraft(
                event_id="event-first-started",
                execution_id=running.execution_id,
                event_type=ExecutionEventType.STEP_STARTED,
                state=ExecutionState.RUNNING,
                step_key=first_step.step_key,
                step_type=first_step.step_type,
            ),
        ),
        expected_runtime_version=running.runtime_version,
    )
    result = ExecutionStepResult(
        step_key=first_step.step_key,
        step_type=first_step.step_type,
        status=ExecutionStepStatus.DRY_RUN_OK,
        dry_run=True,
    )
    return repository.persist_transition(
        snapshot=_replace_step(
            started,
            first_step,
            state=ExecutionRuntimeStepState.COMPLETED,
            result=result,
            completed_step_keys=(first_step.step_key,),
            current_step_key=started.steps[1].step_key,
        ),
        events=(
            ExecutionEventDraft(
                event_id="event-first-completed",
                execution_id=started.execution_id,
                event_type=ExecutionEventType.STEP_COMPLETED,
                state=ExecutionState.RUNNING,
                step_key=first_step.step_key,
                step_type=first_step.step_type,
                data={
                    "status": result.status.value,
                    "dry_run": result.dry_run,
                    "error_code": result.error_code,
                    "message": result.message,
                },
            ),
        ),
        expected_runtime_version=started.runtime_version,
    )


def _replace_snapshot(snapshot: ExecutionSnapshot, *, state: ExecutionState) -> ExecutionSnapshot:
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
        checkpoint=snapshot.checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=snapshot.failure,
    )


def _replace_step(
    snapshot: ExecutionSnapshot,
    step: ExecutionRuntimeStep,
    *,
    state: ExecutionRuntimeStepState,
    result: ExecutionStepResult | None = None,
    completed_step_keys: tuple[str, ...] = (),
    current_step_key: str | None = None,
) -> ExecutionSnapshot:
    steps = tuple(
        ExecutionRuntimeStep(
            step_key=candidate.step_key,
            step_type=candidate.step_type,
            sequence=candidate.sequence,
            state=state if candidate.step_key == step.step_key else candidate.state,
            allocation_keys=candidate.allocation_keys,
            retry_count=candidate.retry_count,
            last_result=result if candidate.step_key == step.step_key else candidate.last_result,
        )
        for candidate in snapshot.steps
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
        checkpoint=ExecutionCheckpoint(
            execution_id=snapshot.execution_id,
            completed_step_keys=completed_step_keys or snapshot.checkpoint.completed_step_keys,
            failed_step_key=None,
            current_step_key=current_step_key,
            retry_count=snapshot.checkpoint.retry_count,
            last_event_id=snapshot.checkpoint.last_event_id,
        ),
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=snapshot.failure,
    )


def _engine() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    return engine
