"""Deterministic operating-expense classification (P0-3C3 / PR 2).

Covers the typed match result, the matching engine, the product-identifier-free
guard, and the single new ``DeterministicRuleEngine`` branch. No builder, evidence,
execution, or production-composition behavior is exercised here.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine, ManualReviewStrategy, WorkflowStrategyResolver
from app.application.dto import DecisionResult, RuleEvaluationResult
from app.application.expense_mapping import (
    NullOperatingExpenseMatcher,
    OperatingExpenseMatchingEngine,
    OperatingExpenseMatchResult,
    OperatingExpenseMatchStatus,
    invoice_is_product_identifier_free,
)
from app.application.expense_mapping.exceptions import (
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
)
from app.application.rules.deterministic import (
    DIRECT_VENDOR_BILL_RULE_ID,
    MANUAL_REVIEW_RULE_ID,
    OPERATING_EXPENSE_VENDOR_BILL_RULE_ID,
    DeterministicRuleEngine,
)
from app.application.workflow import ManualReviewReasonCode, WorkflowType
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
from app.models.operating_expense_mapping import OperatingExpenseMappingRecord
from app.persistence import SqlAlchemyOperatingExpenseMappingRepository
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

COMPANY_ID = 1
PARTNER_ID = 501
EXPENSE_ACCOUNT_ID = 9001
EXPENSE_CATEGORY = "OFFICE_BUILDING_EXPENSE"


# --------------------------------------------------------------------------- fixtures / builders


@pytest.fixture()
def session() -> Session:
    factory = sessionmaker(bind=create_engine("sqlite:///:memory:"))
    with factory() as db_session:
        Base.metadata.create_all(db_session.get_bind())
        yield db_session


def _line(
    line_number: str = "1",
    *,
    buyer_item_code: str | None = None,
    seller_item_code: str | None = None,
    barcode: str | None = None,
) -> InvoiceLine:
    return InvoiceLine(
        line_number=line_number,
        description="Common area fee",
        buyer_item_code=buyer_item_code,
        seller_item_code=seller_item_code,
        barcode=barcode,
        quantity=Decimal("1"),
        unit_code="C62",
        unit_price=Decimal("83.33"),
        taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
    )


def _invoice(*, lines: tuple[InvoiceLine, ...] | None = None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKM2026000004218",
            invoice_uuid="CD3E75D2",
            ettn="CD3E75D2",
            issue_date=date(2026, 8, 18),
            currency_code="TRY",
        ),
        supplier=Party(name="Akyasam", tax_number="0430367181"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=lines if lines is not None else (_line(),),
    )


def _command(invoice: InternalInvoice | None = None, *, company_id: int | None = COMPANY_ID) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(
        invoice=invoice or _invoice(),
        idempotency_key="ettn:CD3E75D2",
        company_id=company_id,
    )


def _partner_match(
    status: PartnerMatchStatus = PartnerMatchStatus.MATCHED, *, partner_id: int | None = PARTNER_ID
) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if matched else None,
        matched_by="tax_number" if matched else None,
        reason="Unique supplier partner match by tax number."
        if matched
        else "No active deterministic supplier partner candidate found.",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _identifier_free_product_match(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="At least one deterministic product identifier is required.",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _matched_product_match(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED,
                    line_number=line.line_number,
                    product_id=2001,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code",
                    reason="matched",
                    candidate_count=1,
                    confidence=Decimal("1.00"),
                ),
            )
            for line in invoice.lines
        )
    )


def _product_status_match(invoice: InternalInvoice, status: ProductMatchStatus) -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=status,
                    line_number=line.line_number,
                    product_id=None,
                    default_code="SKU-1",
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="not found" if status is ProductMatchStatus.NOT_FOUND else "ambiguous",
                    candidate_count=0 if status is ProductMatchStatus.NOT_FOUND else 2,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _tax_match(invoice: InternalInvoice, status: TaxMatchStatus = TaxMatchStatus.MATCHED) -> InvoiceTaxMappingResult:
    matched = status is TaxMatchStatus.MATCHED
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=status,
                    tax_id=34 if matched else None,
                    company_id=COMPANY_ID if matched else None,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if matched else None,
                    confidence=Decimal("1.00") if matched else None,
                    reason="matched" if matched else "unmatched",
                    candidate_count=1 if matched else (2 if status is TaxMatchStatus.MULTIPLE_MATCHES else 0),
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


class _FakeMatcher:
    """Stands in for the partner / product / tax collaborators (match_invoice / map_invoice)."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[object, int | None]] = []

    def match_invoice(self, invoice: object, *, company_id: int | None = None) -> object:
        self.calls.append((invoice, company_id))
        return self.result

    def map_invoice(self, invoice: object, *, company_id: int | None = None) -> object:
        self.calls.append((invoice, company_id))
        return self.result


class _FakeOperatingExpenseMatcher:
    def __init__(self, result: OperatingExpenseMatchResult) -> None:
        self.result = result
        self.calls: list[tuple[object, int | None, object]] = []

    def match_invoice(self, invoice, *, company_id, partner_match) -> OperatingExpenseMatchResult:
        self.calls.append((invoice, company_id, partner_match))
        return self.result


class _FakeMappingRepository:
    def __init__(self, *, mapping: object | None = None, integrity_error: bool = False) -> None:
        self._mapping = mapping
        self._integrity_error = integrity_error

    def find_for_supplier(self, *, company_id: int, vendor_partner_id: int):
        if self._integrity_error:
            raise OperatingExpenseMappingDataIntegrityError("two enabled mappings")
        return self._mapping


def _matched_expense_result() -> OperatingExpenseMatchResult:
    return OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="Exact company and supplier partner operating-expense mapping.",
        candidate_count=1,
        mapping_id=7,
        company_id=COMPANY_ID,
        vendor_partner_id=PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_ID,
        expense_category=EXPENSE_CATEGORY,
        matched_by="company_partner",
        confidence=Decimal("1.00"),
    )


def _engine(
    *,
    invoice: InternalInvoice,
    partner_match: PartnerMatchResult | None = None,
    product_match: InvoiceProductMatchResult | None = None,
    tax_match: InvoiceTaxMappingResult | None = None,
    operating_expense_match: OperatingExpenseMatchResult | None = None,
) -> DeterministicRuleEngine:
    partner_match = partner_match or _partner_match()
    return DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(partner_match),
        product_matcher=_FakeMatcher(product_match if product_match is not None else _matched_product_match(invoice)),
        tax_mapper=_FakeMatcher(tax_match if tax_match is not None else _tax_match(invoice)),
        operating_expense_matcher=_FakeOperatingExpenseMatcher(
            operating_expense_match
            or OperatingExpenseMatchResult(status=OperatingExpenseMatchStatus.NOT_FOUND, reason="none")
        ),
    )


# --------------------------------------------------------------------------- DTO contract


@pytest.mark.parametrize(
    "status",
    [
        OperatingExpenseMatchStatus.NOT_FOUND,
        OperatingExpenseMatchStatus.MULTIPLE_MATCHES,
        OperatingExpenseMatchStatus.INVALID_INPUT,
    ],
)
def test_non_matched_result_must_not_expose_mapping(status: OperatingExpenseMatchStatus) -> None:
    with pytest.raises(OperatingExpenseMappingContractError):
        OperatingExpenseMatchResult(status=status, reason="x", expense_account_id=9001)


def test_matched_result_requires_full_mapping() -> None:
    with pytest.raises(OperatingExpenseMappingContractError):
        OperatingExpenseMatchResult(status=OperatingExpenseMatchStatus.MATCHED, reason="x", mapping_id=1)


# --------------------------------------------------------------------------- matching engine (10-13)


def test_engine_returns_matched_for_exact_enabled_mapping(session: Session) -> None:
    session.add(
        OperatingExpenseMappingRecord(
            company_id=COMPANY_ID,
            vendor_partner_id=PARTNER_ID,
            expense_account_id=EXPENSE_ACCOUNT_ID,
            expense_category=EXPENSE_CATEGORY,
            enabled=True,
        )
    )
    session.flush()
    engine = OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session))

    result = engine.match_invoice(_invoice(), company_id=COMPANY_ID, partner_match=_partner_match())

    assert result.status is OperatingExpenseMatchStatus.MATCHED
    assert result.company_id == COMPANY_ID
    assert result.vendor_partner_id == PARTNER_ID
    assert result.expense_account_id == EXPENSE_ACCOUNT_ID
    assert result.expense_category == EXPENSE_CATEGORY
    assert result.matched_by == "company_partner"
    assert result.confidence == Decimal("1.00")
    assert result.mapping_id is not None


def test_engine_returns_not_found_without_mapping(session: Session) -> None:
    engine = OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session))

    result = engine.match_invoice(_invoice(), company_id=COMPANY_ID, partner_match=_partner_match())

    assert result.status is OperatingExpenseMatchStatus.NOT_FOUND
    assert result.expense_account_id is None


@pytest.mark.parametrize("company_id", [None, 0, -3, "1"])
def test_engine_returns_invalid_input_for_bad_company_id(company_id: object) -> None:
    engine = OperatingExpenseMatchingEngine(_FakeMappingRepository())

    result = engine.match_invoice(_invoice(), company_id=company_id, partner_match=_partner_match())  # type: ignore[arg-type]

    assert result.status is OperatingExpenseMatchStatus.INVALID_INPUT


def test_engine_returns_invalid_input_for_non_invoice() -> None:
    engine = OperatingExpenseMatchingEngine(_FakeMappingRepository())

    result = engine.match_invoice(object(), company_id=COMPANY_ID, partner_match=_partner_match())

    assert result.status is OperatingExpenseMatchStatus.INVALID_INPUT


@pytest.mark.parametrize(
    "partner_match",
    [_partner_match(PartnerMatchStatus.NOT_FOUND), _partner_match(PartnerMatchStatus.MULTIPLE_MATCHES), None],
)
def test_engine_fails_closed_when_partner_not_matched(partner_match: PartnerMatchResult | None) -> None:
    engine = OperatingExpenseMatchingEngine(_FakeMappingRepository(mapping=object()))

    result = engine.match_invoice(_invoice(), company_id=COMPANY_ID, partner_match=partner_match)

    assert result.status in {OperatingExpenseMatchStatus.NOT_FOUND, OperatingExpenseMatchStatus.INVALID_INPUT}
    assert result.expense_account_id is None


def test_engine_maps_repository_integrity_error_to_multiple_matches() -> None:
    engine = OperatingExpenseMatchingEngine(_FakeMappingRepository(integrity_error=True))

    result = engine.match_invoice(_invoice(), company_id=COMPANY_ID, partner_match=_partner_match())

    assert result.status is OperatingExpenseMatchStatus.MULTIPLE_MATCHES
    assert result.expense_account_id is None


# --------------------------------------------------------------------------- identifier-free guard (8)


def test_identifier_free_guard_true_when_no_line_carries_identifiers() -> None:
    assert invoice_is_product_identifier_free(_invoice()) is True
    assert invoice_is_product_identifier_free(_invoice(lines=(_line(buyer_item_code="  "),))) is True


@pytest.mark.parametrize(
    "line",
    [
        _line(buyer_item_code="SKU-1"),
        _line(seller_item_code="V-9"),
        _line(barcode="8690000000001"),
    ],
)
def test_identifier_free_guard_false_when_any_identifier_present(line: InvoiceLine) -> None:
    assert invoice_is_product_identifier_free(_invoice(lines=(_line(), line))) is False


def test_identifier_free_guard_false_for_empty_invoice() -> None:
    assert invoice_is_product_identifier_free(_invoice(lines=())) is False


# --------------------------------------------------------------------------- rule engine branch (1-9, 14)


def test_normal_product_vendor_bill_unchanged() -> None:
    invoice = _invoice(lines=(_line(buyer_item_code="SKU-1"),))
    result = _engine(
        invoice=invoice,
        product_match=_matched_product_match(invoice),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID
    assert result.operating_expense_match is not None


def test_product_match_wins_even_when_expense_mapping_exists() -> None:
    invoice = _invoice(lines=(_line(buyer_item_code="SKU-1"),))
    result = _engine(
        invoice=invoice,
        product_match=_matched_product_match(invoice),
        operating_expense_match=_matched_expense_result(),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == DIRECT_VENDOR_BILL_RULE_ID


def test_identifier_free_plus_expense_mapping_selects_operating_expense_rule() -> None:
    invoice = _invoice()
    result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=_matched_expense_result(),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.VENDOR_BILL
    assert result.matched_rule == OPERATING_EXPENSE_VENDOR_BILL_RULE_ID
    assert result.operating_expense_match is not None
    assert result.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID


def test_identifier_free_without_mapping_is_manual_review_with_explicit_reason() -> None:
    invoice = _invoice()
    result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.NOT_FOUND, reason="no mapping"
        ),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert result.matched_rule == MANUAL_REVIEW_RULE_ID
    codes = {reason.code for reason in result.workflow_decision.manual_review.reasons}
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED in codes
    assert ManualReviewReasonCode.PRODUCT_IDENTIFIER_MISSING not in codes


def test_identifier_free_with_ambiguous_mapping_is_manual_review() -> None:
    invoice = _invoice()
    result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.MULTIPLE_MATCHES, reason="two", candidate_count=2
        ),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    codes = {reason.code for reason in result.workflow_decision.manual_review.reasons}
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_AMBIGUOUS in codes


def test_product_identifier_present_but_not_found_stays_manual_review_despite_mapping() -> None:
    invoice = _invoice(lines=(_line(buyer_item_code="SKU-1"),))
    result = _engine(
        invoice=invoice,
        product_match=_product_status_match(invoice, ProductMatchStatus.NOT_FOUND),
        operating_expense_match=_matched_expense_result(),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    codes = {reason.code for reason in result.workflow_decision.manual_review.reasons}
    assert ManualReviewReasonCode.PRODUCT_NOT_FOUND in codes
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED not in codes


def test_product_identifier_present_but_ambiguous_stays_manual_review() -> None:
    invoice = _invoice(lines=(_line(buyer_item_code="SKU-1"),))
    result = _engine(
        invoice=invoice,
        product_match=_product_status_match(invoice, ProductMatchStatus.MULTIPLE_MATCHES),
        operating_expense_match=_matched_expense_result(),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW


def test_supplier_not_matched_with_valid_expense_is_manual_review() -> None:
    invoice = _invoice()
    result = _engine(
        invoice=invoice,
        partner_match=_partner_match(PartnerMatchStatus.NOT_FOUND),
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.NOT_FOUND, reason="partner unmatched"
        ),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    codes = {reason.code for reason in result.workflow_decision.manual_review.reasons}
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND in codes


@pytest.mark.parametrize("tax_status", [TaxMatchStatus.NOT_FOUND, TaxMatchStatus.MULTIPLE_MATCHES])
def test_tax_mismatch_is_manual_review_even_when_expense_matched(tax_status: TaxMatchStatus) -> None:
    invoice = _invoice()
    result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        tax_match=_tax_match(invoice, tax_status),
        operating_expense_match=_matched_expense_result(),
    ).evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    codes = {reason.code for reason in result.workflow_decision.manual_review.reasons}
    assert codes & {ManualReviewReasonCode.TAX_NOT_FOUND, ManualReviewReasonCode.TAX_AMBIGUOUS}


def test_rule_evaluation_result_carries_operating_expense_match() -> None:
    invoice = _invoice()
    expense = _matched_expense_result()
    result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=expense,
    ).evaluate(_command(invoice))

    assert result.operating_expense_match is expense


# --------------------------------------------------------------------------- Null matcher / defaults


def test_default_engine_uses_null_matcher_and_keeps_manual_review_for_identifier_free() -> None:
    invoice = _invoice()
    engine = DeterministicRuleEngine(
        partner_matcher=_FakeMatcher(_partner_match()),
        product_matcher=_FakeMatcher(_identifier_free_product_match(invoice)),
        tax_mapper=_FakeMatcher(_tax_match(invoice)),
    )

    result = engine.evaluate(_command(invoice))

    assert result.workflow is WorkflowType.MANUAL_REVIEW
    assert result.operating_expense_match is not None
    assert result.operating_expense_match.status is OperatingExpenseMatchStatus.NOT_FOUND


def test_null_operating_expense_matcher_always_not_found() -> None:
    result = NullOperatingExpenseMatcher().match_invoice(_invoice(), company_id=1, partner_match=_partner_match())
    assert result.status is OperatingExpenseMatchStatus.NOT_FOUND


# --------------------------------------------------------------------------- DecisionEngine propagation (15)


class _StubRuleEngine:
    def __init__(self, result: RuleEvaluationResult) -> None:
        self._result = result

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self._result


async def test_decision_engine_propagates_exact_operating_expense_match() -> None:
    invoice = _invoice()
    # A real MANUAL_REVIEW rule result that also carries the operating-expense match.
    rule_result = _engine(
        invoice=invoice,
        product_match=_identifier_free_product_match(invoice),
        operating_expense_match=OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.NOT_FOUND, reason="no mapping"
        ),
    ).evaluate(_command(invoice))
    assert rule_result.workflow is WorkflowType.MANUAL_REVIEW
    expense = rule_result.operating_expense_match

    engine = DecisionEngine(
        rule_engine=_StubRuleEngine(rule_result),
        strategy_resolver=WorkflowStrategyResolver([ManualReviewStrategy()]),
    )

    decision: DecisionResult = await engine.decide(_command(invoice))

    assert decision.operating_expense_match is expense
