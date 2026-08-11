from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import app.application.execution.accepted_decision_use_cases as accepted_decision_use_cases
import app.application.execution.planner as execution_planner
import app.application.workbench.decision_use_cases as decision_use_cases
from app.application.execution import (
    AcceptedReviewDecision,
    ExecutionApproval,
    ExecutionMode,
    ExecutionPlanner,
    ExecutionPreflightPolicy,
    ExecutionRequest,
    ExecutionStepRequest,
    ExecutionStepType,
    ExecutionUnsupportedStepError,
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    customer_invoice_write_idempotency_key,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewExecutionBillingEvidence,
    ReviewItem,
    ReviewStatus,
    SubmitReviewDecisionUseCase,
    TaxResolution,
)
from app.application.workbench.exceptions import (
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewNotFoundError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing.dto import CustomerInvoiceBillingInstruction, CustomerInvoiceBillingLine
from app.db.base import Base
from app.models.execution_customer_billing_evidence import ExecutionCustomerBillingEvidence
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence import (
    SqlAlchemyAcceptedBillingEvidenceReader,
    SqlAlchemyReviewBillingEvidenceReader,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewRepository,
)
from app.persistence.review_billing_evidence_reader import serialize_billing_instruction_payload
from tests.unit.test_workbench_review_execution_evidence import _evidence as _source_evidence


def test_submit_decision_pins_stage_two_billing_evidence_atomically(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))

    acknowledgement = _submit_use_case(session, repository).execute(_command(_allocation("ALLOC-A", 501)))

    record = session.scalar(select(ExecutionCustomerBillingEvidence))
    assert acknowledgement.version == 2
    assert record is not None
    assert record.review_id == "review-1"
    assert record.company_id == 7
    assert record.decision_version == 2
    assert record.billing_key == "BILL-A"
    assert record.billing_instruction["lines"][0]["unit_price"] == "150.00"


def test_stage_two_insert_failure_rolls_back_accepted_decision(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))
    original_add_all = session.add_all

    def fail_on_stage_two(records: object) -> None:
        records_tuple = tuple(records)  # type: ignore[arg-type]
        if records_tuple and isinstance(records_tuple[0], ExecutionCustomerBillingEvidence):
            raise SQLAlchemyError("stage-two-billing-failure")
        original_add_all(records_tuple)

    monkeypatch.setattr(session, "add_all", fail_on_stage_two)

    with pytest.raises(ReviewDecisionError):
        _submit_use_case(session, repository).execute(_command(_allocation("ALLOC-A", 501)))

    assert session.query(WorkbenchReviewDecision).count() == 0
    assert session.query(ExecutionCustomerBillingEvidence).count() == 0


def test_missing_stage_one_billing_evidence_prevents_decision_persistence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_source_evidence(),
    )

    with pytest.raises(ReviewNotFoundError):
        _submit_use_case(session, repository).execute(_command(_allocation("ALLOC-A", 501)))

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_stage_one_billing_coverage_must_be_exact(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))

    with pytest.raises(ReviewDecisionError):
        _submit_use_case(session, repository).execute(
            _command(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 502))
        )

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_stage_one_billing_cannot_cover_existing_invoice_allocation(session: Session) -> None:
    repository = _create_stage_one_review(
        session,
        _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
        _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
    )

    with pytest.raises(ReviewDecisionError):
        _submit_use_case(session, repository).execute(
            _command(_allocation("ALLOC-A", 501, customer_invoice_id=9001), _allocation("ALLOC-B", 502))
        )

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_stage_one_billing_customer_must_match_recharge_partner(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=999))

    with pytest.raises(ReviewDecisionError):
        _submit_use_case(session, repository).execute(_command(_allocation("ALLOC-A", 501)))

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_stage_two_replay_exact_same_set_is_idempotent(session: Session) -> None:
    repository = _create_stage_one_review(
        session,
        _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
        _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
    )
    use_case = _submit_use_case(session, repository)
    command = _command(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 502))

    first = use_case.execute(command)
    second = use_case.execute(command)

    assert second == first
    assert session.query(WorkbenchReviewDecision).count() == 1
    assert session.query(ExecutionCustomerBillingEvidence).count() == 2


def test_stage_two_replay_ordering_difference_is_idempotent(session: Session) -> None:
    repository = _create_stage_one_review(
        session,
        _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
        _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
    )
    command = _command(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 502))
    first = _submit_use_case(session, repository).execute(command)
    source = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id="review-1",
        company_id=7,
        expected_version=1,
    )

    second = repository.submit_review_decision_with_execution_and_billing_evidence(
        command,
        source,
        (
            _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
            _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
        ),
    )

    assert second == first
    assert session.query(ExecutionCustomerBillingEvidence).count() == 2


def test_stage_two_replay_removed_billing_key_conflicts(session: Session) -> None:
    repository = _create_stage_one_review(
        session,
        _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
        _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
    )
    use_case = _submit_use_case(session, repository)
    command = _command(_allocation("ALLOC-A", 501), _allocation("ALLOC-B", 502))
    use_case.execute(command)
    removed = session.scalar(
        select(ExecutionCustomerBillingEvidence).where(ExecutionCustomerBillingEvidence.billing_key == "BILL-B")
    )
    assert removed is not None
    session.delete(removed)
    session.flush()

    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        use_case.execute(command)

    assert session.query(ExecutionCustomerBillingEvidence).count() == 1


def test_stage_two_replay_added_billing_key_conflicts(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))
    use_case = _submit_use_case(session, repository)
    command = _command(_allocation("ALLOC-A", 501))
    use_case.execute(command)
    decision = session.scalar(select(WorkbenchReviewDecision))
    assert decision is not None
    session.add(
        ExecutionCustomerBillingEvidence(
            decision_id=decision.decision_id,
            review_id=decision.review_id,
            company_id=decision.company_id,
            decision_version=decision.review_version_after,
            billing_key="BILL-EXTRA",
            schema_version=1,
            billing_instruction=serialize_billing_instruction_payload(
                _billing_instruction("BILL-EXTRA", "ALLOC-A", customer_id=501)
            ),
        )
    )
    session.flush()

    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        use_case.execute(command)

    assert session.query(ExecutionCustomerBillingEvidence).count() == 2


def test_stage_two_replay_changed_payload_conflicts(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))
    use_case = _submit_use_case(session, repository)
    command = _command(_allocation("ALLOC-A", 501))
    use_case.execute(command)
    stage_two = session.scalar(select(ExecutionCustomerBillingEvidence))
    assert stage_two is not None
    stage_two.billing_instruction = dict(
        stage_two.billing_instruction,
        customer_id=502,
    )
    session.flush()

    with pytest.raises(ReviewDecisionIdempotencyConflictError):
        use_case.execute(command)

    assert session.query(ExecutionCustomerBillingEvidence).count() == 1


def test_accepted_billing_reader_reads_stage_two_only(session: Session) -> None:
    repository = _create_stage_one_review(session, _billing_instruction("BILL-A", "ALLOC-A", customer_id=501))
    _submit_use_case(session, repository).execute(_command(_allocation("ALLOC-A", 501)))
    stage_one = session.scalar(select(WorkbenchReviewBillingEvidence))
    assert stage_one is not None
    stage_one.billing_instruction = serialize_billing_instruction_payload(
        _billing_instruction("BILL-A", "ALLOC-A", customer_id=501, unit_price=Decimal("999.00"))
    )
    session.flush()

    instructions = SqlAlchemyAcceptedBillingEvidenceReader(session).get_billing_instructions(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id=session.scalar(select(WorkbenchReviewDecision)).decision_id,  # type: ignore[union-attr]
    )

    assert instructions[0].lines[0].unit_price == Decimal("150.00")


def test_planner_builds_one_creation_step_per_accepted_billing_instruction() -> None:
    request = _execution_request(
        _allocation("ALLOC-A", 501),
        _allocation("ALLOC-B", 502),
        instructions=(
            _billing_instruction("BILL-A", "ALLOC-A", customer_id=501),
            _billing_instruction("BILL-B", "ALLOC-B", customer_id=502),
        ),
    )

    plan = ExecutionPlanner().plan(request)
    creation_steps = tuple(step for step in plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)

    assert tuple(step.step_key for step in creation_steps) == (
        "review-1:2:customer_invoice_create:BILL-A",
        "review-1:2:customer_invoice_create:BILL-B",
    )
    assert all(step.execute_supported for step in creation_steps)
    assert creation_steps[0].customer_invoice_billing_instruction.billing_key == "BILL-A"  # type: ignore[union-attr]


def test_missing_accepted_billing_evidence_keeps_creation_step_non_execute_capable() -> None:
    plan = ExecutionPlanner().plan(_execution_request(_allocation("ALLOC-A", 501), instructions=()))

    creation_step = next(step for step in plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)

    assert creation_step.execute_supported is False
    assert creation_step.customer_invoice_billing_instruction is None


def test_full_plan_preflight_blocks_before_any_runtime_or_writer_when_one_billing_instruction_invalid() -> None:
    with pytest.raises(ExecutionUnsupportedStepError):
        ExecutionPreflightPolicy(production_execution_enabled=True).ensure_execute_allowed(
            plan=ExecutionPlanner().plan(_execution_request(_allocation("ALLOC-A", 501), instructions=())),
            approval=ExecutionApproval(approved_by="finance.user"),
        )


def test_accepted_execution_preflight_blocks_missing_billing_before_runtime() -> None:
    use_case = RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=_AcceptedDecisionReader(
            AcceptedReviewDecision(
                review_id="review-1",
                company_id=7,
                decision_version=2,
                decision_id="decision-1",
                selected_workflow=WorkflowType.VENDOR_BILL,
                business_context_allocations=BusinessContextAllocationSet(
                    allocations=(_allocation("ALLOC-A", 501),),
                    completeness=AllocationCompleteness.PARTIAL,
                    invoice_total=Decimal("120.00"),
                    currency="TRY",
                ),
            )
        ),
        execution_planner=ExecutionPlanner(),
        runtime_service=_ExplodingRuntimeService(),
        runtime_coordinator=_ExplodingRuntimeCoordinator(),
        runtime_repository=_ExplodingRuntimeRepository(),
        retry_policy_resolver=StaticRetryPolicyResolver(),
        execution_preflight=ExecutionPreflightPolicy(production_execution_enabled=True),
        accepted_billing_evidence_reader=_MissingAcceptedBillingEvidenceReader(),
    )

    with pytest.raises(ExecutionUnsupportedStepError):
        use_case.execute(
            RunAcceptedDecisionExecutionCommand(
                review_id="review-1",
                company_id=7,
                decision_version=2,
                mode=ExecutionMode.EXECUTE,
                approval=ExecutionApproval(approved_by="finance.user"),
            )
        )


def test_mixed_existing_and_creation_recharge_steps_are_separate() -> None:
    plan = ExecutionPlanner().plan(
        _execution_request(
            _allocation("ALLOC-A", 501, customer_invoice_id=9001),
            _allocation("ALLOC-B", 502),
            instructions=(_billing_instruction("BILL-B", "ALLOC-B", customer_id=502),),
        )
    )
    recharge_steps = tuple(step for step in plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)

    assert len(recharge_steps) == 2
    assert recharge_steps[0].writer_required is False
    assert recharge_steps[1].writer_required is True
    assert recharge_steps[1].execute_supported is True


def test_customer_invoice_writer_idempotency_includes_decision_and_billing_identity() -> None:
    plan = ExecutionPlanner().plan(
        _execution_request(
            _allocation("ALLOC-A", 501),
            instructions=(_billing_instruction("BILL-A", "ALLOC-A", customer_id=501),),
        )
    )
    step = next(step for step in plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)

    first = customer_invoice_write_idempotency_key(
        ExecutionStepRequest(
            execution_id=plan.execution_id,
            review_id=plan.review_id,
            company_id=plan.company_id,
            decision_version=plan.decision_version,
            decision_id=plan.decision_id,
            mode=plan.mode,
            step=step,
        )
    )
    changed_plan = ExecutionPlanner().plan(
        _execution_request(
            _allocation("ALLOC-A", 501),
            instructions=(_billing_instruction("BILL-CHANGED", "ALLOC-A", customer_id=501),),
        )
    )
    changed_step = next(step for step in changed_plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)
    second = customer_invoice_write_idempotency_key(
        ExecutionStepRequest(
            execution_id=changed_plan.execution_id,
            review_id=changed_plan.review_id,
            company_id=changed_plan.company_id,
            decision_version=changed_plan.decision_version,
            decision_id=changed_plan.decision_id,
            mode=changed_plan.mode,
            step=changed_step,
        )
    )

    assert first != second


def test_accepted_billing_reader_does_not_read_stage_one_or_provider_boundaries() -> None:
    source = inspect.getsource(SqlAlchemyAcceptedBillingEvidenceReader).lower()

    assert "workbenchreviewbillingevidence" not in source
    assert "workbench_review_billing_evidence" not in source
    for forbidden in ("odoo", "uyumsoft", "display", "fuzzy", "rematch", "provider"):
        assert forbidden not in source


def test_application_customer_invoice_production_wiring_has_no_sqlalchemy_or_provider_leaks() -> None:
    application_source = "\n".join(
        (
            inspect.getsource(decision_use_cases),
            inspect.getsource(accepted_decision_use_cases),
            inspect.getsource(execution_planner),
        )
    ).lower()

    for forbidden in ("sqlalchemy", "app.persistence", "app.models", "app.connectors", "odoo", "uyumsoft"):
        assert forbidden not in application_source


def _submit_use_case(session: Session, repository: SqlAlchemyReviewRepository) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=repository,
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        billing_evidence_reader=SqlAlchemyReviewBillingEvidenceReader(session),
    )


def _create_stage_one_review(
    session: Session,
    *instructions: CustomerInvoiceBillingInstruction,
) -> SqlAlchemyReviewRepository:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_and_billing_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_source_evidence(),
        billing_evidence=tuple(
            ReviewExecutionBillingEvidence(
                review_id="review-1",
                company_id=7,
                review_version=1,
                billing_instruction=instruction,
            )
            for instruction in instructions
        ),
    )
    return repository


def _command(*allocations: BusinessContextAllocation) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        selected_partner_id=501,
        line_resolutions=(LineResolution(line_number="1", selected_product_id=701),),
        tax_resolutions=(TaxResolution(line_number="1", tax_index=0, selected_tax_id=801),),
        business_context_allocations=BusinessContextAllocationSet(
            allocations=allocations,
            completeness=AllocationCompleteness.PARTIAL,
            invoice_total=Decimal("240.00"),
            currency="TRY",
        ),
        decided_by="finance.user",
        idempotency_key="decision-key-1",
    )


def _execution_request(
    *allocations: BusinessContextAllocation,
    instructions: tuple[CustomerInvoiceBillingInstruction, ...],
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id="decision-1",
        idempotency_key=None,
        mode=ExecutionMode.EXECUTE,
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=BusinessContextAllocationSet(
            allocations=allocations,
            completeness=AllocationCompleteness.PARTIAL,
            invoice_total=Decimal("240.00"),
            currency="TRY",
        ),
        accepted_billing_instructions=instructions,
    )


def _allocation(
    allocation_key: str,
    recharge_partner_id: int,
    *,
    customer_invoice_id: int | None = None,
) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=allocation_key,
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        source_line_number="1",
        amount=Decimal("120.00"),
        currency="TRY",
        recharge_partner_id=recharge_partner_id,
        customer_invoice_id=customer_invoice_id,
    )


def _billing_instruction(
    billing_key: str,
    allocation_key: str,
    *,
    customer_id: int,
    currency: str = "TRY",
    unit_price: Decimal = Decimal("150.00"),
) -> CustomerInvoiceBillingInstruction:
    return CustomerInvoiceBillingInstruction(
        billing_key=billing_key,
        customer_id=customer_id,
        currency=currency,
        lines=(
            CustomerInvoiceBillingLine(
                allocation_key=allocation_key,
                product_id=901,
                description=f"Recharge {allocation_key}",
                quantity=Decimal("1.000"),
                unit_price=unit_price,
                sales_tax_ids=(1901,),
            ),
        ),
    )


class _AcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision) -> None:
        self._decision = decision

    def get_accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> AcceptedReviewDecision:
        assert (review_id, company_id, decision_version) == ("review-1", 7, 2)
        return self._decision


class _MissingAcceptedBillingEvidenceReader:
    def get_billing_instructions(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        decision_id: str | None,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        assert (review_id, company_id, decision_version, decision_id) == ("review-1", 7, 2, "decision-1")
        raise ReviewNotFoundError("missing accepted billing evidence")


class _ExplodingRuntimeService:
    def create_or_load(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("runtime must not be created when full-plan preflight fails")


class _ExplodingRuntimeCoordinator:
    def execute(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("coordinator must not run when full-plan preflight fails")


class _ExplodingRuntimeRepository:
    def get_snapshot(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("repository must not be read when full-plan preflight fails")


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="ETTN-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("240.00"),
        workflow=WorkflowType.VENDOR_BILL,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Review customer recharge.",
                line_number="1",
                source="product_matching",
            ),
        ),
        version=1,
    )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewBillingEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            ExecutionCustomerBillingEvidence.__table__,
            WorkflowExecution.__table__,
            WorkflowExecutionStep.__table__,
            WorkflowExecutionEvent.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
