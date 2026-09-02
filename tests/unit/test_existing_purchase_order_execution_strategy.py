from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

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
from app.erp.odoo.purchase_order_vendor_bill_repository import PurchaseOrderVendorBillRepository
from app.erp.write.account_move_repository import AccountMoveDraft
from app.erp.write.exceptions import VendorBillWriteValidationError
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
    def __init__(self, *, recovered: AccountMoveDraft | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.recovered = recovered

    async def create_or_recover_vendor_bill_from_purchase_order(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.recovered is not None:
            return type("Outcome", (), {"move": self.recovered, "created": False})()
        return type("Outcome", (), {"move": AccountMoveDraft(id=901), "created": True})()


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
        _step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"),
        purchase_order_id=501,
    )


def test_existing_purchase_order_execute_recovers_existing_bill_and_marks_created_false() -> None:
    repository = RecordingPurchaseOrderVendorBillRepository(recovered=AccountMoveDraft(id=902, name="BILL-902"))
    strategy = ExistingPurchaseOrderExecutionStrategy(
        source_invoice_reader=RecordingSourceInvoiceReader(_source()),
        purchase_order_vendor_bill_repository=repository,
    )

    result = strategy.execute(_step_request(mode=ExecutionMode.EXECUTE, approved_by="finance.lead"))

    assert result.status is ExecutionStepStatus.EXECUTED
    assert result.produced_artifacts[0].artifact_id == "902"
    assert result.produced_artifacts[0].created is False
    assert repository.calls[0]["purchase_order_id"] == 501


class RecordingPurchaseOrderClient:
    def __init__(
        self,
        *,
        purchase_orders: list[dict[str, object]] | None = None,
        account_moves: list[dict[str, object]] | None = None,
    ) -> None:
        self.purchase_orders = purchase_orders or []
        self.account_moves = account_moves or []
        self.search_calls: list[tuple[str, list[list[object]], list[str]]] = []
        self.action_calls: list[tuple[str, int]] = []
        self.write_calls: list[tuple[int, dict[str, object]]] = []

    async def search_read(
        self,
        *,
        model: str,
        domain: list[Any],
        fields: list[str],
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        self.search_calls.append((model, domain, fields))
        if model == "purchase.order":
            return self.purchase_orders
        if model == "account.move":
            return self.account_moves
        return []

    async def call_model_method(
        self,
        *,
        model: str,
        method: str,
        ids: list[int] | None = None,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        self.action_calls.append((method, ids[0] if ids else 0))
        return {"res_id": 901}

    async def write_account_move(self, *, record_id: int, values: dict[str, Any]) -> bool:
        self.write_calls.append((record_id, values))
        return True


async def test_purchase_order_vendor_bill_repository_recovers_existing_vendor_bill_before_create() -> None:
    client = RecordingPurchaseOrderClient(
        purchase_orders=[
            {
                "id": 501,
                "name": "PO-501",
                "state": "purchase",
                "company_id": [7, "Company"],
                "partner_id": [1001, "Supplier"],
            }
        ],
        account_moves=[
            {
                "id": 901,
                "name": "BILL-901",
                "move_type": "in_invoice",
                "company_id": 7,
                "partner_id": 1001,
                "purchase_id": 501,
            }
        ],
    )
    repo = PurchaseOrderVendorBillRepository(client=client)

    result = await repo.create_or_recover_vendor_bill_from_purchase_order(
        purchase_order_id=501,
        company_id=7,
        partner_id=1001,
        idempotency_key="purchase-order-bill:recovery-test",
    )

    assert result.created is False
    assert result.move.id == 901
    assert client.action_calls == []
    assert client.write_calls == []


async def test_purchase_order_vendor_bill_repository_rejects_non_billable_purchase_order_state() -> None:
    client = RecordingPurchaseOrderClient(
        purchase_orders=[
            {
                "id": 501,
                "name": "PO-501",
                "state": "draft",
                "company_id": [7, "Company"],
                "partner_id": [1001, "Supplier"],
            }
        ]
    )
    repo = PurchaseOrderVendorBillRepository(client=client)

    with pytest.raises(VendorBillWriteValidationError, match="not in a billable state"):
        await repo.create_or_recover_vendor_bill_from_purchase_order(
            purchase_order_id=501,
            company_id=7,
            partner_id=1001,
            idempotency_key="purchase-order-bill:reject-state",
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
