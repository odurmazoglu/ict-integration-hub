"""Non-destructive deterministic reclassification of an existing Workbench review (P0-3D2B).

A review created as ``version 1 / MANUAL_REVIEW / SUPPLIER_NOT_FOUND`` is later
re-run through the *same* normal deterministic classification against current
master data using its immutable source invoice. The current projection advances
to version 2 while an immutable ``WorkbenchReviewReclassification`` event proves
what v1 was, why the reclassification ran, and what deterministic result produced
v2. History is never rewritten.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.application.commands import ImportInvoiceCommand
from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.dto import RuleEvaluationResult
from app.application.expense_mapping import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.application.use_cases import ImportInvoiceUseCase, ReclassifyWorkbenchReviewUseCase
from app.application.workbench import (
    ReclassifyReviewCommand,
    ReviewItemCreationService,
    ReviewReclassificationTrigger,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
    WorkbenchContractError,
)
from app.application.workflow import (
    ManualReviewDecision,
    ManualReviewReason,
    ManualReviewReasonCode,
    WorkflowDecision,
    WorkflowType,
)
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
from app.models.execution_source_invoice_evidence import ExecutionSourceInvoiceEvidence
from app.models.workbench_review_classification_evidence import WorkbenchReviewClassificationEvidence
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.models.workbench_review_reclassification import WorkbenchReviewReclassification
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import (
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from app.persistence import workbench_review_repository as repo_module
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

COMPANY_ID = 7
PARTNER_ID = 4010
EXPENSE_ACCOUNT_ID = 9001
ETTN = "AKYASAM-ETTN-RECLASS-1"
IDEMPOTENCY_KEY = "uyumsoft:7:AKYASAM-ETTN-RECLASS-1"
AKYASAM_VKN = "0430367181"


# --------------------------------------------------------------------------- builders


def _sku_invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-SKU-1",
            invoice_uuid="00000000-0000-4000-8000-0000000000a1",
            ettn=ETTN,
            issue_date=date(2026, 8, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="AKYASAM", tax_number=AKYASAM_VKN),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("120.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Networking switch",
                buyer_item_code="SKU-1",
                quantity=Decimal("2"),
                unit_code="C62",
                unit_price=Decimal("50.00"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _identifier_free_invoice() -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number="AKY-OPEX-1",
            invoice_uuid="00000000-0000-4000-8000-0000000000b2",
            ettn=ETTN,
            issue_date=date(2026, 8, 20),
            currency_code="TRY",
        ),
        supplier=Party(name="AKYASAM", tax_number=AKYASAM_VKN),
        customer=Party(name="ICT TEKNOLOJI", tax_number="1112223334"),
        totals=MonetaryTotals(payable_amount=Decimal("100.00")),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Yillik aidat",
                buyer_item_code=None,
                seller_item_code=None,
                barcode=None,
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("83.33"),
                taxes=(Tax(tax_type="KDV", rate=Decimal("20")),),
            ),
        ),
    )


def _partner(status: PartnerMatchStatus) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=PARTNER_ID if matched else None,
        matched_by="tax_number" if matched else None,
        reason="Unique supplier partner match." if matched else "No supplier partner for VKN.",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _products(invoice: InternalInvoice, status: ProductMatchStatus) -> InvoiceProductMatchResult:
    matched = status is ProductMatchStatus.MATCHED
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=line.line_number,
                result=ProductMatchResult(
                    status=status,
                    line_number=line.line_number,
                    product_id=5001 if matched else None,
                    default_code="SKU-1" if status is not ProductMatchStatus.INVALID_INPUT else None,
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if matched else None,
                    reason="Product match result.",
                    candidate_count=1 if matched else 0,
                    confidence=Decimal("1.00") if matched else None,
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
                tax_index=tax_index,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=6001,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate",
                    confidence=Decimal("1.00"),
                    reason="Exact tax match.",
                    candidate_count=1,
                ),
            )
            for line in invoice.lines
            for tax_index, _tax in enumerate(line.taxes)
        )
    )


def _expense_match(status: OperatingExpenseMatchStatus) -> OperatingExpenseMatchResult:
    if status is not OperatingExpenseMatchStatus.MATCHED:
        return OperatingExpenseMatchResult(status=status, reason="No enabled mapping.", candidate_count=0)
    return OperatingExpenseMatchResult(
        status=status,
        reason="Exact company and supplier partner operating-expense mapping.",
        candidate_count=1,
        mapping_id=42,
        company_id=COMPANY_ID,
        vendor_partner_id=PARTNER_ID,
        expense_account_id=EXPENSE_ACCOUNT_ID,
        expense_category="OFFICE_BUILDING_EXPENSE",
        matched_by="company_partner",
        confidence=Decimal("1.00"),
    )


def _supplier_not_found_rule_result(invoice: InternalInvoice) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.MANUAL_REVIEW,
            matched_rule="RULE-MANUAL-SUPPLIER-NOT-FOUND",
            explanation="Supplier partner does not exist in Odoo yet.",
            manual_review=ManualReviewDecision(
                reasons=(
                    ManualReviewReason(
                        code=ManualReviewReasonCode.SUPPLIER_NOT_FOUND,
                        message="No supplier partner found for VKN 0430367181.",
                        source="partner_matching",
                        candidate_count=0,
                    ),
                ),
                summary="1 review reason.",
            ),
        ),
        partner_match=_partner(PartnerMatchStatus.NOT_FOUND),
        product_match=_products(invoice, ProductMatchStatus.NOT_FOUND),
        tax_match=_taxes(invoice),
    )


def _mapping_required_rule_result(invoice: InternalInvoice) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.MANUAL_REVIEW,
            matched_rule="RULE-MANUAL-OPERATING-EXPENSE",
            explanation="Operating expense mapping is required before a Vendor Bill can be built.",
            manual_review=ManualReviewDecision(
                reasons=(
                    ManualReviewReason(
                        code=ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED,
                        message="No enabled operating-expense mapping for this vendor.",
                        source="operating_expense_matching",
                        candidate_count=0,
                    ),
                ),
                summary="1 review reason.",
            ),
        ),
        partner_match=_partner(PartnerMatchStatus.MATCHED),
        product_match=_products(invoice, ProductMatchStatus.INVALID_INPUT),
        tax_match=_taxes(invoice),
        operating_expense_match=_expense_match(OperatingExpenseMatchStatus.NOT_FOUND),
    )


def _product_vendor_bill_rule_result(invoice: InternalInvoice) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule="RULE-DIRECT-VENDOR-BILL-001",
            explanation="Fully matched direct Vendor Bill.",
            warnings=("rules ok",),
        ),
        partner_match=_partner(PartnerMatchStatus.MATCHED),
        product_match=_products(invoice, ProductMatchStatus.MATCHED),
        tax_match=_taxes(invoice),
        warnings=("rules ok",),
    )


def _operating_expense_vendor_bill_rule_result(invoice: InternalInvoice) -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule="RULE-OPERATING-EXPENSE-VENDOR-BILL-001",
            explanation="Deterministic operating-expense Vendor Bill.",
        ),
        partner_match=_partner(PartnerMatchStatus.MATCHED),
        product_match=_products(invoice, ProductMatchStatus.INVALID_INPUT),
        tax_match=_taxes(invoice),
        operating_expense_match=_expense_match(OperatingExpenseMatchStatus.MATCHED),
    )


class _StubRuleEngine:
    def __init__(self, result: RuleEvaluationResult) -> None:
        self._result = result

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self._result


class _FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str) -> None:
        return None

    def record_import_result(self, *, company_id: int, idempotency_key: str, result: object) -> None:
        return None


def _decision_engine(rule_result: RuleEvaluationResult) -> DecisionEngine:
    return DecisionEngine(
        rule_engine=_StubRuleEngine(rule_result),
        strategy_resolver=WorkflowStrategyResolver([VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]),
    )


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewReclassification.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


async def _seed_supplier_not_found_v1(session: Session, invoice: InternalInvoice) -> str:
    use_case = ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=_decision_engine(_supplier_not_found_rule_result(invoice)),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )
    await use_case.execute(
        ImportInvoiceCommand(invoice=invoice, idempotency_key=IDEMPOTENCY_KEY, company_id=COMPANY_ID)
    )
    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert item.version == 1
    return item.review_id


def _reclassifier(session: Session, rule_result: RuleEvaluationResult) -> ReclassifyWorkbenchReviewUseCase:
    return ReclassifyWorkbenchReviewUseCase(
        decision_engine=_decision_engine(rule_result),
        source_invoice_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        reclassification_writer=SqlAlchemyReviewRepository(session),
    )


def _reason_codes(payload: list[dict]) -> set[str]:
    return {entry["code"] for entry in payload}


# --------------------------------------------------------- Phase 24 / 12: SUPPLIER_NOT_FOUND -> MAPPING REQUIRED


async def test_supplier_not_found_reclassifies_to_operating_expense_mapping_required(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    result = await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
        )
    )

    assert result.changed is True
    assert (result.from_version, result.to_version) == (1, 2)
    assert result.previous_workflow is WorkflowType.MANUAL_REVIEW
    assert result.new_workflow is WorkflowType.MANUAL_REVIEW
    assert result.executable is False

    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert item.status == ReviewStatus.PENDING_REVIEW.value
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert _reason_codes(item.review_reasons) == {ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value}

    events = session.query(WorkbenchReviewReclassification).all()
    assert len(events) == 1
    event = events[0]
    assert (event.from_version, event.to_version) == (1, 2)
    assert event.trigger == ReviewReclassificationTrigger.SUPPLIER_RESOLUTION.value
    assert event.previous_workflow == WorkflowType.MANUAL_REVIEW.value
    assert _reason_codes(event.previous_review_reasons) == {ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value}
    assert _reason_codes(event.new_review_reasons) == {ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value}
    assert event.source_invoice_id == ETTN
    assert event.executable is False

    # No Stage-1 execution evidence for the new version.
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0
    # The immutable source snapshot is untouched and still linked to v1's creation.
    source_rows = session.query(WorkbenchReviewSourceInvoiceEvidence).all()
    assert len(source_rows) == 1
    assert source_rows[0].review_version == 1


# --------------------------------------------------------- Phase 25 / 14: SUPPLIER_NOT_FOUND -> OPERATING EXPENSE VB


async def test_supplier_not_found_reclassifies_to_operating_expense_vendor_bill(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    result = await _reclassifier(session, _operating_expense_vendor_bill_rule_result(invoice)).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )

    assert result.changed is True
    assert result.new_workflow is WorkflowType.VENDOR_BILL
    assert result.executable is True

    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert item.workflow == WorkflowType.VENDOR_BILL.value

    stage1 = session.scalars(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    ).all()
    assert len(stage1) == 1
    assert stage1[0].review_id == review_id
    assert stage1[0].source_invoice_id == ETTN
    assert stage1[0].operating_expense_match is not None
    assert stage1[0].operating_expense_match["expense_account_id"] == EXPENSE_ACCOUNT_ID

    event = session.scalar(select(WorkbenchReviewReclassification))
    assert event.new_workflow == WorkflowType.VENDOR_BILL.value
    assert event.executable is True
    assert _reason_codes(event.previous_review_reasons) == {ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value}


# --------------------------------------------------------- Phase 26 / 13: SUPPLIER_NOT_FOUND -> PRODUCT VENDOR BILL


async def test_supplier_not_found_reclassifies_to_product_vendor_bill(session: Session) -> None:
    invoice = _sku_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    result = await _reclassifier(session, _product_vendor_bill_rule_result(invoice)).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
        )
    )

    assert result.new_workflow is WorkflowType.VENDOR_BILL
    assert result.executable is True

    stage1 = session.scalar(
        select(WorkbenchReviewExecutionEvidence).where(WorkbenchReviewExecutionEvidence.review_version == 2)
    )
    assert stage1 is not None
    assert stage1.operating_expense_match is None  # product mode, no expense fallback
    assert stage1.product_match["line_results"][0]["result"]["status"] == ProductMatchStatus.MATCHED.value

    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 2
    assert item.workflow == WorkflowType.VENDOR_BILL.value


# --------------------------------------------------------- Phase 19: NO-OP


async def test_reclassification_is_noop_when_deterministic_outcome_is_unchanged(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    # Supplier still missing -> identical MANUAL_REVIEW / SUPPLIER_NOT_FOUND outcome.
    result = await _reclassifier(session, _supplier_not_found_rule_result(invoice)).execute(
        ReclassifyReviewCommand(
            review_id=review_id,
            company_id=COMPANY_ID,
            expected_version=1,
            trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
        )
    )

    assert result.changed is False
    assert (result.from_version, result.to_version) == (1, 1)
    assert session.query(WorkbenchReviewReclassification).count() == 0
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


# --------------------------------------------------------- Phase 18: idempotent retry


async def test_reclassification_retry_returns_stable_already_applied_result(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)
    command = ReclassifyReviewCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=1,
        trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
    )

    first = await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(command)
    second = await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(command)

    assert first.to_version == second.to_version == 2
    assert second.changed is True
    assert session.query(WorkbenchReviewReclassification).count() == 1
    assert session.scalar(select(WorkbenchReviewItem)).version == 2
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


async def test_reclassification_retry_with_different_outcome_fails_closed(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)
    command = ReclassifyReviewCommand(
        review_id=review_id,
        company_id=COMPANY_ID,
        expected_version=1,
        trigger=ReviewReclassificationTrigger.SUPPLIER_RESOLUTION,
    )
    await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(command)

    with pytest.raises(ReviewVersionConflictError):
        await _reclassifier(session, _operating_expense_vendor_bill_rule_result(invoice)).execute(command)

    assert session.query(WorkbenchReviewReclassification).count() == 1
    assert session.scalar(select(WorkbenchReviewItem)).version == 2


# --------------------------------------------------------- Phase 27 / 9: version race with a concurrent transition


async def test_reclassification_loses_race_to_a_concurrent_version_bump(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    # A human decision (or another actor) advances the review out from under us.
    session.execute(
        update(WorkbenchReviewItem)
        .where(WorkbenchReviewItem.review_id == review_id)
        .values(version=2, status=ReviewStatus.DECISION_SUBMITTED.value)
    )
    session.flush()

    # Fail closed: exactly one transition from version 1 wins; the loser gets a
    # safe 409-class conflict (state or version), never a silent overwrite.
    with pytest.raises((ReviewStateConflictError, ReviewVersionConflictError)):
        await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(
            ReclassifyReviewCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=1,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            )
        )

    assert session.query(WorkbenchReviewReclassification).count() == 0
    assert session.scalar(select(WorkbenchReviewItem)).version == 2


async def test_reclassification_with_stale_expected_version_raises_version_conflict(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    # The review is still pending but a prior transition already advanced it.
    session.execute(update(WorkbenchReviewItem).where(WorkbenchReviewItem.review_id == review_id).values(version=3))
    session.flush()

    with pytest.raises(ReviewVersionConflictError):
        await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(
            ReclassifyReviewCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=1,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            )
        )

    assert session.query(WorkbenchReviewReclassification).count() == 0
    assert session.scalar(select(WorkbenchReviewItem)).version == 3


# --------------------------------------------------------- Phase 28: terminal review state


async def test_reclassification_is_rejected_for_a_terminal_review(session: Session) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)
    session.execute(
        update(WorkbenchReviewItem)
        .where(WorkbenchReviewItem.review_id == review_id)
        .values(status=ReviewStatus.DISMISSED.value)
    )
    session.flush()

    with pytest.raises(ReviewStateConflictError):
        await _reclassifier(session, _mapping_required_rule_result(invoice)).execute(
            ReclassifyReviewCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=1,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            )
        )

    assert session.query(WorkbenchReviewReclassification).count() == 0
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1
    assert item.status == ReviewStatus.DISMISSED.value


# --------------------------------------------------------- Phase 29: historical review without source evidence


async def test_reclassification_without_source_evidence_raises_not_found(session: Session) -> None:
    # A review created before P0-3D2A: no ReviewSourceInvoiceEvidence row.
    session.add(
        WorkbenchReviewItem(
            review_id="review:pre-3d2a",
            company_id=COMPANY_ID,
            invoice_id=ETTN,
            invoice_number="AKY-OLD-1",
            supplier_tax_number=AKYASAM_VKN,
            supplier_name="AKYASAM",
            invoice_date=date(2026, 6, 1),
            currency="TRY",
            total_amount=Decimal("100.00"),
            workflow=WorkflowType.MANUAL_REVIEW.value,
            status=ReviewStatus.PENDING_REVIEW.value,
            review_reasons=[],
            warnings=[],
            version=1,
            idempotency_key="historical-key",
        )
    )
    session.flush()

    with pytest.raises(ReviewNotFoundError):
        await _reclassifier(session, _mapping_required_rule_result(_identifier_free_invoice())).execute(
            ReclassifyReviewCommand(
                review_id="review:pre-3d2a",
                company_id=COMPANY_ID,
                expected_version=1,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            )
        )

    assert session.query(WorkbenchReviewReclassification).count() == 0
    assert session.scalar(select(WorkbenchReviewItem)).version == 1


# --------------------------------------------------------- Phase 30: atomic rollback


async def test_reclassification_rolls_back_entirely_when_evidence_write_fails(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invoice = _identifier_free_invoice()
    review_id = await _seed_supplier_not_found_v1(session, invoice)

    original = repo_module._evidence_model_from_review_evidence

    def _broken(evidence: object) -> WorkbenchReviewExecutionEvidence:
        record = original(evidence)
        record.review_version = 0  # CHECK (review_version > 0) fails on the nested flush
        return record

    monkeypatch.setattr(repo_module, "_evidence_model_from_review_evidence", _broken)

    with pytest.raises(Exception):  # noqa: B017 - safe fail-closed error after savepoint rollback
        await _reclassifier(session, _operating_expense_vendor_bill_rule_result(invoice)).execute(
            ReclassifyReviewCommand(
                review_id=review_id,
                company_id=COMPANY_ID,
                expected_version=1,
                trigger=ReviewReclassificationTrigger.MASTER_DATA_CHANGED,
            )
        )

    session.rollback()
    item = session.scalar(select(WorkbenchReviewItem))
    assert item.version == 1
    assert item.status == ReviewStatus.PENDING_REVIEW.value
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert session.query(WorkbenchReviewReclassification).count() == 0
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0
    assert session.query(WorkbenchReviewClassificationEvidence).count() == 0


# --------------------------------------------------------- contract guards


def test_reclassify_command_does_not_accept_an_invoice_from_the_caller() -> None:
    field_names = {field.name for field in dataclasses.fields(ReclassifyReviewCommand)}
    assert field_names == {"review_id", "company_id", "expected_version", "trigger", "note"}
    assert "invoice" not in field_names


def test_reclassify_command_rejects_free_text_trigger() -> None:
    with pytest.raises(WorkbenchContractError):
        ReclassifyReviewCommand(
            review_id="review:x",
            company_id=COMPANY_ID,
            expected_version=1,
            trigger="whatever",  # type: ignore[arg-type]
        )


def test_reclassification_modules_have_no_connector_dependency() -> None:
    for module_name in (
        "app.application.use_cases.reclassify_review",
        "app.application.use_cases.review_classification_outcome",
    ):
        module = __import__(module_name, fromlist=["__file__"])
        with open(module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            lowered = name.lower()
            assert "uyumsoft" not in lowered, (module_name, name)
            assert "odoo" not in lowered, (module_name, name)
            assert "connector" not in lowered, (module_name, name)
            assert lowered not in {"httpx", "requests", "aiohttp"}, (module_name, name)
