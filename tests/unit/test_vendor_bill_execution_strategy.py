from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.application.execution as execution_exports
from app.application.commands import VendorBillWriteCommand
from app.application.dto import VendorBillWriteResult
from app.application.exceptions import ApplicationError
from app.application.execution import (
    AcceptedDecisionExecutionStatus,
    AcceptedReviewDecision,
    AcceptedReviewDecisionReader,
    ExecutionApproval,
    ExecutionApprovalError,
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionModeNotEnabledError,
    ExecutionPreflightPolicy,
    ExecutionRetryPolicy,
    ExecutionRuntimeCoordinator,
    ExecutionRuntimeService,
    ExecutionSourceInvoice,
    ExecutionSourceInvoiceNotFoundError,
    ExecutionState,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategyResolver,
    ExecutionUnsupportedStepError,
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    StaticRetryPolicyResolver,
    VendorBillExecutionStrategy,
    vendor_bill_write_idempotency_key,
)
from app.application.workbench import ReviewDecisionType
from app.application.workflow import WorkflowType
from app.billing import VendorBill, VendorBillBuilder
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
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


def test_vendor_bill_execution_strategy_supports_only_vendor_bill_and_modes() -> None:
    strategy = _strategy()

    assert strategy.supported_step_types == (ExecutionStepType.VENDOR_BILL,)
    assert strategy.supports_mode(ExecutionMode.DRY_RUN)
    assert strategy.supports_mode(ExecutionMode.EXECUTE)


def test_strategy_dependencies_are_immutable_by_construction() -> None:
    approval = ExecutionApproval(approved_by="finance.lead")

    assert approval.approved_by == "finance.lead"
    with pytest.raises(FrozenInstanceError):
        approval.approved_by = "other"  # type: ignore[misc]


def test_execution_artifact_is_immutable_and_sanitized() -> None:
    artifact = ExecutionArtifact(
        artifact_type=ExecutionArtifactType.VENDOR_BILL,
        artifact_id="9001",
        external_identity="vendor-bill-write:identity",
        created=True,
    )

    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.external_identity == "vendor-bill-write:identity"
    assert artifact.created is True
    assert not hasattr(artifact, "__dict__")
    assert "payload" not in repr(artifact).lower()
    assert "authorization" not in repr(artifact).lower()
    assert "password" not in repr(artifact).lower()
    assert "token" not in repr(artifact).lower()
    assert "http://" not in repr(artifact).lower()
    assert "https://" not in repr(artifact).lower()
    with pytest.raises(FrozenInstanceError):
        artifact.artifact_id = "9002"  # type: ignore[misc]


def test_wrong_step_type_is_rejected() -> None:
    with pytest.raises(ExecutionUnsupportedStepError):
        _strategy().execute(_step_request(step_type=ExecutionStepType.CUSTOMER_RECHARGE))


def test_dry_run_reuses_builder_and_writer_without_real_write() -> None:
    writer = RecordingVendorBillWriter(result=VendorBillWriteResult(status="dry_run", idempotency_key="unused"))
    builder = RecordingVendorBillBuilder()
    result = _strategy(writer=writer, builder=builder).execute(_step_request(mode=ExecutionMode.DRY_RUN))

    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert result.dry_run is True
    assert builder.calls == 1
    assert writer.calls == 1
    assert writer.commands[0].dry_run is True
    assert writer.real_write_count == 0


def test_source_reader_called_with_exact_execution_identity() -> None:
    reader = RecordingSourceInvoiceReader(source=_source())

    _strategy(reader=reader).execute(_step_request())

    assert reader.calls == (("review-1", 7, 2),)


def test_missing_source_invoice_returns_safe_failure() -> None:
    result = _strategy(reader=MissingSourceInvoiceReader()).execute(_step_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "execution_source_invoice_not_found"
    assert result.message == "Authoritative source invoice was not found."


def test_source_company_mismatch_is_rejected_before_builder_or_writer() -> None:
    writer = RecordingVendorBillWriter()
    builder = RecordingVendorBillBuilder()
    strategy = _strategy(
        reader=RecordingSourceInvoiceReader(source=_source(company_id=8)),
        writer=writer,
        builder=builder,
    )
    result = strategy.execute(_step_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "execution_source_invoice_integrity_error"
    assert builder.calls == 0
    assert writer.calls == 0


def test_source_identity_mismatch_is_rejected_before_builder_or_writer() -> None:
    writer = RecordingVendorBillWriter()
    builder = RecordingVendorBillBuilder()
    result = _strategy(
        reader=RecordingSourceInvoiceReader(source=_source(source_invoice_id="different")),
        writer=writer,
        builder=builder,
    ).execute(_step_request())

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "execution_source_invoice_integrity_error"
    assert builder.calls == 0
    assert writer.calls == 0


def test_strategy_uses_no_workbench_free_form_payload_as_invoice_source() -> None:
    source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")

    assert "comment" not in source
    assert "business_context" not in source
    assert "allocation" not in source.lower()


def test_builder_output_is_passed_to_writer_and_no_build_logic_is_duplicated() -> None:
    writer = RecordingVendorBillWriter()
    builder = RecordingVendorBillBuilder()

    _strategy(writer=writer, builder=builder).execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="lead"))

    assert builder.calls == 1
    assert writer.commands[0].vendor_bill is builder.vendor_bill
    source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")
    assert "VendorBill(" not in source
    assert "VendorBillLine(" not in source
    assert ".match_invoice(" not in source
    assert ".map_invoice(" not in source


def test_execute_without_approved_by_is_rejected_before_writer() -> None:
    writer = RecordingVendorBillWriter()

    with pytest.raises(ExecutionApprovalError):
        _strategy(writer=writer).execute(_step_request(mode=ExecutionMode.EXECUTE))

    assert writer.calls == 0


def test_decided_by_is_not_automatically_used_as_approved_by(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    use_case = _use_case(
        session,
        accepted_decision_reader=StaticAcceptedDecisionReader(_accepted_decision()),
        strategy=_strategy(writer=writer),
    )

    with pytest.raises(ExecutionApprovalError):
        use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert writer.calls == 0
    assert session.query(WorkflowExecution).count() == 0


def test_execute_disabled_fails_before_runtime_creation(session: Session) -> None:
    writer = RecordingVendorBillWriter()

    with pytest.raises(ExecutionModeNotEnabledError):
        _use_case(
            session,
            accepted_decision_reader=StaticAcceptedDecisionReader(_accepted_decision()),
            strategy=_strategy(writer=writer),
            production_execution_enabled=False,
        ).execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert writer.calls == 0
    assert session.query(WorkflowExecution).count() == 0
    assert session.query(WorkflowExecutionEvent).count() == 0


def test_execute_preflight_allows_vendor_bill_only_plan(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    result = _use_case(
        session,
        accepted_decision_reader=StaticAcceptedDecisionReader(_accepted_decision()),
        strategy=_strategy(writer=writer),
    ).execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert writer.calls == 1


@pytest.mark.parametrize("step_type", [ExecutionStepType.SALES_ORDER_COST_LINK, ExecutionStepType.CUSTOMER_RECHARGE])
def test_heterogeneous_execute_plan_rejected_before_writer(session: Session, step_type: ExecutionStepType) -> None:
    writer = RecordingVendorBillWriter()
    decision = _accepted_decision_for_steps((ExecutionStepType.VENDOR_BILL, step_type))

    with pytest.raises(ExecutionUnsupportedStepError):
        _use_case(
            session,
            accepted_decision_reader=StaticAcceptedDecisionReader(decision),
            strategy=_strategy(writer=writer),
        ).execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert writer.calls == 0
    assert session.query(WorkflowExecution).count() == 0
    assert session.query(WorkflowExecutionEvent).count() == 0


def test_unsupported_step_anywhere_prevents_all_erp_writes(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    decision = _accepted_decision_for_steps((ExecutionStepType.VENDOR_BILL, ExecutionStepType.INTERNAL_COST))

    with pytest.raises(ExecutionUnsupportedStepError):
        _use_case(
            session,
            accepted_decision_reader=StaticAcceptedDecisionReader(decision),
            strategy=_strategy(writer=writer),
        ).execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert writer.calls == 0
    assert session.query(WorkflowExecution).count() == 0


def test_execute_calls_writer_once_with_deterministic_write_idempotency_key() -> None:
    writer = RecordingVendorBillWriter(
        result=VendorBillWriteResult(status="created", idempotency_key="unused", external_id=9001)
    )
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead")

    first = _strategy(writer=writer).execute(request)
    second_key = vendor_bill_write_idempotency_key(request)

    assert writer.calls == 1
    assert writer.commands[0].idempotency_key == second_key
    assert len(first.produced_artifacts) == 1
    artifact = first.produced_artifacts[0]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.external_identity == second_key
    assert artifact.created is True
    assert first.status is ExecutionStepStatus.EXECUTED


def test_existing_vendor_bill_artifact_is_successful_and_not_created() -> None:
    writer = RecordingVendorBillWriter(
        result=VendorBillWriteResult(
            status="existing",
            idempotency_key="unused",
            external_id=9001,
            already_exists=True,
            warnings=("Draft Vendor Bill already exists in Odoo.",),
        )
    )

    result = _strategy(writer=writer).execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert result.status is ExecutionStepStatus.EXECUTED
    assert len(result.produced_artifacts) == 1
    artifact = result.produced_artifacts[0]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.external_identity == writer.commands[0].idempotency_key
    assert artifact.created is False
    assert result.warnings == ("Draft Vendor Bill already exists in Odoo.",)


def test_step_result_exposes_produced_artifacts_instead_of_generic_reference_ids() -> None:
    result = _strategy().execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert hasattr(result, "produced_artifacts")
    assert not hasattr(result, "produced_reference_ids")


def test_vendor_bill_artifact_contains_no_provider_payload_or_authentication_data() -> None:
    result = _strategy().execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))
    artifact_text = repr(result.produced_artifacts[0]).lower()

    assert "line_ids" not in artifact_text
    assert "invoice_line_ids" not in artifact_text
    assert "payload" not in artifact_text
    assert "authorization" not in artifact_text
    assert "password" not in artifact_text
    assert "token" not in artifact_text
    assert "api_key" not in artifact_text
    assert "http://" not in artifact_text
    assert "https://" not in artifact_text


def test_writer_command_does_not_include_post_payment_reconcile_or_unlink() -> None:
    writer = RecordingVendorBillWriter()

    _strategy(writer=writer).execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    command_text = repr(writer.commands[0]).lower()
    assert "action_post" not in command_text
    assert "payment" not in command_text
    assert "reconciliation" not in command_text
    assert "unlink" not in command_text


def test_response_loss_retry_uses_same_write_identity_and_recovers_existing_vendor_bill() -> None:
    writer = ResponseLossThenExistingWriter()
    strategy = _strategy(writer=writer)
    request = _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead")

    failed = strategy.execute(request)
    recovered = strategy.execute(request)

    assert failed.status is ExecutionStepStatus.FAILED
    assert failed.error_code == "vendor_bill_transport_failure"
    assert recovered.status is ExecutionStepStatus.EXECUTED
    assert recovered.produced_artifacts[0].artifact_id == "9001"
    assert recovered.produced_artifacts[0].created is False
    assert writer.create_attempts == 1
    assert writer.commands[0].idempotency_key == writer.commands[1].idempotency_key


def test_runtime_completed_step_is_never_replayed(session: Session) -> None:
    writer = RecordingVendorBillWriter()
    use_case = _use_case(
        session,
        accepted_decision_reader=StaticAcceptedDecisionReader(_accepted_decision()),
        strategy=_strategy(writer=writer),
    )

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert second.execution_id == first.execution_id
    assert writer.calls == 1
    assert session.query(WorkflowExecution).count() == 1


def test_runtime_persists_produced_artifact_and_safe_event_metadata(session: Session) -> None:
    repository = SqlAlchemyExecutionRuntimeRepository(session)
    writer = RecordingVendorBillWriter(
        result=VendorBillWriteResult(status="created", idempotency_key="unused", external_id=9001)
    )
    result = _use_case(
        session,
        runtime_repository=repository,
        accepted_decision_reader=StaticAcceptedDecisionReader(_accepted_decision()),
        strategy=_strategy(writer=writer),
    ).execute(_command(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    snapshot = repository.get_snapshot(execution_id=result.execution_id or "")
    history = repository.history(execution_id=result.execution_id or "")

    assert snapshot is not None
    assert snapshot.state is ExecutionState.COMPLETED
    assert snapshot.steps[0].last_result is not None
    assert len(snapshot.steps[0].last_result.produced_artifacts) == 1
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.external_identity == writer.commands[0].idempotency_key
    assert artifact.created is True
    completed_events = [event for event in history.events if event.event_type.value == "step_completed"]
    assert completed_events
    assert completed_events[-1].data["status"] == "executed"
    assert "finance.lead" not in str(completed_events[-1].data)
    assert "line_ids" not in str(completed_events[-1].data)


def test_writer_failures_are_safe_and_conservative() -> None:
    writer = RecordingVendorBillWriter(error=SafeWriterError("transport_failure", "Provider request timed out."))

    result = _strategy(writer=writer).execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert result.status is ExecutionStepStatus.FAILED
    assert result.error_code == "vendor_bill_transport_failure"
    assert result.message == "Provider request timed out."


def test_dry_run_heterogeneous_plan_still_uses_no_write_foundation(session: Session) -> None:
    from app.application.execution import foundation_no_write_strategy_resolver

    repository = SqlAlchemyExecutionRuntimeRepository(session)
    result = RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=StaticAcceptedDecisionReader(
            _accepted_decision_for_steps((ExecutionStepType.VENDOR_BILL, ExecutionStepType.CUSTOMER_RECHARGE))
        ),
        execution_planner=execution_exports.ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(runtime_repository=repository, event_repository=repository),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=repository,
            event_repository=repository,
            strategy_resolver=foundation_no_write_strategy_resolver(),
        ),
        runtime_repository=repository,
        retry_policy_resolver=StaticRetryPolicyResolver(),
    ).execute(_command(mode=ExecutionMode.DRY_RUN))

    assert result.status is AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED


def test_architecture_boundaries() -> None:
    source = Path("app/application/execution/vendor_bill_strategy.py").read_text(encoding="utf-8")

    assert "app.erp" not in source
    assert "sqlalchemy" not in source.lower()
    assert "OdooVendorBillWriter" not in source
    assert "create_account_move" not in source
    assert "search_read" not in source
    assert "uyumsoft" not in source.lower()
    assert "ai_advisor" not in source.lower()
    assert "openai" not in source.lower()
    assert "fuzzy" not in source.lower()
    assert "customer_invoice" not in source
    assert "purchase_order" not in source
    assert "worker" not in source
    assert "scheduler" not in source


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


class RecordingSourceInvoiceReader:
    def __init__(self, *, source: ExecutionSourceInvoice) -> None:
        self._source = source
        self.calls: tuple[tuple[str, int, int], ...] = ()

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        self.calls = (*self.calls, (review_id, company_id, decision_version))
        return self._source


class MissingSourceInvoiceReader:
    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        raise ExecutionSourceInvoiceNotFoundError("Authoritative source invoice was not found.")


class RecordingVendorBillBuilder(VendorBillBuilder):
    def __init__(self) -> None:
        self.calls = 0
        self.vendor_bill = _vendor_bill()

    def build(self, invoice, partner_match, product_match, tax_match, *, company_id: int | None = None) -> VendorBill:
        self.calls += 1
        return self.vendor_bill


class RecordingVendorBillWriter:
    def __init__(
        self,
        *,
        result: VendorBillWriteResult | None = None,
        error: ApplicationError | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0
        self.real_write_count = 0
        self.commands: tuple[VendorBillWriteCommand, ...] = ()

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.calls += 1
        if not command.dry_run:
            self.real_write_count += 1
        self.commands = (*self.commands, command)
        if self._error is not None:
            raise self._error
        return self._result or VendorBillWriteResult(
            status="created" if not command.dry_run else "dry_run",
            idempotency_key=command.idempotency_key,
            external_id=None if command.dry_run else 9001,
            safe_message="ok",
            success=True,
        )


class ResponseLossThenExistingWriter:
    def __init__(self) -> None:
        self.commands: tuple[VendorBillWriteCommand, ...] = ()
        self.create_attempts = 0

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.commands = (*self.commands, command)
        if len(self.commands) == 1:
            self.create_attempts += 1
            raise SafeWriterError("transport_failure", "Provider request timed out.")
        return VendorBillWriteResult(
            status="existing",
            idempotency_key=command.idempotency_key,
            external_id=9001,
            already_exists=True,
            safe_message="Draft Vendor Bill already exists in Odoo.",
            success=True,
        )


class SafeWriterError(ApplicationError):
    def __init__(self, error_category: str, safe_message: str) -> None:
        self.error_category = error_category
        super().__init__(safe_message)


def _strategy(
    *,
    reader=None,
    builder: RecordingVendorBillBuilder | None = None,
    writer=None,
) -> VendorBillExecutionStrategy:
    return VendorBillExecutionStrategy(
        source_invoice_reader=reader or RecordingSourceInvoiceReader(source=_source()),
        vendor_bill_builder=builder or RecordingVendorBillBuilder(),
        vendor_bill_writer=writer or RecordingVendorBillWriter(),
    )


def _step_request(
    *,
    mode: ExecutionMode = ExecutionMode.DRY_RUN,
    step_type: ExecutionStepType = ExecutionStepType.VENDOR_BILL,
    approved_by: str | None = None,
) -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        step=ExecutionStep(
            step_key=f"review-1:2:{step_type.value}:workflow",
            step_type=step_type,
            allocation_keys=(),
            sequence=1,
            execute_supported=step_type is ExecutionStepType.VENDOR_BILL,
        ),
        approval=ExecutionApproval(approved_by=approved_by) if approved_by is not None else None,
    )


def _source(
    *,
    company_id: int = 7,
    source_invoice_id: str = "ETTN-1",
) -> ExecutionSourceInvoice:
    invoice = _invoice()
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=company_id,
        decision_version=2,
        source_invoice_id=source_invoice_id,
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
                        company_id=company_id,
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


def _vendor_bill() -> VendorBill:
    return VendorBillBuilder().build(
        _invoice(),
        _source().partner_match,
        _source().product_match,
        _source().tax_match,
    )


def _accepted_decision() -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id="decision-1",
        selected_workflow=WorkflowType.VENDOR_BILL,
        business_context_allocations=None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _accepted_decision_for_steps(step_types: tuple[ExecutionStepType, ...]) -> AcceptedReviewDecision:
    from app.application.workbench import (
        AllocationCompleteness,
        BusinessContextAllocation,
        BusinessContextAllocationSet,
        BusinessContextAllocationType,
    )

    allocations = []
    if ExecutionStepType.SALES_ORDER_COST_LINK in step_types:
        allocations.append(
            BusinessContextAllocation(
                allocation_key="SO",
                allocation_type=BusinessContextAllocationType.SALES_ORDER_COST,
                amount=Decimal("50.00"),
                currency="TRY",
                sales_order_id=501,
            )
        )
    if ExecutionStepType.CUSTOMER_RECHARGE in step_types:
        allocations.append(
            BusinessContextAllocation(
                allocation_key="RECHARGE",
                allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
                amount=Decimal("50.00"),
                currency="TRY",
                recharge_partner_id=701,
            )
        )
    if ExecutionStepType.INTERNAL_COST in step_types:
        allocations.append(
            BusinessContextAllocation(
                allocation_key="INTERNAL",
                allocation_type=BusinessContextAllocationType.INTERNAL_COST,
                amount=Decimal("50.00"),
                currency="TRY",
            )
        )
    return AcceptedReviewDecision(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        decision_id="decision-1",
        selected_workflow=WorkflowType.VENDOR_BILL if ExecutionStepType.VENDOR_BILL in step_types else None,
        business_context_allocations=BusinessContextAllocationSet(
            allocations=tuple(allocations),
            completeness=AllocationCompleteness.PARTIAL,
            invoice_total=Decimal("150.00"),
            currency="TRY",
        )
        if allocations
        else None,
        decision_type=ReviewDecisionType.SELECT_WORKFLOW,
    )


def _command(*, mode: ExecutionMode, approved_by: str | None = None) -> RunAcceptedDecisionExecutionCommand:
    return RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        approval=ExecutionApproval(approved_by=approved_by) if approved_by is not None else None,
    )


def _use_case(
    session: Session,
    *,
    accepted_decision_reader: AcceptedReviewDecisionReader,
    strategy: VendorBillExecutionStrategy,
    runtime_repository: SqlAlchemyExecutionRuntimeRepository | None = None,
    production_execution_enabled: bool = True,
) -> RunAcceptedDecisionExecutionUseCase:
    repository = runtime_repository or SqlAlchemyExecutionRuntimeRepository(session)
    return RunAcceptedDecisionExecutionUseCase(
        accepted_decision_reader=accepted_decision_reader,
        execution_planner=execution_exports.ExecutionPlanner(),
        runtime_service=ExecutionRuntimeService(runtime_repository=repository, event_repository=repository),
        runtime_coordinator=ExecutionRuntimeCoordinator(
            runtime_repository=repository,
            event_repository=repository,
            strategy_resolver=ExecutionStrategyResolver((strategy,)),
        ),
        runtime_repository=repository,
        retry_policy_resolver=StaticRetryPolicyResolver(ExecutionRetryPolicy.never()),
        execution_preflight=ExecutionPreflightPolicy(production_execution_enabled=production_execution_enabled),
    )


def _engine():
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


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=_engine())
    with factory() as db_session:
        yield db_session
