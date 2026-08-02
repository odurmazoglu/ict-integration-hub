from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench import (
    ReviewDetailQuery,
    ReviewItem,
    ReviewItemCreationService,
    ReviewItemWriter,
    ReviewQueueQuery,
    ReviewStatus,
    WorkbenchContractError,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewRepository


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[WorkbenchReviewItem.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


def test_repository_creates_pending_review_item_with_version_one(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="invoice-ettn:1")

    assert created.review_id == "review-1"
    assert created.status is ReviewStatus.PENDING_REVIEW
    assert created.workflow is WorkflowType.MANUAL_REVIEW
    assert created.version == 1
    assert created.created_at is not None
    assert created.updated_at is not None


def test_repository_round_trips_decimal_date_warnings_and_structured_reasons(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item(
        "review-1",
        total_amount=Decimal("120.25"),
        review_reasons=(
            ManualReviewReason(
                code=ManualReviewReasonCode.TAX_AMBIGUOUS,
                message="Tax mapping returned more than one candidate.",
                line_number="1",
                tax_index=0,
                candidate_count=2,
                source="tax_mapping",
                details=(("tax_rate", "20"),),
            ),
        ),
    )

    created = repository.create_review_item(item, company_id=7, idempotency_key="invoice-ettn:1")
    loaded = repository.get_review_item(ReviewDetailQuery(review_id=created.review_id, company_id=7))

    assert loaded.invoice_date == date(2026, 8, 2)
    assert loaded.total_amount == Decimal("120.25")
    assert loaded.warnings == ("safe warning",)
    assert loaded.review_reasons[0].code is ManualReviewReasonCode.TAX_AMBIGUOUS
    assert loaded.review_reasons[0].details == (("tax_rate", "20"),)


def test_repository_returns_existing_item_for_identical_idempotency_key(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    item = _review_item("review-1")

    first = repository.create_review_item(item, company_id=7, idempotency_key="same-key")
    second = repository.create_review_item(item, company_id=7, idempotency_key="same-key")

    assert second == first
    assert session.query(WorkbenchReviewItem).count() == 1


def test_repository_rejects_conflicting_content_for_same_idempotency_key(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="same-key")

    with pytest.raises(ReviewIdempotencyConflictError) as error:
        repository.create_review_item(
            _review_item("review-2", invoice_number="INV-2"),
            company_id=7,
            idempotency_key="same-key",
        )

    assert "INV-1" not in str(error.value)
    assert "INV-2" not in str(error.value)


def test_repository_scopes_idempotency_key_by_company(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="shared-key")
    repository.create_review_item(_review_item("review-2"), company_id=8, idempotency_key="shared-key")

    assert session.query(WorkbenchReviewItem).count() == 2


def test_repository_rejects_duplicate_review_id_with_safe_error(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="key-1")

    with pytest.raises(ReviewDataIntegrityError) as error:
        repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="key-2")

    assert str(error.value) == "Review item already exists."


def test_repository_validates_writer_contract_inputs(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(WorkbenchContractError):
        repository.create_review_item(_review_item("review-1"), company_id=0, idempotency_key="key-1")
    with pytest.raises(WorkbenchContractError):
        repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key=" ")
    with pytest.raises(WorkbenchContractError):
        repository.create_review_item(
            _review_item("review-1", status=ReviewStatus.RESOLVED),
            company_id=7,
            idempotency_key="key-1",
        )
    with pytest.raises(WorkbenchContractError):
        repository.create_review_item(_review_item("review-1", version=2), company_id=7, idempotency_key="key-1")


def test_repository_gets_review_item_by_review_id_and_company(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="key-1")

    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))

    assert loaded.review_id == "review-1"


def test_repository_does_not_leak_review_items_across_companies(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="key-1")

    with pytest.raises(ReviewNotFoundError):
        repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=8))


def test_repository_lists_pending_review_items_with_total_count(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="key-1")
    repository.create_review_item(_review_item("review-2"), company_id=7, idempotency_key="key-2")

    result = repository.list_review_items(ReviewQueueQuery(company_id=7, limit=1, offset=0))

    assert len(result.items) == 1
    assert result.total_count == 2
    assert result.limit == 1
    assert result.offset == 0


def test_repository_lists_with_stable_created_at_then_review_id_order(session: Session) -> None:
    _insert_record(session, review_id="review-b", company_id=7, idempotency_key="key-b", created_at=_time(1))
    _insert_record(session, review_id="review-a", company_id=7, idempotency_key="key-a", created_at=_time(1))
    _insert_record(session, review_id="review-c", company_id=7, idempotency_key="key-c", created_at=_time(2))
    repository = SqlAlchemyReviewRepository(session)

    result = repository.list_review_items(ReviewQueueQuery(company_id=7))

    assert [item.review_id for item in result.items] == ["review-a", "review-b", "review-c"]


def test_repository_filters_by_status(session: Session) -> None:
    _insert_record(session, review_id="pending", company_id=7, idempotency_key="key-1")
    _insert_record(
        session,
        review_id="resolved",
        company_id=7,
        idempotency_key="key-2",
        status=ReviewStatus.RESOLVED,
    )
    repository = SqlAlchemyReviewRepository(session)

    result = repository.list_review_items(ReviewQueueQuery(company_id=7, status=ReviewStatus.RESOLVED))

    assert [item.review_id for item in result.items] == ["resolved"]


def test_repository_filters_by_supplier_tax_number(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(
        _review_item("review-1", supplier_tax_number="1234567890"),
        company_id=7,
        idempotency_key="key-1",
    )
    repository.create_review_item(
        _review_item("review-2", supplier_tax_number="9999999999"),
        company_id=7,
        idempotency_key="key-2",
    )

    result = repository.list_review_items(ReviewQueueQuery(company_id=7, supplier_tax_number="1234567890"))

    assert [item.review_id for item in result.items] == ["review-1"]


def test_repository_filters_by_workflow(session: Session) -> None:
    _insert_record(session, review_id="manual", company_id=7, idempotency_key="key-1")
    _insert_record(
        session,
        review_id="vendor-bill",
        company_id=7,
        idempotency_key="key-2",
        workflow=WorkflowType.VENDOR_BILL,
    )
    repository = SqlAlchemyReviewRepository(session)

    result = repository.list_review_items(ReviewQueueQuery(company_id=7, workflow=WorkflowType.VENDOR_BILL))

    assert [item.review_id for item in result.items] == ["vendor-bill"]


def test_repository_filters_by_created_range(session: Session) -> None:
    _insert_record(session, review_id="old", company_id=7, idempotency_key="key-1", created_at=_time(1))
    _insert_record(session, review_id="new", company_id=7, idempotency_key="key-2", created_at=_time(3))
    repository = SqlAlchemyReviewRepository(session)

    result = repository.list_review_items(
        ReviewQueueQuery(
            company_id=7,
            created_from=_time(2),
            created_to=_time(4),
        )
    )

    assert [item.review_id for item in result.items] == ["new"]


def test_repository_paginates_with_offset(session: Session) -> None:
    _insert_record(session, review_id="review-1", company_id=7, idempotency_key="key-1", created_at=_time(1))
    _insert_record(session, review_id="review-2", company_id=7, idempotency_key="key-2", created_at=_time(2))
    repository = SqlAlchemyReviewRepository(session)

    result = repository.list_review_items(ReviewQueueQuery(company_id=7, limit=1, offset=1))

    assert [item.review_id for item in result.items] == ["review-2"]
    assert result.total_count == 2


def test_repository_hydration_rejects_invalid_workflow_value(session: Session) -> None:
    _insert_record(session, review_id="bad", company_id=7, idempotency_key="key-1", workflow="not-real")
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewDataIntegrityError):
        repository.get_review_item(ReviewDetailQuery(review_id="bad", company_id=7))


def test_repository_hydration_rejects_invalid_status_value(session: Session) -> None:
    _insert_record(session, review_id="bad", company_id=7, idempotency_key="key-1", status="not-real")
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewDataIntegrityError):
        repository.get_review_item(ReviewDetailQuery(review_id="bad", company_id=7))


def test_repository_hydration_rejects_malformed_reason_json(session: Session) -> None:
    _insert_record(session, review_id="bad", company_id=7, idempotency_key="key-1", review_reasons=["bad"])
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewDataIntegrityError):
        repository.get_review_item(ReviewDetailQuery(review_id="bad", company_id=7))


def test_repository_hydration_rejects_malformed_warning_json(session: Session) -> None:
    _insert_record(session, review_id="bad", company_id=7, idempotency_key="key-1", warnings=[{"bad": "shape"}])
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewDataIntegrityError):
        repository.get_review_item(ReviewDetailQuery(review_id="bad", company_id=7))


def test_repository_translates_database_failures_without_sensitive_text(session: Session, monkeypatch) -> None:
    repository = SqlAlchemyReviewRepository(session)
    sensitive = "raw driver error token=secret http://provider.example/path?password=secret"
    original = SQLAlchemyError(sensitive)

    def fail(*_args: object, **_kwargs: object) -> object:
        raise original

    monkeypatch.setattr(session, "scalar", fail)

    with pytest.raises(ReviewPersistenceError) as error:
        repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))

    assert str(error.value) == "Review persistence operation failed."
    assert sensitive not in str(error.value)
    assert error.value.__cause__ is original


def test_creation_service_delegates_to_writer_port() -> None:
    item = _review_item("review-1")
    writer = RecordingWriter()

    created = ReviewItemCreationService(writer).create_pending_review_item(
        item,
        company_id=7,
        idempotency_key="key-1",
    )

    assert created is item
    assert writer.calls == ((item, 7, "key-1"),)


def test_review_persistence_adapter_does_not_import_provider_or_erp_boundaries() -> None:
    source = Path("app/persistence/workbench_review_repository.py").read_text(encoding="utf-8").lower()
    forbidden = ("app.connectors", "app.erp", "fastapi", "httpx", "zeep", "account.move", "action_post", "unlink")

    for token in forbidden:
        assert token not in source


def test_application_workbench_contracts_do_not_import_sqlalchemy_or_models() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app/application/workbench").rglob("*.py"))

    assert "sqlalchemy" not in source.lower()
    assert "app.models" not in source.lower()
    assert "app.db" not in source.lower()


def test_review_item_writer_port_is_create_only() -> None:
    assert "create_review_item" in ReviewItemWriter.__dict__
    assert "update" not in ReviewItemWriter.__dict__
    assert "delete" not in ReviewItemWriter.__dict__
    assert "save" not in ReviewItemWriter.__dict__


class RecordingWriter:
    def __init__(self) -> None:
        self.calls: tuple[tuple[ReviewItem, int, str], ...] = ()

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        self.calls = (*self.calls, (item, company_id, idempotency_key))
        return item


def _review_item(
    review_id: str,
    *,
    invoice_number: str = "INV-1",
    supplier_tax_number: str | None = "1234567890",
    total_amount: Decimal = Decimal("120.00"),
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW,
    version: int = 1,
    review_reasons: tuple[ManualReviewReason, ...] | None = None,
) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number=invoice_number,
        supplier_tax_number=supplier_tax_number,
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 2),
        currency="TRY",
        total_amount=total_amount,
        workflow=WorkflowType.MANUAL_REVIEW,
        status=status,
        review_reasons=review_reasons
        or (
            ManualReviewReason(
                code=ManualReviewReasonCode.PRODUCT_NOT_FOUND,
                message="Product was not matched deterministically.",
                line_number="1",
                source="product_matching",
            ),
        ),
        warnings=("safe warning",),
        version=version,
    )


def _insert_record(
    session: Session,
    *,
    review_id: str,
    company_id: int,
    idempotency_key: str,
    workflow: WorkflowType | str = WorkflowType.MANUAL_REVIEW,
    status: ReviewStatus | str = ReviewStatus.PENDING_REVIEW,
    created_at: datetime | None = None,
    review_reasons: list[object] | None = None,
    warnings: list[object] | None = None,
) -> None:
    record = WorkbenchReviewItem(
        review_id=review_id,
        company_id=company_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number=f"INV-{review_id}",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 2),
        currency="TRY",
        total_amount=Decimal("120.00"),
        workflow=workflow.value if isinstance(workflow, WorkflowType) else workflow,
        status=status.value if isinstance(status, ReviewStatus) else status,
        review_reasons=review_reasons
        if review_reasons is not None
        else [
            {
                "code": ManualReviewReasonCode.PRODUCT_NOT_FOUND.value,
                "message": "Product was not matched deterministically.",
                "line_number": "1",
                "tax_index": None,
                "candidate_count": None,
                "source": "product_matching",
                "details": [],
            }
        ],
        warnings=warnings if warnings is not None else ["safe warning"],
        version=1,
        idempotency_key=idempotency_key,
    )
    session.add(record)
    session.flush()
    if created_at is not None:
        record.created_at = created_at
        record.updated_at = created_at
    session.commit()


def _time(days: int) -> datetime:
    return datetime(2026, 8, 2, tzinfo=UTC) + timedelta(days=days)
