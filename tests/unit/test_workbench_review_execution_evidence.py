from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench import ReviewExecutionEvidence, ReviewItem, ReviewItemCreationService, ReviewStatus
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    WorkbenchContractError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
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
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewRepository
from app.tax_mapping import InvoiceTaxLineResult, InvoiceTaxMappingResult, TaxMatchResult, TaxMatchStatus, TaxType


def test_full_pre_decision_evidence_snapshot_persisted(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert created.status is ReviewStatus.PENDING_REVIEW
    assert record is not None
    assert record.review_id == "review-1"
    assert record.company_id == 7
    assert record.review_version == 1
    assert record.source_invoice_id == "ETTN-1"
    assert record.invoice["header"]["invoice_uuid"] == "UUID-1"
    assert record.partner_match["status"] == "MATCHED"
    assert record.product_match["line_results"][0]["result"]["product_id"] == 701
    assert record.tax_match["line_results"][0]["result"]["tax_id"] == 801


def test_pre_decision_evidence_round_trips_losslessly(session: Session) -> None:
    evidence = _evidence()
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=evidence,
    )

    loaded = repository.get_review_execution_evidence(review_id="review-1", company_id=7, review_version=1)

    assert loaded == evidence
    assert isinstance(loaded.invoice, InternalInvoice)
    assert isinstance(loaded.partner_match, PartnerMatchResult)
    assert isinstance(loaded.product_match, InvoiceProductMatchResult)
    assert isinstance(loaded.tax_match, InvoiceTaxMappingResult)
    assert loaded.invoice.lines[0].quantity == Decimal("2.00")
    assert loaded.partner_match.confidence == Decimal("1.00")
    assert loaded.product_match.line_results[0].result.confidence == Decimal("1.00")
    assert loaded.tax_match.line_results[0].result.tax_rate == Decimal("20")


def test_review_execution_evidence_dto_is_immutable() -> None:
    evidence = _evidence()

    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.review_id = "other"  # type: ignore[misc]


def test_decimal_values_are_stored_as_canonical_strings(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    assert record.invoice["lines"][0]["quantity"] == "2.00"
    assert record.partner_match["confidence"] == "1.00"
    assert record.tax_match["line_results"][0]["result"]["tax_rate"] == "20"


def test_schema_version_persisted(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    record = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert record is not None
    assert record.schema_version == 1


def test_exact_review_company_version_lookup(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    loaded = repository.get_review_execution_evidence(review_id="review-1", company_id=7, review_version=1)

    assert loaded.review_id == "review-1"
    assert loaded.company_id == 7
    assert loaded.review_version == 1


def test_wrong_company_rejected_without_cross_company_leak(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    with pytest.raises(ReviewNotFoundError) as exc_info:
        repository.get_review_execution_evidence(review_id="review-1", company_id=8, review_version=1)

    assert "company 7" not in str(exc_info.value).lower()
    assert "ETTN-1" not in str(exc_info.value)


def test_wrong_review_version_rejected(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(),
    )

    with pytest.raises(ReviewNotFoundError):
        repository.get_review_execution_evidence(review_id="review-1", company_id=7, review_version=2)


def test_later_review_version_does_not_mutate_earlier_evidence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_execution_evidence(
        _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(review_version=1, invoice_number="INV-1"),
    )
    existing = session.scalar(select(WorkbenchReviewExecutionEvidence))
    assert existing is not None
    session.add(
        WorkbenchReviewExecutionEvidence(
            review_id="review-1",
            company_id=7,
            review_version=2,
            source_invoice_id="ETTN-1",
            schema_version=1,
            invoice=dict(
                existing.invoice,
                header={
                    "invoice_number": "INV-2",
                    "invoice_uuid": "UUID-1",
                    "ettn": "ETTN-1",
                    "invoice_type": None,
                    "profile_id": None,
                    "issue_date": "2026-08-01",
                    "issue_time": None,
                    "currency_code": "TRY",
                    "exchange_rate": None,
                    "notes": [],
                },
            ),
            partner_match=existing.partner_match,
            product_match=existing.product_match,
            tax_match=existing.tax_match,
        )
    )
    session.flush()

    loaded = repository.get_review_execution_evidence(review_id="review-1", company_id=7, review_version=1)

    assert loaded.invoice.header.invoice_number == "INV-1"
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 2


def test_duplicate_same_evidence_is_idempotent(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item("review-1", workflow=WorkflowType.VENDOR_BILL)
    evidence = _evidence()

    first = repository.create_review_item_with_execution_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        evidence=evidence,
    )
    second = repository.create_review_item_with_execution_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        evidence=evidence,
    )

    assert second == first
    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1


def test_duplicate_changed_evidence_conflicts(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item("review-1", workflow=WorkflowType.VENDOR_BILL)
    repository.create_review_item_with_execution_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_evidence(invoice_number="INV-1"),
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item_with_execution_evidence(
            item,
            company_id=7,
            idempotency_key="review-key-1",
            evidence=_evidence(invoice_number="INV-CHANGED"),
        )


def test_source_invoice_id_linkage_validated() -> None:
    with pytest.raises(WorkbenchContractError):
        _evidence(source_invoice_id="OTHER")


def test_malformed_persisted_evidence_rejected_without_raw_leak(session: Session) -> None:
    _seed_malformed_evidence(session, invoice={"raw": "<secret-token>"})

    with pytest.raises(ReviewDataIntegrityError) as exc_info:
        SqlAlchemyReviewRepository(session).get_review_execution_evidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )

    assert "secret-token" not in str(exc_info.value)
    assert "raw" not in str(exc_info.value).lower()


def test_missing_partner_match_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        ReviewExecutionEvidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
            source_invoice_id="ETTN-1",
            invoice=_invoice(),
            partner_match=None,  # type: ignore[arg-type]
            product_match=_product_match(),
            tax_match=_tax_match(),
        )


def test_missing_product_match_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        ReviewExecutionEvidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
            source_invoice_id="ETTN-1",
            invoice=_invoice(),
            partner_match=_partner_match(),
            product_match=None,  # type: ignore[arg-type]
            tax_match=_tax_match(),
        )


def test_missing_tax_mapping_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        ReviewExecutionEvidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
            source_invoice_id="ETTN-1",
            invoice=_invoice(),
            partner_match=_partner_match(),
            product_match=_product_match(),
            tax_match=None,  # type: ignore[arg-type]
        )


def test_incomplete_product_match_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        _evidence(product_match=InvoiceProductMatchResult())


def test_incomplete_tax_mapping_rejected() -> None:
    with pytest.raises(WorkbenchContractError):
        _evidence(tax_match=InvoiceTaxMappingResult())


def test_no_provider_odoo_uyumsoft_or_rematching_boundaries() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path("app/persistence/workbench_review_repository.py"),
            Path("app/application/workbench/evidence.py"),
            Path("app/models/workbench_review_execution_evidence.py"),
        )
    ).lower()

    forbidden = (
        "app.connectors",
        "app.erp",
        "odoo",
        "uyumsoft",
        "httpx",
        "zeep",
        ".match_invoice(",
        ".map_invoice(",
        "fuzzy",
        "levenshtein",
        "openai",
        "anthropic",
        "embedding",
        "display",
        "search_read",
    )
    for token in forbidden:
        assert token not in source


def test_sqlalchemy_stays_out_of_application_workbench_layer() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application/workbench").rglob("*.py"))

    assert "sqlalchemy" not in source.lower()
    assert "app.models" not in source.lower()
    assert "app.db" not in source.lower()


def test_service_delegates_atomic_creation_to_writer() -> None:
    item = _review_item("review-1", workflow=WorkflowType.VENDOR_BILL)
    evidence = _evidence()
    writer = RecordingEvidenceWriter()

    created = ReviewItemCreationService(writer).create_pending_review_item_with_execution_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        evidence=evidence,
    )

    assert created is item
    assert writer.evidence_calls == ((item, 7, "review-key-1", evidence),)


def test_evidence_persistence_failure_prevents_ready_review_state(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = SqlAlchemyReviewRepository(session)
    original_add = session.add

    def fail_on_evidence(record: object) -> None:
        if isinstance(record, WorkbenchReviewExecutionEvidence):
            raise SQLAlchemyError("database details with secret-token")
        original_add(record)

    monkeypatch.setattr(session, "add", fail_on_evidence)

    with pytest.raises(ReviewPersistenceError) as exc_info:
        repository.create_review_item_with_execution_evidence(
            _review_item("review-1", workflow=WorkflowType.VENDOR_BILL),
            company_id=7,
            idempotency_key="review-key-1",
            evidence=_evidence(),
        )

    assert "secret-token" not in str(exc_info.value)
    assert session.query(WorkbenchReviewItem).count() == 0
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 0


class RecordingEvidenceWriter:
    def __init__(self) -> None:
        self.evidence_calls: tuple[tuple[ReviewItem, int, str, ReviewExecutionEvidence], ...] = ()

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        raise AssertionError("plain review creation must not be used")

    def create_review_item_with_execution_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
    ) -> ReviewItem:
        self.evidence_calls = (*self.evidence_calls, (item, company_id, idempotency_key, evidence))
        return item


def _evidence(
    *,
    review_id: str = "review-1",
    company_id: int = 7,
    review_version: int = 1,
    source_invoice_id: str = "ETTN-1",
    invoice_number: str = "INV-1",
    product_match: InvoiceProductMatchResult | None = None,
    tax_match: InvoiceTaxMappingResult | None = None,
) -> ReviewExecutionEvidence:
    return ReviewExecutionEvidence(
        review_id=review_id,
        company_id=company_id,
        review_version=review_version,
        source_invoice_id=source_invoice_id,
        invoice=_invoice(invoice_number=invoice_number),
        partner_match=_partner_match(),
        product_match=product_match or _product_match(),
        tax_match=tax_match or _tax_match(company_id=company_id),
    )


def _invoice(*, invoice_number: str = "INV-1") -> InternalInvoice:
    return InternalInvoice(
        header=Header(
            invoice_number=invoice_number,
            invoice_uuid="UUID-1",
            ettn="ETTN-1",
            issue_date=date(2026, 8, 1),
            currency_code="TRY",
        ),
        supplier=Party(name="Supplier", tax_number="1234567890"),
        customer=Party(name="Customer", tax_number="0987654321"),
        totals=MonetaryTotals(
            line_extension_amount=Decimal("200.00"),
            tax_exclusive_amount=Decimal("200.00"),
            tax_inclusive_amount=Decimal("240.00"),
            payable_amount=Decimal("240.00"),
        ),
        lines=(
            InvoiceLine(
                line_number="1",
                seller_item_code="SELL-1",
                buyer_item_code="P-001",
                barcode="BAR-1",
                quantity=Decimal("2.00"),
                unit_code="EA",
                unit_price=Decimal("100.00"),
                line_extension_amount=Decimal("200.00"),
                taxes=(
                    Tax(
                        tax_type="VAT",
                        rate=Decimal("20"),
                        base_amount=Decimal("200.00"),
                        tax_amount=Decimal("40.00"),
                    ),
                ),
            ),
        ),
    )


def _partner_match() -> PartnerMatchResult:
    return PartnerMatchResult(
        status=PartnerMatchStatus.MATCHED,
        partner_id=501,
        matched_by="tax_number",
        reason="Matched by supplier tax number.",
        candidate_count=1,
        confidence=Decimal("1.00"),
    )


def _product_match() -> InvoiceProductMatchResult:
    return InvoiceProductMatchResult(
        line_results=(
            InvoiceProductLineResult(
                line_number="1",
                result=ProductMatchResult(
                    status=ProductMatchStatus.MATCHED,
                    line_number="1",
                    product_id=701,
                    default_code="P-001",
                    barcode="BAR-1",
                    seller_item_code="SELL-1",
                    matched_by="default_code",
                    reason="Matched by default_code.",
                    candidate_count=1,
                    confidence=Decimal("1.00"),
                ),
            ),
        )
    )


def _tax_match(*, company_id: int = 7) -> InvoiceTaxMappingResult:
    return InvoiceTaxMappingResult(
        line_results=(
            InvoiceTaxLineResult(
                line_number="1",
                tax_index=0,
                result=TaxMatchResult(
                    status=TaxMatchStatus.MATCHED,
                    tax_id=801,
                    company_id=company_id,
                    tax_type=TaxType.VAT,
                    tax_rate=Decimal("20"),
                    matched_by="rate",
                    confidence=Decimal("1.00"),
                    reason="Matched by VAT rate.",
                    candidate_count=1,
                ),
            ),
        )
    )


def _review_item(
    review_id: str,
    *,
    workflow: WorkflowType = WorkflowType.MANUAL_REVIEW,
) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id="ETTN-1",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 1),
        currency="TRY",
        total_amount=Decimal("240.00"),
        workflow=workflow,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
        warnings=("safe warning",),
        version=1,
    )


def _seed_malformed_evidence(session: Session, **overrides: object) -> None:
    values: dict[str, object] = {
        "review_id": "review-1",
        "company_id": 7,
        "review_version": 1,
        "source_invoice_id": "ETTN-1",
        "schema_version": 1,
        "invoice": {},
        "partner_match": {},
        "product_match": {},
        "tax_match": {},
    }
    values.update(overrides)
    session.add(WorkbenchReviewExecutionEvidence(**values))
    session.flush()


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[WorkbenchReviewItem.__table__, WorkbenchReviewExecutionEvidence.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
