from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import app.application
from app.application.commands import ImportInvoiceCommand
from app.application.rules import (
    DIRECT_VENDOR_BILL_RULE_ID,
    DeterministicRuleEngine,
    PartnerRuleEvaluationError,
    ProductRuleEvaluationError,
    TaxRuleEvaluationError,
)
from app.application.workflow import WorkflowType
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


def test_successful_vendor_bill_rule_evaluation() -> None:
    engine = _engine()
    command = _command()

    result = engine.evaluate(command)

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.workflow_decision.workflow is WorkflowType.VENDOR_BILL
    assert result.workflow_decision.matched_rule == DIRECT_VENDOR_BILL_RULE_ID
    assert result.workflow_decision.explanation == (
        "Supplier, products and taxes matched deterministically; Vendor Bill workflow selected."
    )
    assert result.partner_match == engine.partner_matcher.result
    assert result.product_match == engine.product_matcher.result
    assert result.tax_match == engine.tax_mapper.result
    assert result.warnings == ()
    assert result.errors == ()
    assert engine.partner_matcher.calls == [(command.invoice, 7)]
    assert engine.product_matcher.calls == [(command.invoice, 7)]
    assert engine.tax_mapper.calls == [(command.invoice, 7)]


def test_missing_supplier_fails_safely_before_vendor_bill_selection() -> None:
    engine = _engine(partner_match=_partner_match(PartnerMatchStatus.NOT_FOUND, partner_id=None))

    with pytest.raises(PartnerRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message.startswith("Supplier was not matched deterministically:")
    assert engine.product_matcher.calls == []
    assert engine.tax_mapper.calls == []


def test_ambiguous_supplier_fails_safely() -> None:
    engine = _engine(
        partner_match=_partner_match(
            PartnerMatchStatus.MULTIPLE_MATCHES,
            partner_id=None,
            reason="Multiple active supplier partner candidates found by tax number.",
            candidate_count=2,
        )
    )

    with pytest.raises(PartnerRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message == (
        "Supplier match is ambiguous: Multiple active supplier partner candidates found by tax number."
    )


def test_missing_product_fails_safely() -> None:
    engine = _engine(product_match=_product_match(ProductMatchStatus.NOT_FOUND, product_id=None))

    with pytest.raises(ProductRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message.startswith("Product was not matched deterministically for line 1:")


def test_ambiguous_product_fails_safely() -> None:
    engine = _engine(
        product_match=_product_match(
            ProductMatchStatus.MULTIPLE_MATCHES,
            product_id=None,
            reason="Multiple active product candidates found by default_code.",
            candidate_count=2,
        )
    )

    with pytest.raises(ProductRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message == (
        "Product match is ambiguous for line 1: Multiple active product candidates found by default_code."
    )


def test_partial_invoice_line_product_matching_fails_as_incomplete_result() -> None:
    invoice = _invoice(
        lines=(
            _line("1", buyer_item_code="SKU-1"),
            _line("2", buyer_item_code="SKU-2"),
        )
    )
    engine = _engine(product_match=_product_match(ProductMatchStatus.MATCHED, product_id=20))

    with pytest.raises(ProductRuleEvaluationError) as exc_info:
        engine.evaluate(_command(invoice=invoice))

    assert exc_info.value.safe_message == "Product matching result is incomplete for invoice lines."


def test_tax_mapping_failure_fails_safely() -> None:
    engine = _engine(tax_match=_tax_match(TaxMatchStatus.NOT_FOUND, tax_id=None))

    with pytest.raises(TaxRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message.startswith("Tax was not mapped deterministically for line 1:")


def test_tax_mapping_errors_are_propagated_as_tax_rule_error() -> None:
    engine = _engine(tax_match=InvoiceTaxMappingResult(errors=("Invoice has no lines to map.",)))

    with pytest.raises(TaxRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message == "Tax mapping failed: Invoice has no lines to map."


def test_warning_propagation_uses_rule_result_for_component_warnings() -> None:
    engine = _engine(
        product_match=_product_match(ProductMatchStatus.MATCHED, product_id=20, warnings=("Product warning.",)),
        tax_match=_tax_match(TaxMatchStatus.MATCHED, tax_id=30, warnings=("Tax warning.",)),
    )

    result = engine.evaluate(_command())

    assert result.workflow_decision.warnings == ()
    assert result.warnings == ("Product warning.", "Tax warning.")


def test_mapper_exception_is_translated_without_raw_exception_leakage() -> None:
    engine = _engine(tax_mapper=FakeTaxMapper(RuntimeError("raw database timeout")))

    with pytest.raises(TaxRuleEvaluationError) as exc_info:
        engine.evaluate(_command())

    assert exc_info.value.safe_message == "Tax mapping failed."
    assert "raw database timeout" not in exc_info.value.safe_message


def test_rule_evaluation_result_is_immutable() -> None:
    result = _engine().evaluate(_command())

    with pytest.raises(FrozenInstanceError):
        result.workflow_decision.workflow = WorkflowType.EXPENSE


def test_rule_engine_is_exported_from_application_package() -> None:
    assert app.application.DeterministicRuleEngine is DeterministicRuleEngine


def test_rule_engine_does_not_import_strategy_erp_write_or_future_engines() -> None:
    source = "\n".join(path.read_text() for path in Path("app/application/rules").rglob("*.py"))
    forbidden_terms = (
        "WorkflowStrategy",
        "VendorBillStrategy",
        "VendorBillWriter",
        "VendorBillBuilder",
        "app.connectors",
        "app.models",
        "app.db",
        "fastapi",
        "sqlalchemy",
        "httpx",
        "zeep",
        "OdooJson2Client",
        "OdooVendorBillWriter",
        "create_draft_vendor_bill",
        "write_vendor_bill",
        "action_post",
        "ai_advisor",
        "ollama",
        "fuzzy",
        "levenshtein",
        "embedding",
        "similarity",
    )

    for forbidden in forbidden_terms:
        assert forbidden not in source, f"Rule Engine depends on {forbidden}"


class RuleEngineFixture(DeterministicRuleEngine):
    partner_matcher: FakePartnerMatcher
    product_matcher: FakeProductMatcher
    tax_mapper: FakeTaxMapper


class FakePartnerMatcher:
    def __init__(self, result: PartnerMatchResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[InternalInvoice, int | None]] = []

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> PartnerMatchResult:
        self.calls.append((invoice, company_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeProductMatcher:
    def __init__(self, result: InvoiceProductMatchResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[InternalInvoice, int | None]] = []

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceProductMatchResult:
        self.calls.append((invoice, company_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeTaxMapper:
    def __init__(self, result: InvoiceTaxMappingResult | Exception) -> None:
        self.result = result
        self.calls: list[tuple[InternalInvoice, int | None]] = []

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        self.calls.append((invoice, company_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _engine(
    *,
    partner_match: PartnerMatchResult | None = None,
    product_match: InvoiceProductMatchResult | None = None,
    tax_match: InvoiceTaxMappingResult | None = None,
    tax_mapper: FakeTaxMapper | None = None,
) -> RuleEngineFixture:
    partner_matcher = FakePartnerMatcher(partner_match or _partner_match())
    product_matcher = FakeProductMatcher(product_match or _product_match(ProductMatchStatus.MATCHED, product_id=20))
    mapper = tax_mapper or FakeTaxMapper(tax_match or _tax_match(TaxMatchStatus.MATCHED, tax_id=30))
    engine = RuleEngineFixture(
        partner_matcher=partner_matcher,
        product_matcher=product_matcher,
        tax_mapper=mapper,
    )
    engine.partner_matcher = partner_matcher
    engine.product_matcher = product_matcher
    engine.tax_mapper = mapper
    return engine


def _command(*, invoice: InternalInvoice | None = None) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(invoice=invoice or _invoice(), idempotency_key="ettn:INV-ETTN", company_id=7)


def _invoice(*, lines: tuple[InvoiceLine, ...] | None = None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="INV-1",
            invoice_uuid="INV-UUID",
            ettn="INV-ETTN",
            issue_date=date(2026, 8, 2),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer"),
        totals=MonetaryTotals(payable_amount=Decimal("120")),
        lines=lines or (_line("1", buyer_item_code="SKU-1"),),
    )


def _line(line_number: str, *, buyer_item_code: str) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description="Line",
        buyer_item_code=buyer_item_code,
        quantity=Decimal("2"),
        unit_code="NIU",
        unit_price=Decimal("50"),
        taxes=(Tax(tax_type="VAT", rate=Decimal("20")),),
    )


def _partner_match(
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED,
    *,
    partner_id: int | None = 10,
    reason: str = "Unique supplier partner match by tax number.",
    candidate_count: int = 1,
) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id,
        matched_by="tax_number" if status is PartnerMatchStatus.MATCHED else None,
        reason=reason,
        candidate_count=candidate_count,
        confidence=Decimal("1.00") if status is PartnerMatchStatus.MATCHED else None,
    )


def _product_match(
    status: ProductMatchStatus,
    *,
    product_id: int | None,
    reason: str = "Product match result.",
    candidate_count: int = 1,
    warnings: tuple[str, ...] = (),
) -> InvoiceProductMatchResult:
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
                    matched_by="default_code" if status is ProductMatchStatus.MATCHED else None,
                    reason=reason,
                    candidate_count=candidate_count,
                    confidence=Decimal("1.00") if status is ProductMatchStatus.MATCHED else None,
                ),
            ),
        ),
        warnings=warnings,
    )


def _tax_match(
    status: TaxMatchStatus,
    *,
    tax_id: int | None,
    reason: str = "Tax match result.",
    candidate_count: int = 1,
    warnings: tuple[str, ...] = (),
) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=status,
                    tax_id=tax_id,
                    company_id=7,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if status is TaxMatchStatus.MATCHED else None,
                    confidence=Decimal("1.00") if status is TaxMatchStatus.MATCHED else None,
                    reason=reason,
                    candidate_count=candidate_count,
                ),
            ),
        ),
        warnings=warnings,
    )
