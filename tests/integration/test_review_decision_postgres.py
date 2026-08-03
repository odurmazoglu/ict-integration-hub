from __future__ import annotations

import os
import threading
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.application.workbench import ReviewDecisionCommand, ReviewDecisionType, ReviewItem, ReviewStatus
from app.application.workbench.exceptions import ReviewStateConflictError, ReviewVersionConflictError
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType
from app.db.base import Base
from app.models.workbench_review_decision import WorkbenchReviewDecision
from app.models.workbench_review_item import WorkbenchReviewItem
from app.persistence import SqlAlchemyReviewRepository

pytestmark = pytest.mark.skipif(
    not os.getenv("POSTGRES_TEST_DATABASE_URL"),
    reason="POSTGRES_TEST_DATABASE_URL is required for focused PostgreSQL concurrency validation.",
)


@pytest.fixture()
def engine() -> Engine:
    database_url = os.environ["POSTGRES_TEST_DATABASE_URL"]
    db_engine = create_engine(database_url)
    with db_engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS workbench_review_decisions"))
        connection.execute(text("DROP TABLE IF EXISTS workbench_review_items"))
    Base.metadata.create_all(db_engine, tables=[WorkbenchReviewItem.__table__, WorkbenchReviewDecision.__table__])
    try:
        yield db_engine
    finally:
        with db_engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS workbench_review_decisions"))
            connection.execute(text("DROP TABLE IF EXISTS workbench_review_items"))
        db_engine.dispose()


def test_concurrent_review_decision_submissions_only_one_succeeds(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    with factory() as setup_session:
        SqlAlchemyReviewRepository(setup_session).create_review_item(
            _review_item("review-1"),
            company_id=7,
            idempotency_key="review-key-1",
        )
        setup_session.commit()

    barrier = threading.Barrier(2)
    results: list[str] = []

    def submit(idempotency_key: str) -> None:
        with factory() as session:
            barrier.wait(timeout=10)
            try:
                SqlAlchemyReviewRepository(session).submit_review_decision(
                    _select_workflow_command(idempotency_key=idempotency_key)
                )
                session.commit()
                results.append("success")
            except (ReviewStateConflictError, ReviewVersionConflictError):
                session.rollback()
                results.append("conflict")

    threads = (
        threading.Thread(target=submit, args=("decision-key-1",)),
        threading.Thread(target=submit, args=("decision-key-2",)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert sorted(results) == ["conflict", "success"]
    with factory() as session:
        review = session.query(WorkbenchReviewItem).filter_by(review_id="review-1", company_id=7).one()
        assert review.status == ReviewStatus.DECISION_SUBMITTED.value
        assert review.version == 2
        assert session.query(WorkbenchReviewDecision).count() == 1


def _review_item(review_id: str) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 2),
        currency="TRY",
        total_amount=Decimal("120.00"),
        workflow=WorkflowType.MANUAL_REVIEW,
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


def _select_workflow_command(*, idempotency_key: str) -> ReviewDecisionCommand:
    return ReviewDecisionCommand(
        review_id="review-1",
        company_id=7,
        expected_version=1,
        decision=ReviewDecisionType.SELECT_WORKFLOW,
        selected_workflow=WorkflowType.VENDOR_BILL,
        decided_by="finance.user",
        idempotency_key=idempotency_key,
    )
