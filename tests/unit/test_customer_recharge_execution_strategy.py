from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.application.execution as execution_exports
from app.application.commands import VendorBillWriteCommand
from app.application.dto import VendorBillWriteResult
from app.application.execution import (
    AcceptedDecisionExecutionStatus,
    AcceptedReviewDecision,
    CustomerRechargeExecutionStrategy,
    CustomerRechargeInvoiceCreationRequiredError,
    ExecutionApproval,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionPlanner,
    ExecutionPreflightPolicy,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeService,
    ExecutionSourceInvoice,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepType,
    ExecutionStrategyResolver,
    ExecutionUnsupportedStepError,
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    VendorBillExecutionStrategy,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    ReviewDecisionType,
)
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.models.workflow_execution import WorkflowExecution, WorkflowExecutionEvent, WorkflowExecutionStep
from app.persistence import SqlAlchemyExecutionRuntimeRepository
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    with factory() as db_session:
        Base.metadata.create_all(
            db_session.get_bind(),
            tables=[
                WorkflowExecution.__table__,
                WorkflowExecutionStep.__table__,
                WorkflowExecutionEvent.__table__,
            ],
        )
        yield db_session


def test_customer_recharge_strategy_supports_only_customer_recharge() -> None:
    strategy = CustomerRechargeExecutionStrategy()

    assert strategy.supported_step_types == (ExecutionStepType.CUSTOMER_RECHARGE,)
    assert strategy.supports_mode(ExecutionMode.DRY_RUN)
    assert strategy.supports_mode(ExecutionMode.EXECUTE)
    with pytest.raises(ExecutionUnsupportedStepError):
        strategy.execute(_step_request(step_type=ExecutionStepType.VENDOR_BILL))


def test_dry_run_performs_no_erp_write_and_returns_existing_invoice_preview_artifact() -> None:
    result = CustomerRechargeExecutionStrategy().execute(_step_request(mode=ExecutionMode.DRY_RUN))

    assert result.dry_run is True
    assert result.produced_artifacts[0].artifact_type is ExecutionArtifactType.CUSTOMER_INVOICE
    assert result.produced_artifacts[0].artifact_id == "7001"
    assert result.produced_artifacts[0].external_identity == "account.move:7001"
    assert result.produced_artifacts[0].created is False


def test_execute_with_one_existing_customer_invoice_completes_with_no_created_artifact() -> None:
    result = CustomerRechargeExecutionStrategy().execute(_step_request(mode=ExecutionMode.EXECUTE))

    assert result.dry_run is False
    assert result.produced_artifacts[0].artifact_type is ExecutionArtifactType.CUSTOMER_INVOICE
    assert result.produced_artifacts[0].artifact_id == "7001"
    assert result.produced_artifacts[0].external_identity == "account.move:7001"
    assert result.produced_artifacts[0].created is False


def test_duplicate_customer_invoice_references_produce_one_artifact() -> None:
    result = CustomerRechargeExecutionStrategy().execute(
        _step_request(
            mode=ExecutionMode.EXECUTE,
            allocations=(
                _allocation("A", customer_invoice_id=7001),
                _allocation("B", customer_invoice_id=7001),
            ),
        )
    )

    assert [artifact.artifact_id for artifact in result.produced_artifacts] == ["7001"]


def test_multiple_customer_invoices_produce_deterministically_sorted_artifacts() -> None:
    result = CustomerRechargeExecutionStrategy().execute(
        _step_request(
            mode=ExecutionMode.EXECUTE,
            allocations=(
                _allocation("B", customer_invoice_id=7002),
                _allocation("A", customer_invoice_id=7001),
            ),
        )
    )

    assert [artifact.artifact_id for artifact in result.produced_artifacts] == ["7001", "7002"]
    assert [artifact.created for artifact in result.produced_artifacts] == [False, False]


def test_execute_without_customer_invoice_id_requires_future_creation_strategy() -> None:
    with pytest.raises(CustomerRechargeInvoiceCreationRequiredError):
        CustomerRechargeExecutionStrategy().execute(
            _step_request(mode=ExecutionMode.EXECUTE, allocations=(_allocation("A", customer_invoice_id=None),))
        )


def test_planner_marks_customer_recharge_creation_fail_closed_without_billing_instruction() -> None:
    planner = ExecutionPlanner()

    valid = planner.plan(_request(allocations=_allocation_set(_allocation("A", customer_invoice_id=7001))))
    missing = planner.plan(_request(allocations=_allocation_set(_allocation("A", customer_invoice_id=None))))

    assert _step(valid, ExecutionStepType.CUSTOMER_RECHARGE).execute_supported is True
    assert _step(valid, ExecutionStepType.CUSTOMER_RECHARGE).writer_required is False
    assert _step(missing, ExecutionStepType.CUSTOMER_RECHARGE).execute_supported is False
    assert _step(missing, ExecutionStepType.CUSTOMER_RECHARGE).writer_required is True
    assert _step(missing, ExecutionStepType.CUSTOMER_RECHARGE).customer_invoice_billing_instruction is None
    assert _step(valid, ExecutionStepType.CUSTOMER_RECHARGE).allocations[0].customer_invoice_id == 7001


def test_planner_keeps_multiple_customer_invoice_creation_allocations_as_separate_steps() -> None:
    plan = ExecutionPlanner().plan(
        _request(
            allocations=_allocation_set(
                _allocation("A", customer_invoice_id=None),
                _allocation("B", customer_invoice_id=None),
            )
        )
    )

    recharge_steps = tuple(step for step in plan.steps if step.step_type is ExecutionStepType.CUSTOMER_RECHARGE)

    assert [step.allocation_keys for step in recharge_steps] == [("A",), ("B",)]
    assert [step.writer_required for step in recharge_steps] == [True, True]
    assert [step.execute_supported for step in recharge_steps] == [False, False]


def test_no_write_customer_recharge_execute_does_not_require_writer_gate() -> None:
    plan = ExecutionPlanner().plan(_request(allocations=_allocation_set(_allocation("A", customer_invoice_id=7001))))
    gate = ExplodingWriteGate()

    ExecutionPreflightPolicy(
        production_execution_enabled=True,
        real_write_gate=gate,
    ).ensure_execute_allowed(plan=plan, approval=ExecutionApproval(approved_by="finance.lead"))

    assert gate.calls == 0


def test_completed_customer_recharge_runtime_is_not_replayed_or_duplicated(session: Session) -> None:
    strategy = CountingCustomerRechargeExecutionStrategy()
    use_case = _use_case(
        session,
        decision=_decision(allocations=_allocation_set(_allocation("A", customer_invoice_id=7001))),
        strategies=(strategy,),
    )

    first = use_case.execute(_command())
    second = use_case.execute(_command())

    assert second.execution_id == first.execution_id
    assert strategy.calls == 1
    snapshot = SqlAlchemyExecutionRuntimeRepository(session).get_snapshot(execution_id=second.execution_id or "")
    assert snapshot is not None
    assert len(snapshot.steps[0].last_result.produced_artifacts) == 1  # type: ignore[union-attr]


def test_one_customer_invoice_can_be_referenced_across_multiple_accepted_decisions(session: Session) -> None:
    strategy = CountingCustomerRechargeExecutionStrategy()
    first = _use_case(
        session,
        decision=_decision(review_id="review-1", allocations=_allocation_set(_allocation("A", 7001))),
        strategies=(strategy,),
    ).execute(_command(review_id="review-1"))
    second = _use_case(
        session,
        decision=_decision(review_id="review-2", allocations=_allocation_set(_allocation("B", 7001))),
        strategies=(strategy,),
    ).execute(_command(review_id="review-2"))

    assert first.execution_id != second.execution_id
    assert strategy.calls == 2
    assert session.query(WorkflowExecution).count() == 2


def test_vendor_bill_plus_existing_customer_recharge_full_plan_executes(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    decision = _decision(
        selected_workflow=WorkflowType.VENDOR_BILL,
        allocations=_allocation_set(_allocation("A", customer_invoice_id=7001)),
    )

    result = _use_case(
        session,
        decision=decision,
        strategies=(_vendor_bill_strategy(writer=writer), CustomerRechargeExecutionStrategy()),
    ).execute(_command())

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert writer.calls == 1


def test_vendor_bill_plus_missing_customer_invoice_fails_before_vendor_bill_writer(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    decision = _decision(
        selected_workflow=WorkflowType.VENDOR_BILL,
        allocations=_allocation_set(_allocation("A", customer_invoice_id=None)),
    )

    with pytest.raises(ExecutionUnsupportedStepError):
        _use_case(
            session,
            decision=decision,
            strategies=(_vendor_bill_strategy(writer=writer), CustomerRechargeExecutionStrategy()),
        ).execute(_command())

    assert writer.calls == 0
    assert session.query(WorkflowExecution).count() == 0
    assert session.query(WorkflowExecutionEvent).count() == 0


def test_customer_recharge_strategy_has_no_write_or_infrastructure_dependencies() -> None:
    source = Path("app/application/execution/customer_recharge_strategy.py").read_text(encoding="utf-8")

    assert "sqlalchemy" not in source.lower()
    assert "app.erp" not in source
    assert "odoo" not in source.lower()
    assert "create_account_move" not in source
    assert "account.move/create" not in source
    assert "write(" not in source
    assert "action_post" not in source
    assert "payment" not in source.lower()
    assert "reconciliation" not in source.lower()
    assert "fuzzy" not in source.lower()
    assert "openai" not in source.lower()


class CountingCustomerRechargeExecutionStrategy(CustomerRechargeExecutionStrategy):
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request: ExecutionStepRequest):
        self.calls += 1
        return super().execute(request)


class StaticAcceptedDecisionReader:
    def __init__(self, decision: AcceptedReviewDecision) -> None:
        self._decision = decision

    def get_accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> AcceptedReviewDecision:
        return self._decision


class StaticSourceInvoiceReader:
    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        return _source()


class RecordingVendorBillWriter:
    def __init__(self) -> None:
        self.calls = 0

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.calls += 1
        return VendorBillWriteResult(
            status="created" if not command.dry_run else "dry_run",
            idempotency_key=command.idempotency_key,
            external_id=None if command.dry_run else 9001,
            success=True,
            safe_message="ok",
        )


class ExplodingWriteGate:
    def __init__(self) -> None:
        self.calls = 0

    def ensure_real_write_allowed(self, *, approved_by: str | None) -> None:
        self.calls += 1
        raise AssertionError("Writer gate should not run for no-write Customer Recharge execution.")


def _vendor_bill_strategy(*, writer: RecordingVendorBillWriter) -> VendorBillExecutionStrategy:
    return VendorBillExecutionStrategy(
        source_invoice_reader=StaticSourceInvoiceReader(),
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=writer,
    )


def _use_case(
    session: Session,
    *,
    decision: AcceptedReviewDecision,
    strategies,
) -> RunAcceptedDecisionExecutionUseCase:
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    return RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=StaticAcceptedDecisionReader(decision),
        execution_planner=execution_exports.ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(runtime_repository=repository, event_repository=repository),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=repository,
            event_repository=repository,
            strategy_resolver=ExecutionStrategyResolver(strategies),
        ),
        runtime_repository=repository,
        retry_policy_resolver=StaticRetryPolicyResolver(ExecutionRetryPolicy.never()),
        execution_preflight=ExecutionPreflightPolicy(production_execution_enabled=True),
    )


def _command(*, review_id: str = "review-1") -> RunAcceptedDecisionExecutionCommand:
    return RunAcceptedDecisionExecutionCommand(
        review_id=review_id,
        company_id=7,
        decision_version=2,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="finance.lead"),
    )


def _step_request(
    *,
    mode: ExecutionMode = ExecutionMode.DRY_RUN,
    step_type: ExecutionStepType = ExecutionStepType.CUSTOMER_RECHARGE,
    allocations: tuple[BusinessContextAllocation, ...] | None = None,
) -> ExecutionStepRequest:
    allocations = allocations or (_allocation("A", customer_invoice_id=7001),)
    allocation_keys = tuple(sorted(allocation.allocation_key for allocation in allocations))
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        step=ExecutionStep(
            step_key=f"review-1:2:{step_type.value}:{'+'.join(allocation_keys) or 'workflow'}",
            step_type=step_type,
            allocation_keys=allocation_keys,
            sequence=1,
            execute_supported=all(allocation.customer_invoice_id is not None for allocation in allocations),
            allocations=tuple(sorted(allocations, key=lambda allocation: allocation.allocation_key)),
        ),
        approval=ExecutionApproval(approved_by="finance.lead") if mode is ExecutionMode.EXECUTE else None,
    )


def _request(*, allocations: BusinessContextAllocationSet):
    return execution_exports.ExecutionRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id="decision-1",
        idempotency_key=None,
        mode=ExecutionMode.EXECUTE,
        selected_workflow=None,
        business_context_allocations=allocations,
    )


def _decision(
    *,
    review_id: str = "review-1",
    selected_workflow: WorkflowType | None = None,
    allocations: BusinessContextAllocationSet,
) -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id=review_id,
        company_id=7,
        decision_version=2,
        decision_id=f"decision:{review_id}",
        selected_workflow=selected_workflow,
        business_context_allocations=allocations,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _allocation_set(*allocations: BusinessContextAllocation) -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=allocations,
        completeness=AllocationCompleteness.PARTIAL,
        invoice_total=Decimal("120.00"),
        currency="TRY",
    )


def _allocation(allocation_key: str, customer_invoice_id: int | None) -> BusinessContextAllocation:
    return BusinessContextAllocation(
        allocation_key=allocation_key,
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        amount=Decimal("60.00"),
        currency="TRY",
        recharge_partner_id=701,
        customer_invoice_id=customer_invoice_id,
    )


def _step(plan, step_type: ExecutionStepType) -> ExecutionStep:
    return next(step for step in plan.steps if step.step_type is step_type)


def _source() -> ExecutionSourceInvoice:
    invoice = _invoice()
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        source_invoice_id="ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=1001,
            matched_by="tax_number",
            reason="Unique supplier partner match by tax number.",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(
            line_results=(
                InvoiceProductLineResult(
                    line_number="1",
                    result=ProductMatchResult(
                        status=ProductMatchStatus.MATCHED,
                        line_number="1",
                        product_id=2001,
                        default_code="SKU-1",
                        barcode=None,
                        seller_item_code=None,
                        matched_by="default_code",
                        reason="matched",
                        candidate_count=1,
                        confidence=Decimal("1.00"),
                    ),
                ),
            )
        ),
        tax_match=InvoiceTaxMappingResult(
            line_results=(
                InvoiceTaxLineResult(
                    line_number="1",
                    tax_index=0,
                    result=TaxMatchResult(
                        status=TaxMatchStatus.MATCHED,
                        tax_id=3001,
                        company_id=7,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="rate",
                        confidence=Decimal("1.00"),
                        reason="matched",
                        candidate_count=1,
                    ),
                ),
            )
        ),
    )


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="ETTN-1",
            ettn="ETTN-1",
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="ICT", tax_number="9876543210"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Service",
                buyer_item_code="SKU-1",
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )
