from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.commands import CustomerInvoiceWriteCommand
from app.application.dto import CustomerInvoiceWriteResult
from app.application.execution import (
    CustomerInvoiceExecutionStrategy,
    CustomerRechargeExecutionRouter,
    CustomerRechargeExecutionStrategy,
    ExecutionApproval,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionSourceInvoice,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategyResolver,
    ExecutionUnsupportedStepError,
    customer_invoice_write_idempotency_key,
)
from app.application.workbench import BusinessContextAllocation, BusinessContextAllocationType
from app.billing import CustomerInvoiceBuilder
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


def test_customer_invoice_strategy_supports_only_creation_mode_allocations() -> None:
    strategy = _strategy(writer=RecordingCustomerInvoiceWriter())

    assert strategy.supported_step_types == (ExecutionStepType.CUSTOMER_RECHARGE,)
    assert strategy.supports_step(step=_step(customer_invoice_id=None), mode=ExecutionMode.EXECUTE)
    assert not strategy.supports_step(step=_step(customer_invoice_id=7001), mode=ExecutionMode.EXECUTE)


def test_customer_invoice_strategy_rejects_existing_invoice_allocation() -> None:
    with pytest.raises(ExecutionUnsupportedStepError):
        _strategy(writer=RecordingCustomerInvoiceWriter()).execute(
            _request(customer_invoice_id=7001, mode=ExecutionMode.EXECUTE)
        )


def test_customer_recharge_router_keeps_existing_and_creation_modes_separate() -> None:
    creation_writer = RecordingCustomerInvoiceWriter()
    router = CustomerRechargeExecutionRouter((CustomerRechargeExecutionStrategy(), _strategy(writer=creation_writer)))

    existing = router.execute(_request(customer_invoice_id=7001, mode=ExecutionMode.EXECUTE))
    created = router.execute(_request(customer_invoice_id=None, mode=ExecutionMode.EXECUTE))

    assert existing.produced_artifacts[0].created is False
    assert created.produced_artifacts[0].artifact_type is ExecutionArtifactType.CUSTOMER_INVOICE
    assert created.produced_artifacts[0].artifact_id == "9101"
    assert created.produced_artifacts[0].external_identity.startswith("customer-invoice-write:")
    assert created.produced_artifacts[0].created is True
    assert creation_writer.calls == 1


def test_creation_dry_run_builds_without_external_write() -> None:
    writer = RecordingCustomerInvoiceWriter()
    result = _strategy(writer=writer).execute(_request(customer_invoice_id=None, mode=ExecutionMode.DRY_RUN))

    assert result.status is ExecutionStepStatus.DRY_RUN_OK
    assert result.produced_artifacts == ()
    assert writer.commands[0].dry_run is True


def test_customer_invoice_builder_returns_immutable_draft() -> None:
    source = _source(review_id="review-1", company_id=7, decision_version=2)
    draft = CustomerInvoiceBuilder().build(
        company_id=7,
        source_invoice_id=source.source_invoice_id,
        invoice=source.invoice,
        product_match=source.product_match,
        tax_match=source.tax_match,
        allocations=_step(customer_invoice_id=None).allocations,
    )

    with pytest.raises(AttributeError):
        draft.customer_id = 1  # type: ignore[misc]


def test_creation_idempotency_is_deterministic_and_distinct_from_execution_id() -> None:
    request = _request(customer_invoice_id=None, mode=ExecutionMode.EXECUTE)

    first = customer_invoice_write_idempotency_key(request)
    second = customer_invoice_write_idempotency_key(request)

    assert first == second
    assert first != request.execution_id
    assert first.startswith("customer-invoice-write:")


def test_resolver_rejects_creation_step_when_only_existing_invoice_strategy_is_registered() -> None:
    resolver = ExecutionStrategyResolver((CustomerRechargeExecutionStrategy(),))

    try:
        resolver.ensure_plan_supports_mode(plan=_plan_like(_step(customer_invoice_id=None)), mode=ExecutionMode.EXECUTE)
    except ExecutionUnsupportedStepError:
        pass
    else:  # pragma: no cover - assertion branch
        raise AssertionError("creation-mode customer recharge must not be authorized by existing strategy")


def test_customer_invoice_strategy_has_no_infrastructure_dependencies() -> None:
    source = Path("app/application/execution/customer_invoice_strategy.py").read_text(encoding="utf-8")

    assert "sqlalchemy" not in source.lower()
    assert "app.erp" not in source
    assert "odoo" not in source.lower()
    assert "create_account_move" not in source
    assert "action_post" not in source
    assert "payment" not in source.lower()
    assert "reconciliation" not in source.lower()
    assert "fuzzy" not in source.lower()
    assert "openai" not in source.lower()


class RecordingCustomerInvoiceWriter:
    def __init__(self, *, status: str = "created", external_id: int = 9101) -> None:
        self.status = status
        self.external_id = external_id
        self.calls = 0
        self.commands: list[CustomerInvoiceWriteCommand] = []

    async def write_customer_invoice(self, command: CustomerInvoiceWriteCommand) -> CustomerInvoiceWriteResult:
        self.calls += 1
        self.commands.append(command)
        return CustomerInvoiceWriteResult(
            status="dry_run" if command.dry_run else self.status,  # type: ignore[arg-type]
            idempotency_key=command.idempotency_key,
            external_id=None if command.dry_run else self.external_id,
            external_model=None if command.dry_run else "account.move",
            safe_message="ok",
            success=True,
        )


class StaticSourceInvoiceReader:
    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
        return _source(review_id=review_id, company_id=company_id, decision_version=decision_version)


def _strategy(*, writer: RecordingCustomerInvoiceWriter) -> CustomerInvoiceExecutionStrategy:
    return CustomerInvoiceExecutionStrategy(
        source_invoice_reader=StaticSourceInvoiceReader(),
        customer_invoice_builder=CustomerInvoiceBuilder(),
        customer_invoice_writer=writer,
    )


def _request(*, customer_invoice_id: int | None, mode: ExecutionMode) -> ExecutionStepRequest:
    return ExecutionStepRequest(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=mode,
        step=_step(customer_invoice_id=customer_invoice_id),
        approval=ExecutionApproval(approved_by="finance.lead") if mode is ExecutionMode.EXECUTE else None,
    )


def _step(*, customer_invoice_id: int | None) -> ExecutionStep:
    allocation = BusinessContextAllocation(
        allocation_key="A",
        allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE,
        source_line_number="1",
        amount=Decimal("120.00"),
        currency="TRY",
        recharge_partner_id=701,
        customer_invoice_id=customer_invoice_id,
    )
    return ExecutionStep(
        step_key="review-1:2:customer_recharge:A",
        step_type=ExecutionStepType.CUSTOMER_RECHARGE,
        allocation_keys=("A",),
        sequence=1,
        execute_supported=True,
        writer_required=customer_invoice_id is None,
        allocations=(allocation,),
    )


def _plan_like(step: ExecutionStep):
    from app.application.execution import ExecutionPlan

    return ExecutionPlan(
        execution_id="execution-1",
        review_id="review-1",
        company_id=7,
        decision_version=2,
        mode=ExecutionMode.EXECUTE,
        steps=(step,),
        idempotency_key="execution:key",
    )


def _source(*, review_id: str, company_id: int, decision_version: int) -> ExecutionSourceInvoice:
    invoice = InternalInvoice(
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
                quantity=Decimal("1"),
                unit_price=Decimal("100.00"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )
    return ExecutionSourceInvoice(
        review_id=review_id,
        company_id=company_id,
        decision_version=decision_version,
        source_invoice_id="ETTN-1",
        invoice=invoice,
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=1001,
            matched_by="tax_number",
            reason="matched",
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
