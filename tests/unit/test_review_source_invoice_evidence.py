"""Immutable review source-invoice evidence (P0-3D2A).

Every review persisted through :class:`ImportInvoiceUseCase` retains a complete,
immutable canonical ``InternalInvoice`` snapshot so deterministic re-evaluation can
later consume the exact same source invoice with **zero connector dependency**
(no Uyumsoft, no Odoo) and without depending on mutable Odoo state.

Scope guard: this suite only observes the review-persistence boundary. It never
creates a supplier, never writes ``res.partner`` / ``account.move``, never
reclassifies, and never mutates review workflow state.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

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
from app.application.dto import RuleEvaluationResult
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import (
    ReviewItemCreationService,
    ReviewSourceInvoiceEvidence,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewNotFoundError,
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
from app.domain.invoice import (
    Address,
    Attachment,
    Discount,
    Header,
    InternalInvoice,
    InvoiceLine,
    MonetaryTotals,
    Party,
    Tax,
)
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
from app.models.workbench_review_source_invoice_evidence import WorkbenchReviewSourceInvoiceEvidence
from app.persistence import (
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyUnitOfWork,
)
from app.persistence import workbench_review_repository as repo_module
from app.persistence.workbench_review_source_invoice_reader import (
    deserialize_review_source_invoice_evidence,
    serialize_review_source_invoice_evidence,
)
from app.tax_mapping import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

COMPANY_ID = 7
ETTN = "AKYASAM-ETTN-0001"
IDEMPOTENCY_KEY = "uyumsoft:7:AKYASAM-ETTN-0001"
AKYASAM_VKN = "0430367181"


# --------------------------------------------------------------------------- builders


def _rich_invoice() -> InternalInvoice:
    """An Akyasam-style incoming invoice exercising every serialized field."""

    return InternalInvoice(
        header=Header(
            invoice_number="AKY2026000000123",
            invoice_uuid="9f1d6f4e-0000-4a11-bbbb-000000000123",
            ettn=ETTN,
            invoice_type="SATIS",
            profile_id="TEMELFATURA",
            issue_date=date(2026, 8, 14),
            issue_time=time(9, 45, 12),
            currency_code="TRY",
            exchange_rate=Decimal("1"),
            notes=("Aidat ve kart bedeli tahsilatidir.", "Elektronik arsiv fatura."),
        ),
        supplier=Party(
            name="AKYASAM GIDA SANAYI VE TICARET ANONIM SIRKETI",
            tax_number=AKYASAM_VKN,
            tax_office="Buyuk Mukellefler",
            mersis_number="0430367181000015",
            website="https://akyasam.example",
            emails=("muhasebe@akyasam.example",),
            phones=("+902165550000",),
            addresses=(
                Address(
                    street="Organize Sanayi Bolgesi",
                    building_number="12",
                    city="Istanbul",
                    district="Tuzla",
                    postal_code="34959",
                    country="Turkiye",
                ),
            ),
        ),
        customer=Party(
            name="ICT TEKNOLOJI HIZMETLERI A.S.",
            tax_number="1112223334",
            tax_office="Kozyatagi",
            addresses=(Address(city="Istanbul", district="Kadikoy", country="Turkiye"),),
        ),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("1000.00"),
            tax_exclusive_amount=Decimal("950.00"),
            tax_inclusive_amount=Decimal("1121.00"),
            allowance_total=Decimal("50.00"),
            charge_total=Decimal("0.00"),
            payable_amount=Decimal("1121.00"),
            rounding_amount=Decimal("0.00"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                description="Uyelik kart bedeli",
                seller_item_code="AKY-KART-01",
                buyer_item_code="ICT-BUY-777",
                barcode="8690000000017",
                quantity=Decimal("2"),
                unit_code="C62",
                unit_price=Decimal("300.00"),
                line_extension_amount=Decimal("600.00"),
                discounts=(Discount(amount=Decimal("30.00"), reason="Kampanya", rate=Decimal("5")),),
                taxes=(
                    Tax(
                        tax_type="0015",
                        rate=Decimal("20"),
                        base_amount=Decimal("570.00"),
                        tax_amount=Decimal("114.00"),
                    ),
                ),
            ),
            InvoiceLine(
                line_number="2",
                description="Yillik aidat",
                seller_item_code="AKY-AIDAT-Y",
                buyer_item_code="ICT-BUY-778",
                barcode=None,
                quantity=Decimal("1"),
                unit_code="C62",
                unit_price=Decimal("400.00"),
                line_extension_amount=Decimal("400.00"),
                discounts=(Discount(amount=Decimal("20.00"), reason="Erken odeme", rate=None),),
                taxes=(
                    Tax(
                        tax_type="0015",
                        rate=Decimal("15"),
                        base_amount=Decimal("380.00"),
                        tax_amount=Decimal("57.00"),
                    ),
                    Tax(
                        tax_type="9015",
                        rate=Decimal("0"),
                        base_amount=Decimal("380.00"),
                        tax_amount=Decimal("0.00"),
                        exemption_reason="Tevkifat kapsami disi",
                    ),
                ),
            ),
        ),
        attachments=(
            Attachment(
                filename="AKY2026000000123.pdf",
                mime_type="application/pdf",
                sha256="a" * 64,
                size=20481,
            ),
        ),
    )


def _simple_invoice() -> InternalInvoice:
    """A single-line invoice whose deterministic matches form a valid Vendor Bill."""

    return InternalInvoice(
        header=Header(
            invoice_number="INV-SIMPLE-1",
            invoice_uuid="00000000-0000-4000-8000-000000000001",
            ettn=ETTN,
            issue_date=date(2026, 8, 14),
            currency_code="TRY",
        ),
        supplier=Party(name="AKYASAM", tax_number=AKYASAM_VKN),
        customer=Party(name="ICT TEKNOLOJI"),
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


def _command(invoice: InternalInvoice | None = None) -> ImportInvoiceCommand:
    return ImportInvoiceCommand(
        invoice=invoice or _rich_invoice(),
        idempotency_key=IDEMPOTENCY_KEY,
        company_id=COMPANY_ID,
    )


def _partner_match(status: PartnerMatchStatus) -> PartnerMatchResult:
    matched = status is PartnerMatchStatus.MATCHED
    return PartnerMatchResult(
        status=status,
        partner_id=4010 if matched else None,
        matched_by="tax_number" if matched else None,
        reason="Unique supplier partner match." if matched else "No supplier partner for VKN.",
        candidate_count=1 if matched else 0,
        confidence=Decimal("1.00") if matched else None,
    )


def _product_match(status: ProductMatchStatus) -> InvoiceProductMatchResult:
    matched = status is ProductMatchStatus.MATCHED
    return InvoiceProductMatchResult(
        line_results=tuple(
            InvoiceProductLineResult(
                line_number=str(index),
                result=ProductMatchResult(
                    status=status,
                    line_number=str(index),
                    product_id=(5000 + index) if matched else None,
                    default_code=f"ICT-BUY-77{index}",
                    barcode=None,
                    seller_item_code=None,
                    matched_by="default_code" if matched else None,
                    reason="Product match result.",
                    candidate_count=1 if matched else 0,
                    confidence=Decimal("1.00") if matched else None,
                ),
            )
            for index in (1, 2)
        )
    )


def _tax_match(status: TaxMatchStatus) -> InvoiceTaxMappingResult:
    matched = status is TaxMatchStatus.MATCHED
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=status,
                    tax_id=6001 if matched else None,
                    company_id=COMPANY_ID,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="company_type_rate" if matched else None,
                    confidence=Decimal("1.00") if matched else None,
                    reason="Exact tax match.",
                    candidate_count=1 if matched else 0,
                ),
            ),
        )
    )


def _supplier_not_found_rule_result() -> RuleEvaluationResult:
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
        partner_match=_partner_match(PartnerMatchStatus.NOT_FOUND),
        product_match=_product_match(ProductMatchStatus.NOT_FOUND),
        tax_match=_tax_match(TaxMatchStatus.MATCHED),
    )


def _operating_expense_manual_review_rule_result() -> RuleEvaluationResult:
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
        partner_match=_partner_match(PartnerMatchStatus.MATCHED),
        product_match=_product_match(ProductMatchStatus.NOT_FOUND),
        tax_match=_tax_match(TaxMatchStatus.MATCHED),
    )


def _simple_matched_rule_result() -> RuleEvaluationResult:
    return RuleEvaluationResult(
        workflow_decision=WorkflowDecision(
            workflow=WorkflowType.VENDOR_BILL,
            matched_rule="vendor_bill_direct_import",
            explanation="Direct vendor bill import.",
            warnings=("rules ok",),
        ),
        partner_match=PartnerMatchResult(
            status=PartnerMatchStatus.MATCHED,
            partner_id=4010,
            matched_by="tax_number",
            reason="Unique supplier partner match.",
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
                        product_id=5001,
                        default_code="SKU-1",
                        barcode=None,
                        seller_item_code=None,
                        matched_by="default_code",
                        reason="Product match result.",
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
                        tax_id=6001,
                        company_id=COMPANY_ID,
                        tax_type=TaxType.VAT,
                        tax_rate=Decimal("20"),
                        matched_by="company_type_rate",
                        confidence=Decimal("1.00"),
                        reason="Exact tax match.",
                        candidate_count=1,
                    ),
                ),
            )
        ),
        warnings=("rules ok",),
    )


class _FakeRuleEngine:
    def __init__(self, rule_result: RuleEvaluationResult) -> None:
        self._rule_result = rule_result

    def evaluate(self, command: ImportInvoiceCommand) -> RuleEvaluationResult:
        return self._rule_result


class _FakeImportHistory:
    def find_imported_invoice(self, idempotency_key: str):
        return None


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewClassificationEvidence.__table__,
            WorkbenchReviewDecision.__table__,
            ExecutionSourceInvoiceEvidence.__table__,
            WorkbenchReviewSourceInvoiceEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def _import_use_case(session: Session, rule_result: RuleEvaluationResult) -> ImportInvoiceUseCase:
    return ImportInvoiceUseCase(
        import_history=_FakeImportHistory(),
        decision_engine=DecisionEngine(
            rule_engine=_FakeRuleEngine(rule_result),
            strategy_resolver=WorkflowStrategyResolver(
                [VendorBillReviewRecommendationStrategy(), ManualReviewStrategy()]
            ),
        ),
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def _assert_invoices_equal(actual: InternalInvoice, expected: InternalInvoice) -> None:
    assert isinstance(actual, InternalInvoice)
    # Whole-object structural equality (frozen dataclasses compare by value).
    assert actual == expected
    # Explicit spot checks so a regression names the field that drifted.
    assert (actual.header.ettn or actual.header.invoice_uuid) == (expected.header.ettn or expected.header.invoice_uuid)
    assert actual.header.invoice_number == expected.header.invoice_number
    assert actual.header.issue_date == expected.header.issue_date
    assert actual.header.issue_time == expected.header.issue_time
    assert actual.header.currency_code == expected.header.currency_code
    assert actual.header.notes == expected.header.notes
    assert actual.supplier.name == expected.supplier.name
    assert actual.supplier.tax_number == expected.supplier.tax_number
    assert actual.supplier.addresses == expected.supplier.addresses
    assert actual.customer.name == expected.customer.name
    assert actual.customer.tax_number == expected.customer.tax_number
    assert actual.totals == expected.totals
    assert len(actual.lines) == len(expected.lines)
    for actual_line, expected_line in zip(actual.lines, expected.lines, strict=True):
        assert actual_line.description == expected_line.description
        assert actual_line.quantity == expected_line.quantity
        assert actual_line.unit_price == expected_line.unit_price
        assert actual_line.unit_code == expected_line.unit_code
        assert actual_line.buyer_item_code == expected_line.buyer_item_code
        assert actual_line.seller_item_code == expected_line.seller_item_code
        assert actual_line.barcode == expected_line.barcode
        assert actual_line.line_extension_amount == expected_line.line_extension_amount
        assert actual_line.discounts == expected_line.discounts
        assert actual_line.taxes == expected_line.taxes
    assert actual.attachments == expected.attachments


# --------------------------------------------------------- Phase 10: SUPPLIER_NOT_FOUND import


async def test_supplier_not_found_import_persists_immutable_source_invoice_evidence(session: Session) -> None:
    invoice = _rich_invoice()
    await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command(invoice))

    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None
    # Classification behavior is unchanged: still a missing-supplier manual review.
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    assert item.status == ReviewStatus.PENDING_REVIEW.value
    reason_codes = {reason["code"] for reason in item.review_reasons}
    assert ManualReviewReasonCode.SUPPLIER_NOT_FOUND.value in reason_codes

    # MANUAL_REVIEW is intentionally not executable: no Stage-1 execution evidence.
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0

    # Exactly one immutable source-invoice snapshot exists for the review.
    rows = session.query(WorkbenchReviewSourceInvoiceEvidence).all()
    assert len(rows) == 1
    assert rows[0].review_id == item.review_id
    assert rows[0].company_id == COMPANY_ID
    assert rows[0].review_version == 1
    assert rows[0].source_invoice_id == ETTN
    assert rows[0].schema_version == 1

    # Typed reader reconstructs the exact InternalInvoice with no connector dependency.
    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    evidence = reader.get(review_id=item.review_id, company_id=COMPANY_ID)
    assert isinstance(evidence, ReviewSourceInvoiceEvidence)
    assert evidence.review_id == item.review_id
    assert evidence.company_id == COMPANY_ID
    assert evidence.review_version == 1
    assert evidence.source_invoice_id == ETTN
    _assert_invoices_equal(evidence.invoice, invoice)
    _assert_invoices_equal(reader.get_invoice(review_id=item.review_id, company_id=COMPANY_ID), invoice)


def test_source_invoice_reader_has_no_connector_dependency() -> None:
    """The reader module must not import any Uyumsoft/Odoo connector or HTTP client."""

    import ast

    import app.persistence.workbench_review_source_invoice_reader as reader_module

    source = reader_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    for module_name in imported:
        lowered = module_name.lower()
        assert "uyumsoft" not in lowered, module_name
        assert "odoo" not in lowered, module_name
        assert "connector" not in lowered, module_name
        assert lowered not in {"httpx", "requests", "aiohttp"}, module_name


# --------------------------------------------------------- Phase 11: round-trip semantic equality


def test_source_invoice_evidence_round_trip_is_semantically_exact() -> None:
    evidence = ReviewSourceInvoiceEvidence(
        review_id="review:round-trip",
        company_id=COMPANY_ID,
        review_version=1,
        source_invoice_id=ETTN,
        invoice=_rich_invoice(),
    )

    payload = serialize_review_source_invoice_evidence(evidence)
    # Persisted payload is plain JSON-safe scalars only.
    assert payload["schema_version"] == 1
    assert isinstance(payload["invoice"], dict)

    restored = deserialize_review_source_invoice_evidence(payload)
    assert restored == evidence
    _assert_invoices_equal(restored.invoice, _rich_invoice())

    # Re-serializing the restored evidence is byte-stable.
    assert serialize_review_source_invoice_evidence(restored) == payload


# --------------------------------------------------------- Phase 12: coexists with execution evidence


async def test_product_vendor_bill_keeps_both_source_and_execution_evidence(session: Session) -> None:
    invoice = _simple_invoice()
    await _import_use_case(session, _simple_matched_rule_result()).execute(_command(invoice))

    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None
    assert item.workflow == WorkflowType.VENDOR_BILL.value

    # Stage-1 execution evidence is still produced and unchanged.
    execution = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert execution is not None
    assert execution.review_id == item.review_id
    assert execution.review_version == 1
    assert execution.source_invoice_id == ETTN

    # The new immutable source snapshot coexists (duplication of the invoice payload is acceptable).
    source_rows = session.query(WorkbenchReviewSourceInvoiceEvidence).all()
    assert len(source_rows) == 1
    assert source_rows[0].source_invoice_id == ETTN

    evidence = SqlAlchemyReviewSourceInvoiceEvidenceReader(session).get(review_id=item.review_id, company_id=COMPANY_ID)
    _assert_invoices_equal(evidence.invoice, invoice)


# --------------------------------------------------------- Phase 13: operating-expense regression


async def test_operating_expense_manual_review_still_persists_source_evidence(session: Session) -> None:
    invoice = _rich_invoice()
    await _import_use_case(session, _operating_expense_manual_review_rule_result()).execute(_command(invoice))

    item = session.scalar(select(WorkbenchReviewItem))
    assert item is not None
    assert item.workflow == WorkflowType.MANUAL_REVIEW.value
    reason_codes = {reason["code"] for reason in item.review_reasons}
    assert ManualReviewReasonCode.OPERATING_EXPENSE_MAPPING_REQUIRED.value in reason_codes
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0

    evidence = SqlAlchemyReviewSourceInvoiceEvidenceReader(session).get(review_id=item.review_id, company_id=COMPANY_ID)
    _assert_invoices_equal(evidence.invoice, invoice)


# --------------------------------------------------------- Phase 14: idempotency / fail-closed


async def test_replay_creates_no_conflicting_source_snapshot(session: Session) -> None:
    await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command())
    await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command())

    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewSourceInvoiceEvidence).count() == 1

    item = session.scalar(select(WorkbenchReviewItem))
    evidence = SqlAlchemyReviewSourceInvoiceEvidenceReader(session).get(review_id=item.review_id, company_id=COMPANY_ID)
    _assert_invoices_equal(evidence.invoice, _rich_invoice())


async def test_replay_with_different_source_content_fails_closed(session: Session) -> None:
    await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command())

    mutated = _rich_invoice()
    mutated_lines = (
        (
            mutated.lines[0].__class__(  # replace a monetary field on line 1
                line_number=mutated.lines[0].line_number,
                description="TAMPERED DESCRIPTION",
                seller_item_code=mutated.lines[0].seller_item_code,
                buyer_item_code=mutated.lines[0].buyer_item_code,
                barcode=mutated.lines[0].barcode,
                quantity=mutated.lines[0].quantity,
                unit_code=mutated.lines[0].unit_code,
                unit_price=Decimal("999999.99"),
                line_extension_amount=mutated.lines[0].line_extension_amount,
                discounts=mutated.lines[0].discounts,
                taxes=mutated.lines[0].taxes,
            ),
        )
        + mutated.lines[1:]
    )
    tampered_invoice = InternalInvoice(
        header=mutated.header,
        supplier=mutated.supplier,
        customer=mutated.customer,
        totals=mutated.totals,
        lines=mutated_lines,
        attachments=mutated.attachments,
    )

    with pytest.raises(Exception):  # noqa: B017 - repository raises a safe idempotency conflict
        await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command(tampered_invoice))

    # The original immutable snapshot is never overwritten.
    assert session.query(WorkbenchReviewSourceInvoiceEvidence).count() == 1
    item = session.scalar(select(WorkbenchReviewItem))
    evidence = SqlAlchemyReviewSourceInvoiceEvidenceReader(session).get(review_id=item.review_id, company_id=COMPANY_ID)
    _assert_invoices_equal(evidence.invoice, _rich_invoice())


# --------------------------------------------------------- Phase 15: historical reviews & corruption


def _persist_bare_review_item(session: Session, *, review_id: str) -> WorkbenchReviewItem:
    record = WorkbenchReviewItem(
        review_id=review_id,
        company_id=COMPANY_ID,
        invoice_id=ETTN,
        invoice_number="AKY2026000000123",
        supplier_tax_number=AKYASAM_VKN,
        supplier_name="AKYASAM",
        invoice_date=date(2026, 8, 14),
        currency="TRY",
        total_amount=Decimal("1121.00"),
        workflow=WorkflowType.MANUAL_REVIEW.value,
        status=ReviewStatus.PENDING_REVIEW.value,
        review_reasons=[],
        warnings=[],
        version=1,
        idempotency_key="historical-key",
    )
    session.add(record)
    session.flush()
    return record


def test_historical_review_without_row_raises_not_found(session: Session) -> None:
    _persist_bare_review_item(session, review_id="review:historical")

    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    with pytest.raises(ReviewNotFoundError):
        reader.get(review_id="review:historical", company_id=COMPANY_ID)


def test_corrupt_source_evidence_is_distinguished_from_not_found(session: Session) -> None:
    _persist_bare_review_item(session, review_id="review:corrupt")
    session.add(
        WorkbenchReviewSourceInvoiceEvidence(
            review_id="review:corrupt",
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id=ETTN,
            schema_version=1,
            invoice={"header": {"invoice_number": "X"}},  # structurally incomplete payload
        )
    )
    session.flush()

    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    with pytest.raises(ReviewDataIntegrityError):
        reader.get(review_id="review:corrupt", company_id=COMPANY_ID)


def test_unknown_schema_version_is_data_integrity_error(session: Session) -> None:
    _persist_bare_review_item(session, review_id="review:future-schema")
    session.add(
        WorkbenchReviewSourceInvoiceEvidence(
            review_id="review:future-schema",
            company_id=COMPANY_ID,
            review_version=1,
            source_invoice_id=ETTN,
            schema_version=999,
            invoice=serialize_review_source_invoice_evidence(
                ReviewSourceInvoiceEvidence(
                    review_id="review:future-schema",
                    company_id=COMPANY_ID,
                    review_version=1,
                    source_invoice_id=ETTN,
                    invoice=_rich_invoice(),
                )
            )["invoice"],
        )
    )
    session.flush()

    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    with pytest.raises(ReviewDataIntegrityError):
        reader.get(review_id="review:future-schema", company_id=COMPANY_ID)


def test_reader_rejects_invalid_query_arguments(session: Session) -> None:
    reader = SqlAlchemyReviewSourceInvoiceEvidenceReader(session)
    with pytest.raises(WorkbenchContractError):
        reader.get(review_id="  ", company_id=COMPANY_ID)
    with pytest.raises(WorkbenchContractError):
        reader.get(review_id="review:x", company_id=0)


# --------------------------------------------------------- atomicity


async def test_review_creation_rolls_back_when_source_evidence_insert_fails(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_model(evidence: ReviewSourceInvoiceEvidence) -> WorkbenchReviewSourceInvoiceEvidence:
        # A CHECK-constraint violation (review_version > 0) forces the nested flush to fail
        # *after* the review item row has already been flushed inside the same savepoint.
        return WorkbenchReviewSourceInvoiceEvidence(
            review_id=evidence.review_id,
            company_id=evidence.company_id,
            review_version=0,
            source_invoice_id=evidence.source_invoice_id,
            schema_version=1,
            invoice=serialize_review_source_invoice_evidence(evidence)["invoice"],
        )

    monkeypatch.setattr(repo_module, "model_from_review_source_invoice_evidence", _broken_model)

    with pytest.raises(Exception):  # noqa: B017 - safe persistence error after savepoint rollback
        await _import_use_case(session, _supplier_not_found_rule_result()).execute(_command())

    session.rollback()
    # Neither the review item nor a partial source snapshot survived.
    assert session.query(WorkbenchReviewItem).count() == 0
    assert session.query(WorkbenchReviewSourceInvoiceEvidence).count() == 0
