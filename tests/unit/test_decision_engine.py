from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand, VendorBillWriteCommand
from app.application.decision import (
    DecisionEngine,
    UnsupportedWorkflowError,
    VendorBillStrategy,
    WorkflowStrategyResolver,
)
from app.application.dto import DecisionResult, RuleEvaluationResult, VendorBillWriteResult
from app.application.ports import RuleEngine
from app.billing import VendorBillBuilder
from app.domain.invoice import Header, InternalInvoice, InvoiceLine, MonetaryTotals, Party, Tax
from app.matching import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)


@pytest.mark.asyncio
async def test_decision_engine_selects_and_executes_resolved_strategy() -> None:
    rule_result = _rule_result()
    rule_engine = FakeRuleEngine(rule_result)
    strategy = FakeWorkflowStrategy("vendor_bill")
    engine = DecisionEngine(rule_engine=rule_engine, strategy_resolver=WorkflowStrategyResolver([strategy]))
    command = _command()

    result = await engine.decide(command)

    assert result == DecisionResult(
        success=True,
        invoice_id="INV-ETTN",
        workflow="vendor_bill",
        strategy="fake_vendor_bill",
        status="created",
        vendor_bill_id=42,
        warnings=("rules ok", "strategy ok"),
        errors=(),
        duration=result.duration,
    )
    assert result.duration >= 0
    assert rule_engine.commands == [command]
    assert strategy.calls == [(command, rule_result)]


@pytest.mark.asyncio
async def test_decision_engine_propagates_rule_engine_errors() -> None:
    engine = DecisionEngine(
        rule_engine=FakeRuleEngine(RuntimeError("rule failure")),
        strategy_resolver=WorkflowStrategyResolver([FakeWorkflowStrategy("vendor_bill")]),
    )

    with pytest.raises(RuntimeError, match="rule failure"):
        await engine.decide(_command())


@pytest.mark.asyncio
async def test_decision_engine_propagates_strategy_errors() -> None:
    engine = DecisionEngine(
        rule_engine=FakeRuleEngine(_rule_result()),
        strategy_resolver=WorkflowStrategyResolver([FakeWorkflowStrategy("vendor_bill", error=RuntimeError("boom"))]),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await engine.decide(_command())


@pytest.mark.asyncio
async def test_decision_engine_rejects_unsupported_workflow() -> None:
    engine = DecisionEngine(
        rule_engine=FakeRuleEngine(RuleEvaluationResult(workflow="expense")),
        strategy_resolver=WorkflowStrategyResolver([FakeWorkflowStrategy("vendor_bill")]),
    )

    with pytest.raises(UnsupportedWorkflowError) as exc_info:
        await engine.decide(_command())

    assert exc_info.value.safe_message == "Unsupported workflow: expense."


@pytest.mark.asyncio
async def test_vendor_bill_strategy_delegates_to_builder_and_writer() -> None:
    writer = FakeVendorBillWriter(
        VendorBillWriteResult(
            status="created",
            idempotency_key="ettn:INV-ETTN",
            external_id=99,
            safe_message="Draft created.",
        )
    )
    strategy = VendorBillStrategy(vendor_bill_builder=VendorBillBuilder(), vendor_bill_writer=writer)

    result = await strategy.execute(_command(), _rule_result(warnings=()))

    assert result.success is True
    assert result.invoice_id == "INV-ETTN"
    assert result.workflow == "vendor_bill"
    assert result.strategy == "vendor_bill"
    assert result.status == "created"
    assert result.vendor_bill_id == 99
    assert writer.commands == [
        VendorBillWriteCommand(
            vendor_bill=writer.commands[0].vendor_bill,
            idempotency_key="ettn:INV-ETTN",
            dry_run=True,
            approved_by=None,
        )
    ]
    assert writer.commands[0].vendor_bill.invoice_number == "INV-1"


@pytest.mark.asyncio
async def test_vendor_bill_strategy_maps_existing_write_result_to_already_exists() -> None:
    strategy = VendorBillStrategy(
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=FakeVendorBillWriter(
            VendorBillWriteResult(
                status="existing",
                idempotency_key="ettn:INV-ETTN",
                external_id=99,
                warnings=("Existing draft found.",),
            )
        ),
    )

    result = await strategy.execute(_command(), _rule_result(warnings=()))

    assert result.success is True
    assert result.status == "already_exists"
    assert result.vendor_bill_id == 99
    assert result.warnings == ("Existing draft found.",)


@pytest.mark.asyncio
async def test_vendor_bill_strategy_propagates_builder_error() -> None:
    strategy = VendorBillStrategy(
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=FakeVendorBillWriter(),
    )

    with pytest.raises(Exception) as exc_info:
        await strategy.execute(_command(), _rule_result(product_match=_product_match(ProductMatchStatus.NOT_FOUND)))

    assert "Product mapping for line 1 is not matched." in exc_info.value.safe_message


@pytest.mark.asyncio
async def test_vendor_bill_strategy_rejects_missing_rule_outputs() -> None:
    strategy = VendorBillStrategy(
        vendor_bill_builder=VendorBillBuilder(),
        vendor_bill_writer=FakeVendorBillWriter(),
    )

    with pytest.raises(UnsupportedWorkflowError) as exc_info:
        await strategy.execute(_command(), RuleEvaluationResult(workflow="vendor_bill"))

    assert exc_info.value.safe_message == "Vendor Bill workflow requires matching rule outputs."


def test_decision_dtos_are_immutable() -> None:
    rule_result = _rule_result()
    decision_result = DecisionResult(
        success=True,
        invoice_id="INV-ETTN",
        workflow="vendor_bill",
        strategy="vendor_bill",
        status="dry_run",
    )

    with pytest.raises(FrozenInstanceError):
        rule_result.workflow = "expense"
    with pytest.raises(FrozenInstanceError):
        decision_result.status = "failed"


def test_decision_engine_exports() -> None:
    assert app.application.DecisionEngine is DecisionEngine
    assert app.application.WorkflowStrategyResolver is WorkflowStrategyResolver
    assert app.application.VendorBillStrategy is VendorBillStrategy
    assert hasattr(RuleEngine, "evaluate")


def test_decision_engine_does_not_import_infrastructure_or_future_engines() -> None:
    forbidden_terms = (
        "app.connectors",
        "app.models",
        "app.db",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "zeep",
        "ai_advisor",
        "ollama",
        "account.move",
        "OdooJson2Client",
    )
    for path in Path("app/application/decision").rglob("*.py"):
        content = path.read_text()
        for forbidden in forbidden_terms:
            assert forbidden not in content, f"{path} depends on {forbidden}"


class FakeRuleEngine:
    def __init__(self, result: RuleEvaluationResult | Exception) -> None:
        self.result = result
        self.commands: list[ImportInvoiceCommand] = []

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        self.commands.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeWorkflowStrategy:
    def __init__(self, workflow: str, error: Exception | None = None) -> None:
        self.workflow = workflow
        self.name = f"fake_{workflow}"
        self.error = error
        self.calls: list[tuple[ImportInvoiceCommand, RuleEvaluationResult]] = []

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        self.calls.append((command, rule_result))
        if self.error is not None:
            raise self.error
        return DecisionResult(
            success=True,
            invoice_id=command.invoice.header.ettn or command.invoice.header.invoice_uuid,
            workflow=rule_result.workflow,
            strategy=self.name,
            status="created",
            vendor_bill_id=42,
            warnings=("strategy ok",),
        )


class FakeVendorBillWriter:
    def __init__(self, result: VendorBillWriteResult | None = None) -> None:
        self.result = result or VendorBillWriteResult(status="dry_run", idempotency_key="ettn:INV-ETTN")
        self.commands: list[VendorBillWriteCommand] = []

    async def write_vendor_bill(self, command: VendorBillWriteCommand) -> VendorBillWriteResult:
        self.commands.append(command)
        return self.result


def _command() -> ImportInvoiceCommand:
    return ImportInvoiceCommand(invoice=_invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)


def _invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
            issue_date=date(2026, 7, 30),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Line 1",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_code="NIU",
                unit_price=Decimal("50"),
                taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
            ),
        ),
    )


def _rule_result(
    *,
    warnings: tuple[str, ...] = ("rules ok",),
    product_match: InvoiceProductMatchResult | None = None,
) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow="vendor_bill",
        partner_match=_partner_match(),
        product_match=product_match or _product_match(ProductMatchStatus.MATCHED),
        tax_match=_tax_match(),
        warnings=warnings,
    )


def _partner_match() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=10,
        matched_by="tax_number",
        reason="Unique supplier partner match.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_match(status: ProductMatchStatus) -> InvoiceProductMatchResult:
    product_id = 20 if status is ProductMatchStatus.MATCHED else None
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=status,
                    line_number="1",
                    product_id=product_id,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if product_id is not None else None,
                    reason="Product match result.",
                    candidate_count=1 if product_id is not None else 0,
                    confidence=Decimal("1.00") if product_id is not None else None,
                ),
            ),
        )
    )


def _tax_match() -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=30,
                    company_id=7,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="Exact tax match.",
                    candidate_count=1,
                ),
            ),
        )
    )
