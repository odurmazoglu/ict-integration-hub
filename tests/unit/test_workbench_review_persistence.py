from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.application.workbench import (
    BusinessContextDecision,
    LineResolution,
    ReviewDecisionCommand,
    ReviewDecisionType,
    ReviewDecisionWriter,
    ReviewDetailQuery,
    ReviewItem,
    ReviewItemCreationService,
    ReviewItemWriter,
    ReviewQueueQuery,
    ReviewStatus,
    TaxResolution,
    WorkbenchContractError,
)
from app.application.workbench.exceptions import (
    ReviewDataIntegrityError,
    ReviewDecisionDataIntegrityError,
    ReviewDecisionError,
    ReviewDecisionIdempotencyConflictError,
    ReviewIdempotencyConflictError,
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewStateConflictError,
    ReviewVersionConflictError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewRepository


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[WorkbenchReviewItem.__table__, WorkbenchReviewDecision.__table__])
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


def test_repository_round_trips_four_fractional_digit_amount_without_business_value_loss(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item(
        _review_item("review-1", total_amount=Decimal("259.2000")),
        company_id=7,
        idempotency_key="invoice-ettn:1",
    )
    loaded = repository.get_review_item(ReviewDetailQuery(review_id=created.review_id, company_id=7))

    assert loaded.total_amount == Decimal("259.2000")


def test_repository_treats_hydrated_two_decimal_amount_as_idempotently_identical(session: Session) -> None:
    _insert_record(
        session,
        review_id="review-1",
        company_id=7,
        idempotency_key="same-key",
        invoice_number="INV-1",
        total_amount=Decimal("259.20"),
    )
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item(
        _review_item("review-1", total_amount=Decimal("259.2000")),
        company_id=7,
        idempotency_key="same-key",
    )

    assert created.total_amount == Decimal("259.20")
    assert session.query(WorkbenchReviewItem).count() == 1


def test_repository_rejects_genuinely_different_amount_for_same_idempotency_key(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(
        _review_item("review-1", total_amount=Decimal("259.2000")),
        company_id=7,
        idempotency_key="same-key",
    )

    with pytest.raises(ReviewIdempotencyConflictError):
        repository.create_review_item(
            _review_item("review-1", total_amount=Decimal("259.200001")),
            company_id=7,
            idempotency_key="same-key",
        )


def test_repository_round_trips_amount_with_maximum_supported_scale(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    created = repository.create_review_item(
        _review_item("review-1", total_amount=Decimal("123.123456")),
        company_id=7,
        idempotency_key="invoice-ettn:1",
    )

    assert created.total_amount == Decimal("123.123456")


def test_repository_rejects_amount_exceeding_supported_scale(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(WorkbenchContractError) as error:
        repository.create_review_item(
            _review_item("review-1", total_amount=Decimal("1.1234567")),
            company_id=7,
            idempotency_key="invoice-ettn:1",
        )

    assert str(error.value) == "total_amount supports at most 6 fractional digits."


def test_repository_rejects_amount_exceeding_supported_precision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(WorkbenchContractError) as error:
        repository.create_review_item(
            _review_item("review-1", total_amount=Decimal("1000000000000000000")),
            company_id=7,
            idempotency_key="invoice-ettn:1",
        )

    assert str(error.value) == "total_amount supports at most 24 total digits."


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


def test_repository_submits_select_workflow_decision_and_persists_explicit_content(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")
    command = _select_workflow_command(
        line_resolutions=(LineResolution(line_number="1", selected_product_id=10),),
        tax_resolutions=(TaxResolution(line_number="1", tax_index=0, selected_tax_id=20),),
        business_context=BusinessContextDecision(sales_order_id=30, analytic_account_id=40),
        comment="Reviewed by finance.",
        selected_partner_id=50,
        decided_by="finance.user",
    )

    acknowledgement = repository.submit_review_decision(command)

    assert acknowledgement.accepted is True
    assert acknowledgement.review_id == "review-1"
    assert acknowledgement.status is ReviewStatus.DECISION_SUBMITTED
    assert acknowledgement.version == 2
    assert acknowledgement.decision is ReviewDecisionType.SELECT_WORKFLOW
    assert acknowledgement.selected_workflow is WorkflowType.VENDOR_BILL

    record = session.scalar(select(WorkbenchReviewDecision).where(WorkbenchReviewDecision.review_id == "review-1"))
    assert record is not None
    assert record.company_id == 7
    assert record.review_version_before == 1
    assert record.review_version_after == 2
    assert record.decision_type == ReviewDecisionType.SELECT_WORKFLOW.value
    assert record.selected_workflow == WorkflowType.VENDOR_BILL.value
    assert record.selected_partner_id == 50
    assert record.line_resolutions == [{"line_number": "1", "selected_product_id": 10}]
    assert record.tax_resolutions == [{"line_number": "1", "tax_index": 0, "selected_tax_id": 20}]
    assert record.business_context == {"sales_order_id": 30, "analytic_account_id": 40}
    assert record.comment == "Reviewed by finance."
    assert record.decided_by == "finance.user"
    assert record.idempotency_key == "decision-key-1"
    assert record.submitted_at is not None


def test_repository_submits_dismiss_decision(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")

    acknowledgement = repository.submit_review_decision(_dismiss_command())

    assert acknowledgement.status is ReviewStatus.DISMISSED
    assert acknowledgement.version == 2
    assert acknowledgement.decision is ReviewDecisionType.DISMISS
    assert acknowledgement.selected_workflow is None
    record = session.scalar(select(WorkbenchReviewDecision).where(WorkbenchReviewDecision.review_id == "review-1"))
    assert record is not None
    assert record.selected_workflow is None


def test_repository_updates_review_status_and_version_once(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")

    repository.submit_review_decision(_select_workflow_command())

    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.status is ReviewStatus.DECISION_SUBMITTED
    assert loaded.version == 2


def test_repository_wrong_expected_version_raises_conflict_without_mutation(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1", version=1), company_id=7, idempotency_key="review-key-1")

    with pytest.raises(ReviewVersionConflictError) as error:
        repository.submit_review_decision(_select_workflow_command(expected_version=2))

    assert str(error.value) == "Review item version does not match expected_version."
    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.status is ReviewStatus.PENDING_REVIEW
    assert loaded.version == 1
    assert session.query(WorkbenchReviewDecision).count() == 0


def test_repository_non_pending_review_raises_state_conflict_without_mutation(session: Session) -> None:
    _insert_record(
        session,
        review_id="review-1",
        company_id=7,
        idempotency_key="review-key-1",
        status=ReviewStatus.RESOLVED,
    )
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewStateConflictError):
        repository.submit_review_decision(_select_workflow_command())

    assert session.query(WorkbenchReviewDecision).count() == 0


def test_repository_decision_submission_is_company_scoped(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=8, idempotency_key="review-key-1")

    with pytest.raises(ReviewNotFoundError):
        repository.submit_review_decision(_select_workflow_command(company_id=7))

    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=8))
    assert loaded.status is ReviewStatus.PENDING_REVIEW
    assert loaded.version == 1


def test_repository_decision_submission_not_found_is_safe(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewNotFoundError) as error:
        repository.submit_review_decision(_select_workflow_command())

    assert str(error.value) == "Review item was not found."


def test_repository_returns_existing_acknowledgement_for_identical_decision_idempotency_key(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")
    command = _select_workflow_command()

    first = repository.submit_review_decision(command)
    second = repository.submit_review_decision(command)

    assert second == first
    assert session.query(WorkbenchReviewDecision).count() == 1
    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.version == 2


def test_repository_rejects_conflicting_decision_idempotency_reuse_without_mutation(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")
    repository.submit_review_decision(_select_workflow_command())

    with pytest.raises(ReviewDecisionIdempotencyConflictError) as error:
        repository.submit_review_decision(_select_workflow_command(selected_workflow=WorkflowType.RFQ))

    assert "vendor_bill" not in str(error.value)
    assert "rfq" not in str(error.value)
    assert session.query(WorkbenchReviewDecision).count() == 1


def test_repository_rejects_malformed_persisted_decision_data_safely(session: Session) -> None:
    _insert_record(session, review_id="review-1", company_id=7, idempotency_key="review-key-1")
    _insert_decision_record(session, decision_type="not-real")
    repository = SqlAlchemyReviewRepository(session)

    with pytest.raises(ReviewDecisionDataIntegrityError):
        repository.submit_review_decision(_select_workflow_command())


def test_repository_rolls_back_review_update_when_decision_insert_fails(session: Session, monkeypatch) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")
    original_add = session.add
    sensitive = SQLAlchemyError("driver insert failure password=secret")

    def fail_on_decision_add(record: object) -> None:
        if isinstance(record, WorkbenchReviewDecision):
            raise sensitive
        original_add(record)

    monkeypatch.setattr(session, "add", fail_on_decision_add)

    with pytest.raises(ReviewDecisionError) as error:
        repository.submit_review_decision(_select_workflow_command())

    assert str(error.value) == "Review decision persistence operation failed."
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is sensitive
    monkeypatch.undo()
    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.status is ReviewStatus.PENDING_REVIEW
    assert loaded.version == 1
    assert session.query(WorkbenchReviewDecision).count() == 0


def test_repository_rolls_back_when_review_update_fails(session: Session, monkeypatch) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")
    sensitive = SQLAlchemyError("driver update failure token=secret")

    def fail_execute(*_args: object, **_kwargs: object) -> object:
        raise sensitive

    monkeypatch.setattr(session, "execute", fail_execute)

    with pytest.raises(ReviewDecisionError) as error:
        repository.submit_review_decision(_select_workflow_command())

    assert str(error.value) == "Review decision persistence operation failed."
    assert "secret" not in str(error.value)
    assert error.value.__cause__ is sensitive
    monkeypatch.undo()
    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.status is ReviewStatus.PENDING_REVIEW
    assert loaded.version == 1
    assert session.query(WorkbenchReviewDecision).count() == 0


def test_repository_preserves_original_review_reasons_after_decision(session: Session) -> None:
    reason = ManualReviewReason(
        code=ManualReviewReasonCode.TAX_AMBIGUOUS,
        message="Tax mapping returned more than one candidate.",
        line_number="1",
        tax_index=0,
        source="tax_mapping",
    )
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(
        _review_item("review-1", review_reasons=(reason,)),
        company_id=7,
        idempotency_key="review-key-1",
    )

    repository.submit_review_decision(_select_workflow_command())

    loaded = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert loaded.review_reasons == (reason,)


def test_repository_queue_and_detail_reflect_decision_status(session: Session) -> None:
    repository = SqlAlchemyReviewRepository(session)
    repository.create_review_item(_review_item("review-1"), company_id=7, idempotency_key="review-key-1")

    repository.submit_review_decision(_select_workflow_command())

    pending = repository.list_review_items(ReviewQueueQuery(company_id=7))
    submitted = repository.list_review_items(ReviewQueueQuery(company_id=7, status=ReviewStatus.DECISION_SUBMITTED))
    detail = repository.get_review_item(ReviewDetailQuery(review_id="review-1", company_id=7))
    assert pending.items == ()
    assert [item.review_id for item in submitted.items] == ["review-1"]
    assert detail.status is ReviewStatus.DECISION_SUBMITTED
    assert detail.version == 2


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


def test_review_persistence_adapter_does_not_convert_money_through_float() -> None:
    source = Path("app/persistence/workbench_review_repository.py").read_text(encoding="utf-8").lower()

    assert "float(" not in source


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


def test_review_decision_writer_port_is_submit_only() -> None:
    assert "submit_review_decision" in ReviewDecisionWriter.__dict__
    assert "create_review_item" not in ReviewDecisionWriter.__dict__
    assert "list_review_items" not in ReviewDecisionWriter.__dict__
    assert "delete" not in ReviewDecisionWriter.__dict__


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
    invoice_number: str | None = None,
    workflow: WorkflowType | str = WorkflowType.MANUAL_REVIEW,
    status: ReviewStatus | str = ReviewStatus.PENDING_REVIEW,
    created_at: datetime | None = None,
    total_amount: Decimal = Decimal("120.00"),
    review_reasons: list[object] | None = None,
    warnings: list[object] | None = None,
) -> None:
    record = WorkbenchReviewItem(
        review_id=review_id,
        company_id=company_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number=invoice_number if invoice_number is not None else f"INV-{review_id}",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 2),
        currency="TRY",
        total_amount=total_amount,
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


def _insert_decision_record(
    session: Session,
    *,
    decision_type: str = ReviewDecisionType.SELECT_WORKFLOW.value,
    review_id: str = "review-1",
    company_id: int = 7,
    idempotency_key: str = "decision-key-1",
) -> None:
    session.add(
        WorkbenchReviewDecision(
            decision_id=f"decision-{review_id}",
            review_id=review_id,
            company_id=company_id,
            review_version_before=1,
            review_version_after=2,
            decision_type=decision_type,
            selected_workflow=WorkflowType.VENDOR_BILL.value,
            selected_partner_id=None,
            line_resolutions=[],
            tax_resolutions=[],
            business_context=None,
            comment=None,
            decided_by="finance.user",
            idempotency_key=idempotency_key,
        )
    )
    session.commit()


def _select_workflow_command(
    *,
    review_id: str = "review-1",
    company_id: int = 7,
    expected_version: int = 1,
    selected_workflow: WorkflowType = WorkflowType.VENDOR_BILL,
    selected_partner_id: int | None = None,
    line_resolutions: tuple[LineResolution, ...] = (),
    tax_resolutions: tuple[TaxResolution, ...] = (),
    business_context: BusinessContextDecision | None = None,
    comment: str | None = None,
    decided_by: str = "finance.user",
    idempotency_key: str = "decision-key-1",
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=review_id,
        company_id=company_id,
        expected_version=expected_version,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=selected_workflow,
        selected_partner_id=selected_partner_id,
        line_resolutions=line_resolutions,
        tax_resolutions=tax_resolutions,
        business_context=business_context,
        comment=comment,
        decided_by=decided_by,
        idempotency_key=idempotency_key,
    )


def _dismiss_command(
    *,
    review_id: str = "review-1",
    company_id: int = 7,
    expected_version: int = 1,
    decided_by: str = "finance.user",
    idempotency_key: str = "decision-key-1",
) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id=review_id,
        company_id=company_id,
        expected_version=expected_version,
        decision=ReviewDecisionType.DISMISS,
        decided_by=decided_by,
        idempotency_key=idempotency_key,
    )


def _time(days: int) -> datetime:
    return datetime(2026, 8, 2, tzinfo=UTC) + timedelta(days=days)
