from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.application.execution import (
    ExecutionApproval,
    ExecutionApprovalError,
    ExecutionMode,
    ExecutionSourceInvoice,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.existing_purchase_order_strategy import (
    ExistingPurchaseOrderExecutionStrategy,
    existing_purchase_order_write_idempotency_key,
)
from app.application.workbench.allocations import (
    BusinessContextAllocation,
    BusinessContextAllocationType,
)
from app.domain.invoice import Header, InternalInvoice, MonetaryTotals, Party
from app.erp.write.account_move_repository import AccountMoveDraft
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus
from app.tax_mapping import InvoiceTaxMappingResult


class RecordingSourceInvoiceReader:
    def __init__(self, source: ExecutionSourceInvoice) -> None:
        self.source = source
        self.calls: list[tuple[str, int, int]] = []

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        self.calls.append((review_id, company_id, decision_version))
        return self.source


class RecordingPurchaseOrderVendorBillRepository:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_vendor_bill_from_purchase_order(self, **kwargs: object) -> AccountMoveDraft:
        self.calls.append(kwargs)
        return AccountMoveDraft(id=901)


def test_existing_purchase_order_strategy_supports_only_existing_purchase_order_and_modes() -> None:
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(_source()),
        purchase_order_vendor_bill_repository=RecordingPurchaseOrderVendorBillRepository(),
    )

    assert strategy.supported_step_types == (ExecutionStepType.EXISTING_PURCHASE_ORDER,)
    assert strategy.supports_mode(ExecutionMode.DRY_RUN)
    assert strategy.supports_mode(ExecutionMode.EXECUTE)


def test_existing_purchase_order_dry_run_does_not_write() -> None:
    repository = RecordingPurchaseOrderVendorBillRepository()
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(_source()),
        purchase_order_vendor_bill_repository=repository,
    )

    result = strategy.execute(_step_request(mode=ExecutionMode.DRY_RUN))

    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert result.dry_run is True
    assert repository.calls == []


def test_existing_purchase_order_execute_requires_approval() -> None:
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(_source()),
        purchase_order_vendor_bill_repository=RecordingPurchaseOrderVendorBillRepository(),
    )

    with pytest.raises(ExecutionApprovalError):
        strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by=None))


def test_existing_purchase_order_execute_uses_purchase_order_and_returns_artifact() -> None:
    repository = RecordingPurchaseOrderVendorBillRepository()
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(_source()),
        purchase_order_vendor_bill_repository=repository,
    )

    result = strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert result.status is ExecutionStepStatus.EXECUTED
    assert result.produced_artifacts[0].artifact_id == "901"
    assert result.produced_artifacts[0].created is True
    assert repository.calls[0]["purchase_order_id"] == 501
    assert repository.calls[0]["company_id"] == 7
    assert repository.calls[0]["partner_id"] == 1001
    assert repository.calls[0]["idempotency_key"] == existing_purchase_order_write_idempotency_key(
        _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead")
    )


def _step_request(
    *,
    mode: ExecutionMode = ExecutionMode.EXECUTE,
    approved_by: str | None = "finance.lead",
) -> ExecutionStepRequest:
    allocation = BusinessContextAllocation(
        allocation_key="PO-1",
        allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
        amount=Decimal("100.00"),
        purchase_order_id=501,
        currency="USD",
    )
    return ExecutionStepRequest(
        execution_id="exec-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        step=ExecutionStep(
            step_key="po-step-1",
            step_type=ExecutionStepType.EXISTING_PURCHASE_ORDER,
            allocation_keys=("PO-1",),
            sequence=1,
            dry_run_supported=True,
            execute_supported=True,
            writer_required=True,
            allocations=(allocation,),
        ),
        approval=ExecutionApproval(approved_by=approved_by) if approved_by else None,
    )


def _source() -> ExecutionSourceInvoice:
    invoice = InternalInvoice(
        header=Header(
            invoice_number="INV-1001",
            invoice_uuid="uuid-1001",
            ettn="ETTN-1001",
            invoice_type="invoice",
            issue_date=date(2024, 1, 15),
            currency_code="USD",
            notes=("Source invoice",),
        ),
        supplier=Party(name="Supplier"),
        customer=Party(name="Buyer"),
        totals=MonetaryTotals(),
        lines=(),
    )
    return ExecutionSourceInvoice(
        review_id="review-1",
        company_id=7,
        decision_version=2,
        source_invoice_id="ETTN-1001",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=1001,
            matched_by="supplier_tax_number",
            reason="matched",
            candidate_count=1,
            confidence=Decimal("1.00"),
        ),
        product_match=InvoiceProductMatchResult(),
        tax_match=InvoiceTaxMappingResult(),
    )
