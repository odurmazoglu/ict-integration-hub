from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench import (
    ReviewExecutionBillingEvidence,
    ReviewItem,
    ReviewItemCreationService,
    ReviewStatus,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    WorkbenchContractError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.billing.dto import CustomerInvoiceBillingInstruction, CustomerInvoiceBillingLine
from app.db.base import Base
from app.models.workbench_review_billing_evidence import WorkbenchReviewBillingEvidence
from app.models.workbench_review_execution_evidence import WorkbenchReviewExecutionEvidence
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewBillingEvidenceReader, SqlAlchemyReviewRepository
from app.persistence.review_billing_evidence_reader import (
    deserialize_billing_instruction_payload,
    serialize_billing_instruction_payload,
)


def test_billing_evidence_snapshot_persisted_immutably(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item_with_billing_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(_billing_evidence(),),
    )

    record = session.scalar(select(WorkbenchReviewBillingEvidence))
    assert created.status is ReviewStatus.PENDING_REVIEW
    assert record is not None
    assert record.review_id == "review-1"
    assert record.company_id == 7
    assert record.review_version == 1
    assert record.billing_key == "bill:customer-501:alloc-1"
    assert record.schema_version == 1
    assert record.billing_instruction["customer_id"] == 501
    assert record.billing_instruction["currency"] == "TRY"
    assert record.billing_instruction["lines"][0]["allocation_key"] == "ALLOC-1"


def test_billing_evidence_dto_is_immutable() -> None:
    evidence = _billing_evidence()

    assert not hasattr(evidence, "__dict__")
    with pytest.raises(FrozenInstanceError):
        evidence.review_id = "other"  # type: ignore[misc]


def test_billing_evidence_reader_uses_exact_review_company_version(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_billing_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(_billing_evidence(),),
    )

    loaded = SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
        review_id="review-1",
        company_id=7,
        review_version=1,
    )

    assert loaded == (_billing_instruction(),)


def test_billing_evidence_wrong_company_rejected(session: Session) -> None:
    _create_billing_evidence(session)

    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
            review_id="review-1",
            company_id=8,
            review_version=1,
        )


def test_billing_evidence_wrong_version_rejected(session: Session) -> None:
    _create_billing_evidence(session)

    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
            review_id="review-1",
            company_id=7,
            review_version=2,
        )


def test_missing_billing_evidence_fails_closed(session: Session) -> None:
    with pytest.raises(ReviewNotFoundError):
        SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )


def test_duplicate_same_billing_evidence_is_idempotent(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    evidence = _billing_evidence()

    first = repository.create_review_item_with_billing_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(evidence,),
    )
    second = repository.create_review_item_with_billing_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(evidence,),
    )

    assert second == first
    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewBillingEvidence).count() == 1


def test_duplicate_changed_billing_evidence_conflicts(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item()
    repository.create_review_item_with_billing_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(_billing_evidence(unit_price=Decimal("150.00")),),
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item_with_billing_evidence(
            item,
            company_id=7,
            idempotency_key="review-key-1",
            billing_evidence=(_billing_evidence(unit_price=Decimal("151.00")),),
        )


def test_later_review_version_does_not_mutate_earlier_billing_evidence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item_with_billing_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(_billing_evidence(unit_price=Decimal("150.00")),),
    )
    session.add(
        WorkbenchReviewBillingEvidence(
            review_id="review-1",
            company_id=7,
            review_version=2,
            billing_key="bill:customer-501:alloc-1",
            schema_version=1,
            billing_instruction=serialize_billing_instruction_payload(
                _billing_instruction(unit_price=Decimal("175.00"))
            ),
        )
    )
    session.flush()

    loaded = repository.get_review_billing_evidence(review_id="review-1", company_id=7, review_version=1)

    assert loaded[0].billing_instruction.lines[0].unit_price == Decimal("150.00")
    assert session.query(WorkbenchReviewBillingEvidence).count() == 2


def test_serializer_round_trips_billing_instruction_losslessly() -> None:
    instruction = _billing_instruction(quantity=Decimal("2.500"), unit_price=Decimal("150.120000"))

    loaded = deserialize_billing_instruction_payload(serialize_billing_instruction_payload(instruction))

    assert loaded == instruction
    assert loaded.lines[0].quantity == Decimal("2.500")
    assert loaded.lines[0].unit_price == Decimal("150.120000")


def test_decimal_values_are_stored_as_strings_without_float_conversion(session: Session) -> None:
    _create_billing_evidence(session)

    record = session.scalar(select(WorkbenchReviewBillingEvidence))
    assert record is not None
    assert record.billing_instruction["lines"][0]["quantity"] == "1.000"
    assert record.billing_instruction["lines"][0]["unit_price"] == "150.00"
    assert not isinstance(record.billing_instruction["lines"][0]["unit_price"], float)


def test_float_decimal_payload_is_rejected() -> None:
    payload = serialize_billing_instruction_payload(_billing_instruction())
    payload["lines"][0]["unit_price"] = 150.0

    with pytest.raises(ReviewDataIntegrityError):
        deserialize_billing_instruction_payload(payload)


def test_malformed_billing_evidence_fails_closed_without_raw_leak(session: Session) -> None:
    session.add(
        WorkbenchReviewBillingEvidence(
            review_id="review-1",
            company_id=7,
            review_version=1,
            billing_key="bill:customer-501:alloc-1",
            schema_version=1,
            billing_instruction={"raw": "<secret-token>", "billing_key": "bill:customer-501:alloc-1"},
        )
    )
    session.flush()

    with pytest.raises(ReviewDataIntegrityError) as exc_info:
        SqlAlchemyReviewBillingEvidenceReader(session).get_billing_instructions(
            review_id="review-1",
            company_id=7,
            review_version=1,
        )

    assert "secret-token" not in str(exc_info.value)
    assert "raw" not in str(exc_info.value).lower()


def test_billing_evidence_rejects_empty_or_duplicate_billing_keys() -> None:
    item = _review_item()
    with pytest.raises(WorkbenchContractError):
        SqlAlchemyReviewRepository(_NullSession()).create_review_item_with_billing_evidence(  # type: ignore[arg-type]
            item,
            company_id=7,
            idempotency_key="review-key-1",
            billing_evidence=(),
        )
    with pytest.raises(WorkbenchContractError):
        SqlAlchemyReviewRepository(_NullSession()).create_review_item_with_billing_evidence(  # type: ignore[arg-type]
            item,
            company_id=7,
            idempotency_key="review-key-1",
            billing_evidence=(_billing_evidence(), _billing_evidence()),
        )


def test_billing_evidence_can_be_created_atomically_with_source_evidence(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item_with_execution_and_billing_evidence(
        _review_item(workflow=WorkflowType.VENDOR_BILL),
        company_id=7,
        idempotency_key="review-key-1",
        evidence=_source_evidence(),
        billing_evidence=(_billing_evidence(),),
    )

    assert created.review_id == "review-1"
    assert session.query(WorkbenchReviewItem).count() == 1
    assert session.query(WorkbenchReviewExecutionEvidence).count() == 1
    assert session.query(WorkbenchReviewBillingEvidence).count() == 1


def test_service_delegates_billing_evidence_creation_to_writer() -> None:
    item = _review_item()
    evidence = _billing_evidence()
    writer = RecordingBillingEvidenceWriter()

    created = ReviewItemCreationService(writer).create_pending_review_item_with_billing_evidence(
        item,
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(evidence,),
    )

    assert created is item
    assert writer.billing_calls == ((item, 7, "review-key-1", (evidence,)),)


def test_billing_reader_application_port_has_no_sqlalchemy_leak() -> None:
    source = Path("app/application/workbench/ports.py").read_text(encoding="utf-8").lower()

    assert "sqlalchemy" not in source
    assert "app.models" not in source
    assert "session" not in source


def test_billing_evidence_persistence_has_no_provider_ai_or_inference_boundaries() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            Path("app/persistence/workbench_review_repository.py"),
            Path("app/persistence/review_billing_evidence_reader.py"),
            Path("app/application/workbench/evidence.py"),
            Path("app/models/workbench_review_billing_evidence.py"),
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
        "pricelist",
    )
    for token in forbidden:
        assert token not in source


def test_billing_evidence_does_not_reuse_allocation_amount_or_purchase_tax() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (Path("app/persistence/review_billing_evidence_reader.py"),)
    )

    assert "BusinessContextAllocation" not in source
    assert "InvoiceTaxMappingResult" not in source
    assert ".amount" not in source
    assert ".percentage" not in source
    assert "purchase" not in source.lower()


class RecordingBillingEvidenceWriter:
    def __init__(self) -> None:
        self.billing_calls: tuple[tuple[ReviewItem, int, str, tuple[ReviewExecutionBillingEvidence, ...]], ...] = ()

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        raise AssertionError("plain review creation must not be used")

    def create_review_item_with_execution_evidence(self, *args: object, **kwargs: object) -> ReviewItem:
        raise AssertionError("source evidence creation must not be used")

    def create_review_item_with_billing_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        billing_evidence: tuple[ReviewExecutionBillingEvidence, ...],
    ) -> ReviewItem:
        self.billing_calls = (*self.billing_calls, (item, company_id, idempotency_key, billing_evidence))
        return item

    def create_review_item_with_execution_and_billing_evidence(self, *args: object, **kwargs: object) -> ReviewItem:
        raise AssertionError("combined evidence creation must not be used")


class _NullSession:
    pass


def _create_billing_evidence(session: Session) -> None:
    SqlAlchemyReviewRepository(session).create_review_item_with_billing_evidence(
        _review_item(),
        company_id=7,
        idempotency_key="review-key-1",
        billing_evidence=(_billing_evidence(),),
    )


def _billing_evidence(
    *,
    review_id: str = "review-1",
    company_id: int = 7,
    review_version: int = 1,
    unit_price: Decimal = Decimal("150.00"),
) -> ReviewExecutionBillingEvidence:
    return ReviewExecutionBillingEvidence(
        review_id=review_id,
        company_id=company_id,
        review_version=review_version,
        billing_instruction=_billing_instruction(unit_price=unit_price),
    )


def _billing_instruction(
    *,
    quantity: Decimal = Decimal("1.000"),
    unit_price: Decimal = Decimal("150.00"),
) -> CustomerInvoiceBillingInstruction:
    return CustomerInvoiceBillingInstruction(
        billing_key="bill:customer-501:alloc-1",
        customer_id=501,
        currency="try",
        lines=(
            CustomerInvoiceBillingLine(
                allocation_key="ALLOC-1",
                product_id=901,
                description="Managed service recharge",
                quantity=quantity,
                unit_price=unit_price,
                sales_tax_ids=(1901,),
            ),
        ),
    )


def _review_item(*, workflow: WorkflowType = WorkflowType.MANUAL_REVIEW) -> ReviewItem:
    return ReviewItem(
        review_id="review-1",
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


def _source_evidence():
    from tests.unit.test_workbench_review_execution_evidence import _evidence

    return _evidence()


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            WorkbenchReviewItem.__table__,
            WorkbenchReviewExecutionEvidence.__table__,
            WorkbenchReviewBillingEvidence.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session
