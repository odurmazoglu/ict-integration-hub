from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.execution import (
    AcceptedDecisionExecutionStatus,
    ExecutionApproval,
    ExecutionArtifactType,
    ExecutionEventType,
    ExecutionMode,
    ExecutionModeNotEnabledError,
    ExecutionRuntimeStepState,
    ExecutionSourceInvoice,
    ExecutionState,
    RunAcceptedDecisionExecutionCommand,
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
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.composition import build_vendor_bill_execution_use_case
from app.connectors.exceptions import ConnectorTimeoutError
from app.core.config import Settings
from app.core.runtime_checks import PRODUCTION_APPROVAL_ACK
from app.db.base import Base
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.erp.write import CustomerInvoiceWriteSafetyGateError, VendorBillWriteSafetyGateError
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.persistence import SqlAlchemyExecutionRuntimeRepository, SqlAlchemyReviewRepository
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    with factory() as db_session:
        Base.metadata.create_all(db_session.get_bind())
        yield db_session


def test_dry_run_uses_full_composition_without_odoo_write(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()

    result = _use_case(session, client=client, settings=_settings()).execute(_command(mode=ExecutionMode.DRY_RUN))

    assert result.status is AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert client.search_calls == []
    assert client.create_calls == []


def test_execute_disabled_fails_before_runtime_or_odoo_call(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(ExecutionModeNotEnabledError):
        _use_case(session, client=client, settings=_settings(execution_execute_enabled=False)).execute(
            _command(mode=ExecutionMode.EXECUTE)
        )

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


@pytest.mark.parametrize(
    ("production_operations_enabled", "production_approval_ack"),
    [
        (False, PRODUCTION_APPROVAL_ACK),
        (True, ""),
    ],
)
def test_writer_safety_gate_failures_happen_before_runtime_or_odoo_call(
    session: Session,
    production_operations_enabled: bool,
    production_approval_ack: str,
) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient()
    settings = _settings(
        execution_execute_enabled=True,
        production_operations_enabled=production_operations_enabled,
        production_approval_ack=production_approval_ack,
    )

    with pytest.raises(VendorBillWriteSafetyGateError):
        _use_case(session, client=client, settings=settings).execute(_command(mode=ExecutionMode.EXECUTE))

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_execute_enabled_creates_one_draft_vendor_bill_and_persists_artifact(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9001)
    repository = SqlAlchemyExecutionRuntimeRepository(session)

    result = _use_case(session, client=client, settings=_execute_settings()).execute(
        _command(mode=ExecutionMode.EXECUTE)
    )

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert len(client.search_calls) == 1
    assert len(client.create_calls) == 1
    payload = client.create_calls[0]
    assert payload["move_type"] == "in_invoice"
    assert "action_post" not in str(payload).lower()
    assert "payment" not in str(payload).lower()
    snapshot = repository.get_snapshot(execution_id=result.execution_id or "")
    assert snapshot is not None
    assert snapshot.state is ExecutionState.COMPLETED
    assert snapshot.steps[0].state is ExecutionRuntimeStepState.COMPLETED
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_type is ExecutionArtifactType.VENDOR_BILL
    assert artifact.artifact_id == "9001"
    assert artifact.created is True
    history = repository.history(execution_id=result.execution_id or "")
    assert history.events[-1].event_type is ExecutionEventType.EXECUTION_COMPLETED


def test_transport_timeout_retry_recovers_existing_vendor_bill_without_duplicate(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(timeout_after_create=True, created_id=9001)
    use_case = _use_case(session, client=client, settings=_execute_settings())

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert second.runtime_state is ExecutionState.COMPLETED
    assert len(client.create_calls) == 1
    assert len(client.search_calls) == 2
    snapshot = SqlAlchemyExecutionRuntimeRepository(session).get_snapshot(execution_id=second.execution_id or "")
    assert snapshot is not None
    artifact = snapshot.steps[0].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_id == "9001"
    assert artifact.created is False


def test_completed_runtime_never_replays_vendor_bill_write(session: Session) -> None:
    _submit_vendor_bill_decision(session)
    client = FakeOdooVendorBillClient(created_id=9001)
    use_case = _use_case(session, client=client, settings=_execute_settings())

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert second.execution_id == first.execution_id
    assert len(client.search_calls) == 1
    assert len(client.create_calls) == 1
    assert _runtime_count(session) == 1


def test_customer_invoice_execute_disabled_fails_before_runtime_or_odoo_call(session: Session) -> None:
    _submit_customer_invoice_decision(session)
    client = FakeOdooVendorBillClient()

    with pytest.raises(CustomerInvoiceWriteSafetyGateError):
        _use_case(session, client=client, settings=_execute_settings(customer_invoice_execute_enabled=False)).execute(
            _command(mode=ExecutionMode.EXECUTE)
        )

    assert _runtime_count(session) == 0
    assert client.search_calls == []
    assert client.create_calls == []


def test_execute_enabled_creates_draft_vendor_bill_and_customer_invoice_artifacts(session: Session) -> None:
    _submit_customer_invoice_decision(session)
    client = FakeOdooVendorBillClient(created_id=9101)
    repository = SqlAlchemyExecutionRuntimeRepository(session)

    result = _use_case(
        session,
        client=client,
        settings=_execute_settings(customer_invoice_execute_enabled=True),
    ).execute(
        _command(mode=ExecutionMode.EXECUTE),
    )

    assert result.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert result.runtime_state is ExecutionState.COMPLETED
    assert len(client.search_calls) == 2
    assert len(client.create_calls) == 2
    payload = client.create_calls[1]
    assert payload["move_type"] == "out_invoice"
    assert payload["company_id"] == 7
    assert payload["partner_id"] == 701
    assert "action_post" not in str(payload).lower()
    assert "payment" not in str(payload).lower()
    snapshot = repository.get_snapshot(execution_id=result.execution_id or "")
    assert snapshot is not None
    artifact = snapshot.steps[1].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_type is ExecutionArtifactType.CUSTOMER_INVOICE
    assert artifact.artifact_id == "9101"
    assert artifact.external_identity.startswith("customer-invoice-write:")
    assert artifact.created is True


def test_customer_invoice_timeout_retry_recovers_existing_without_duplicate(session: Session) -> None:
    _submit_customer_invoice_decision(session)
    client = FakeOdooVendorBillClient(timeout_after_create=True, timeout_move_type="out_invoice", created_id=9101)
    use_case = _use_case(session, client=client, settings=_execute_settings(customer_invoice_execute_enabled=True))

    first = use_case.execute(_command(mode=ExecutionMode.EXECUTE))
    second = use_case.execute(_command(mode=ExecutionMode.EXECUTE))

    assert first.runtime_state is ExecutionState.WAITING_RETRY
    assert second.status is AcceptedDecisionExecutionStatus.EXECUTED
    assert len(client.create_calls) == 2
    assert len(client.search_calls) == 3
    snapshot = SqlAlchemyExecutionRuntimeRepository(session).get_snapshot(execution_id=second.execution_id or "")
    assert snapshot is not None
    artifact = snapshot.steps[1].last_result.produced_artifacts[0]  # type: ignore[union-attr]
    assert artifact.artifact_id == "9101"
    assert artifact.created is False


def test_production_composition_keeps_infrastructure_out_of_application_layer() -> None:
    application_source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application").rglob("*.py"))
    composition_source = Path("app/composition/execution.py").read_text(encoding="utf-8")

    assert "OdooVendorBillWriter" not in application_source
    assert "OdooCustomerInvoiceWriter" not in application_source
    assert "AccountMoveRepository" not in application_source
    assert "sqlalchemy" not in application_source.lower()
    assert "OdooVendorBillWriter" in composition_source
    assert "OdooCustomerInvoiceWriter" in composition_source
    assert "AccountMoveRepository" in composition_source
    assert "ExecutionRetryPolicy.immediate(max_attempts=2)" in composition_source


class FakeOdooVendorBillClient:
    def __init__(
        self,
        *,
        created_id: int = 9001,
        timeout_after_create: bool = False,
        timeout_move_type: str | None = None,
    ) -> None:
        self.created_id = created_id
        self.timeout_after_create = timeout_after_create
        self.timeout_move_type = timeout_move_type
        self.created_move_types: set[str] = set()
        self.search_calls: list[list[Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(domain)
        move_type = _domain_value(domain, "move_type") or "in_invoice"
        if move_type not in self.created_move_types:
            return []
        partner_id = _domain_value(domain, "partner_id")
        return [{"id": self.created_id, "name": "MOVE/2026/001", "move_type": move_type, "partner_id": partner_id}]

    async def create_account_move(self, payload: dict[str, Any]) -> int:
        self.create_calls.append(payload)
        move_type = str(payload.get("move_type"))
        self.created_move_types.add(move_type)
        if self.timeout_after_create and (
            self.timeout_move_type is None and len(self.create_calls) == 1 or self.timeout_move_type == move_type
        ):
            self.timeout_after_create = False
            raise ConnectorTimeoutError("Odoo request timed out.")
        return self.created_id


def _use_case(session: Session, *, client: FakeOdooVendorBillClient, settings: Settings):
    return build_vendor_bill_execution_use_case(session=session, settings=settings, odoo_client=client)  # type: ignore[arg-type]


def _command(*, mode: ExecutionMode) -> RunAcceptedDecisionExecutionCommand:
    return RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        approval=ExecutionApproval(approved_by="finance.lead") if mode is ExecutionMode.EXECUTE else None,
    )


def _submit_vendor_bill_decision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(), company_id=7, idempotency_key="review:item")
    repository.submit_review_decision_with_execution_evidence(
        ReviewDecisionCommand(
            review_id="review-1",
            company_id=7,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            decided_by="finance.user",
            idempotency_key="review:decision",
        ),
        _source(),
    )


def _submit_customer_invoice_decision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item(), company_id=7, idempotency_key="review:item")
    repository.submit_review_decision_with_execution_evidence(
        ReviewDecisionCommand(
            review_id="review-1",
            company_id=7,
            expected_version=1,
            decision=ReviewDecisionType.SELECT_WORKFLOW,
            selected_workflow=WorkflowType.VENDOR_BILL,
            business_context_allocations=BusinessContextAllocationSet(
                allocations=(
                    BusinessContextAllocation(
                        allocation_key="A",
                        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
                        source_line_number="1",
                        amount=Decimal("120.00"),
                        currency="TRY",
                        recharge_partner_id=701,
                    ),
                ),
                completeness=AllocationCompleteness.PARTIAL,
                invoice_total=Decimal("120.00"),
                currency="TRY",
            ),
            decided_by="finance.user",
            idempotency_key="review:decision",
        ),
        _source(),
    )


def _review_item() -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
        invoice_id="ETTN-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("120.00"),
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
    )


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


def _settings(
    *,
    execution_execute_enabled: bool = False,
    production_operations_enabled: bool = False,
    production_approval_ack: str = "",
    customer_invoice_execute_enabled: bool = False,
) -> Settings:
    return Settings(
        execution_execute_enabled=execution_execute_enabled,
        production_operations_enabled=production_operations_enabled,
        production_approval_ack=production_approval_ack,
        customer_invoice_execute_enabled=customer_invoice_execute_enabled,
    )


def _execute_settings(*, customer_invoice_execute_enabled: bool = False) -> Settings:
    return _settings(
        execution_execute_enabled=True,
        production_operations_enabled=True,
        production_approval_ack=PRODUCTION_APPROVAL_ACK,
        customer_invoice_execute_enabled=customer_invoice_execute_enabled,
    )


def _runtime_count(session: Session) -> int:
    from app.models.workflow_execution import WorkflowExecution

    return session.query(WorkflowExecution).count()


def _domain_value(domain: list[Any], field: str) -> Any:
    for item in domain:
        if isinstance(item, list) and len(item) >= 3 and item[0] == field:
            return item[2]
    return None
