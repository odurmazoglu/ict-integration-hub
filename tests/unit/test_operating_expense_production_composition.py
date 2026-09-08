"""Production import composition wires the real deterministic operating-expense matcher.

The composition factory ``build_uyumsoft_canonical_invoice_importer`` cannot run a full
import in-process (its Odoo reads go through a sync adapter that refuses a running event
loop), so it is inspected structurally. Behavior is proven end to end through the real
``ImportInvoiceUseCase`` + real Workbench persistence + the real
``OperatingExpenseMatchingEngine`` bound to the real ``SqlAlchemyOperatingExpenseMappingRepository``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.expense_mapping import OperatingExpenseMatchingEngine, OperatingExpenseMatchStatus
from app.application.rules.deterministic import DeterministicRuleEngine
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import ReviewItemCreationService
from app.application.workflow import WorkflowType
from app.composition import build_uyumsoft_canonical_invoice_importer
from app.core.config import Settings
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
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import (
    SqlAlchemyImportHistory,
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.execution_source_invoice_reader import _operating_expense_match_from_data
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType

COMPANY_ID = 1
PARTNER_ID = 101
EXPENSE_ACCOUNT_ID = 9001
PURCHASE_TAX_ID = 34


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


# --------------------------------------------------------------------------- structural: the factory wires it


class _NoLoopOdooClient:
    async def search_read(self, *, model, domain, fields, limit=20, offset=0):
        return []

    async def read_model_field_metadata(self, *, model, field_name):
        return []


def test_composition_factory_wires_real_session_backed_matcher(session: Session) -> None:
    importer = build_uyumsoft_canonical_invoice_importer(
        session=session,
        settings=Settings(),
        uyumsoft_client=object(),
        storage=object(),
        odoo_client=_NoLoopOdooClient(),  # type: ignore[arg-type]
    )
    use_case = importer._import_use_case_factory()
    matcher = use_case._decision_engine._rule_engine._operating_expense_matcher
    assert isinstance(matcher, OperatingExpenseMatchingEngine)
    assert isinstance(matcher._repository, SqlAlchemyOperatingExpenseMappingRepository)
    assert matcher._repository._session is session


def test_composition_source_replaces_null_matcher_with_real_matcher() -> None:
    source = Path("app/composition/imports.py").read_text(encoding="utf-8")
    assert "OperatingExpenseMatchingEngine(" in source
    assert "SqlAlchemyOperatingExpenseMappingRepository(session)" in source
    assert "operating_expense_matcher=operating_expense_matcher" in source
    assert "NullOperatingExpenseMatcher" not in source


def test_null_matcher_still_exists_for_isolated_contexts() -> None:
    from app.application.expense_mapping import NullOperatingExpenseMatcher

    result = NullOperatingExpenseMatcher().match_invoice(object(), company_id=1, partner_match=None)
    assert result.status is OperatingExpenseMatchStatus.NOT_FOUND


# --------------------------------------------------------------------------- behavioral: real matcher + real repo


def _invoice(*, ettn: str = "OPEX-COMPO", buyer_item_code: str | None = None) -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKM-COMPO",
            invoice_uuid=ettn,
            ettn=ettn,
            issue_date=date(2026, 8, 18),
            currency_code="TRY",
        ),
        supplier=Party(name="Akyasam", tax_number="0430367181"),
        customer=Party(name="ICT", tax_number="4651205941"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="KART BEDELI",
                buyer_item_code=buyer_item_code,
                seller_item_code=None,
                barcode=None,
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _partner(invoice: InternalInvoice) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=PARTNER_ID,
        matched_by="tax_number",
        reason="matched",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _products(invoice: InternalInvoice) -> InvoiceProductMatchResult:
    has_identifier = any(line.buyer_item_code or line.seller_item_code or line.barcode for line in invoice.lines)
    status = ProductMatchStatus.NOT_FOUND if has_identifier else ProductMatchStatus.INVALID_INPUT
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=status,
                    line_number=line.line_number,
                    product_id=None,
                    default_code=None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by=None,
                    reason="At least one deterministic product identifier is required."
                    if status is ProductMatchStatus.INVALID_INPUT
                    else "not found",
                    candidate_count=0,
                    confidence=None,
                ),
            )
            for line in invoice.lines
        )
    )


def _taxes(invoice: InternalInvoice) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=tuple(
            InvoiceTaxLineResult(
                line_number=line.line_number,
                tax_index=idx,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=PURCHASE_TAX_ID,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="matched",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for idx, _t in enumerate(line.taxes)
        )
    )


class _FixedMatcher:
    def __init__(self, result) -> None:
        self._result = result

    def match_invoice(self, invoice, *, company_id=None):
        return self._result

    def map_invoice(self, invoice, *, company_id=None):
        return self._result


def _add_mapping(session: Session, *, company_id: int = COMPANY_ID, enabled: bool = True) -> None:
    session.add(
        OperatingExpenseMappingRecord(
            company_id=company_id,
            vendor_partner_id=PARTNER_ID,
            expense_account_id=EXPENSE_ACCOUNT_ID,
            expense_category="OFFICE_OPERATING_EXPENSE",
            enabled=enabled,
        )
    )
    session.flush()


def _use_case(session: Session, invoice: InternalInvoice) -> ImportInvoiceUseCase:
    """The real ImportInvoiceUseCase + Workbench persistence, with the real operating-expense
    matcher bound to the real session-backed repository (as the production factory does)."""
    rule_engine = DeterministicRuleEngine(
        partner_matcher=_FixedMatcher(_partner(invoice)),
        product_matcher=_FixedMatcher(_products(invoice)),
        tax_mapper=_FixedMatcher(_taxes(invoice)),
        operating_expense_matcher=OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session)),
    )
    decision_engine = DecisionEngine(
        rule_engine=rule_engine,
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )
    return ImportInvoiceUseCase(
        import_history=SqlAlchemyImportHistory(session),
        decision_engine=decision_engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


async def _run(session: Session, invoice: InternalInvoice, *, company_id: int = COMPANY_ID):
    return await _use_case(session, invoice).execute(
        ImportInvoiceCommand(
            invoice=invoice,
            idempotency_key=f"compo:{company_id}:{invoice.header.ettn}",
            company_id=company_id,
        )
    )


async def test_enabled_mapping_drives_vendor_bill_and_pins_stage1_evidence(session: Session) -> None:
    _add_mapping(session)
    invoice = _invoice()

    result = await _run(session, invoice)

    assert result.review_required is True
    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()
    assert review_item.workflow == WorkflowType.VENDOR_BILL.value

    stage1 = session.execute(select(WorkbenchReviewExecutionEvidence)).scalar_one()
    match = _operating_expense_match_from_data(stage1.operating_expense_match)
    assert match is not None
    assert match.status is OperatingExpenseMatchStatus.MATCHED
    assert match.expense_account_id == EXPENSE_ACCOUNT_ID
    assert match.vendor_partner_id == PARTNER_ID

    source = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=review_item.review_id, company_id=COMPANY_ID, expected_version=1
    )
    assert source.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID


async def test_no_mapping_stays_manual_review_with_no_executable_evidence(session: Session) -> None:
    await _run(session, _invoice())

    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()
    assert review_item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert session.execute(select(WorkbenchReviewExecutionEvidence)).scalars().all() == []


async def test_unknown_sku_is_never_rescued_by_expense_mapping(session: Session) -> None:
    _add_mapping(session)

    await _run(session, _invoice(buyer_item_code="UNKNOWN-SKU"))

    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()
    assert review_item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert session.execute(select(WorkbenchReviewExecutionEvidence)).scalars().all() == []


async def test_mapping_is_company_isolated(session: Session) -> None:
    _add_mapping(session, company_id=COMPANY_ID)

    await _run(session, _invoice(ettn="OPEX-CO2"), company_id=2)

    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()
    assert review_item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert session.execute(select(WorkbenchReviewExecutionEvidence)).scalars().all() == []


async def test_disabled_mapping_is_not_used(session: Session) -> None:
    _add_mapping(session, enabled=False)

    await _run(session, _invoice())

    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()
    assert review_item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert session.execute(select(WorkbenchReviewExecutionEvidence)).scalars().all() == []


async def test_stage1_pin_survives_a_later_mapping_change(session: Session) -> None:
    _add_mapping(session)
    invoice = _invoice()
    await _run(session, invoice)
    review_item = session.execute(select(WorkbenchReviewItem)).scalar_one()

    # A later administrative change to a different account must not move the pinned Stage-1 value.
    row = session.execute(select(OperatingExpenseMappingRecord)).scalar_one()
    row.expense_account_id = 9999
    session.flush()

    source = SqlAlchemyReviewExecutionEvidenceReader(session).get_evidence(
        review_id=review_item.review_id, company_id=COMPANY_ID, expected_version=1
    )
    assert source.operating_expense_match.expense_account_id == EXPENSE_ACCOUNT_ID
