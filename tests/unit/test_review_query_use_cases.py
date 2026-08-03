from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.workbench import GetReviewItemUseCase, ListReviewQueueUseCase, ReviewDetailQuery, ReviewQueueQuery
from app.application.workbench.dto import ReviewItem, ReviewQueueResult, ReviewStatus
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    ReviewPersistenceError,
    ReviewQueryError,
    WorkbenchContractError,
)
from app.application.workflow import ManualReviewReason, ManualReviewReasonCode, WorkflowType


def test_list_review_queue_use_case_delegates_exact_query_once_and_returns_exact_result() -> None:
    query = ReviewQueueQuery(company_id=7, limit=1, offset=5, supplier_tax_number="1234567890")
    result = ReviewQueueResult(items=(_review_item("review-b"), _review_item("review-a")), total_count=99, limit=99)
    reader = FakeReviewQueueReader(list_result=result)

    returned = ListReviewQueueUseCase(review_queue_reader=reader).execute(query)

    assert returned is result
    assert reader.list_queries == (query,)
    assert reader.detail_queries == ()
    assert [item.review_id for item in returned.items] == ["review-b", "review-a"]
    assert returned.total_count == 99
    assert returned.limit == 99


def test_list_review_queue_use_case_rejects_non_query_input() -> None:
    use_case = ListReviewQueueUseCase(review_queue_reader=FakeReviewQueueReader())

    with pytest.raises(WorkbenchContractError):
        use_case.execute("not-a-query")  # type: ignore[arg-type]


def test_list_review_queue_use_case_propagates_workbench_contract_error() -> None:
    error = WorkbenchContractError("safe contract failure")
    use_case = ListReviewQueueUseCase(review_queue_reader=FakeReviewQueueReader(list_error=error))

    with pytest.raises(WorkbenchContractError) as raised:
        use_case.execute(ReviewQueueQuery(company_id=7))

    assert raised.value is error


def test_list_review_queue_use_case_propagates_review_persistence_error() -> None:
    error = ReviewPersistenceError("safe persistence failure")
    use_case = ListReviewQueueUseCase(review_queue_reader=FakeReviewQueueReader(list_error=error))

    with pytest.raises(ReviewPersistenceError) as raised:
        use_case.execute(ReviewQueueQuery(company_id=7))

    assert raised.value is error


def test_list_review_queue_use_case_translates_unexpected_error_safely() -> None:
    sensitive = RuntimeError("sql password=secret postgresql://user:pass@db")
    use_case = ListReviewQueueUseCase(review_queue_reader=FakeReviewQueueReader(list_error=sensitive))

    with pytest.raises(ReviewQueryError) as raised:
        use_case.execute(ReviewQueueQuery(company_id=7))

    assert str(raised.value) == "Review queue query failed."
    assert "secret" not in str(raised.value)
    assert "postgresql://" not in str(raised.value)
    assert raised.value.__cause__ is sensitive


def test_get_review_item_use_case_delegates_exact_query_once_and_returns_exact_item() -> None:
    query = ReviewDetailQuery(review_id="review-1", company_id=7)
    item = _review_item("review-1")
    reader = FakeReviewQueueReader(detail_result=item)

    returned = GetReviewItemUseCase(review_queue_reader=reader).execute(query)

    assert returned is item
    assert reader.detail_queries == (query,)
    assert reader.list_queries == ()
    assert reader.detail_queries[0].review_id == "review-1"
    assert reader.detail_queries[0].company_id == 7


def test_get_review_item_use_case_rejects_non_query_input() -> None:
    use_case = GetReviewItemUseCase(review_queue_reader=FakeReviewQueueReader())

    with pytest.raises(WorkbenchContractError):
        use_case.execute("not-a-query")  # type: ignore[arg-type]


def test_get_review_item_use_case_propagates_review_not_found_error() -> None:
    error = ReviewNotFoundError("Review item was not found.")
    use_case = GetReviewItemUseCase(review_queue_reader=FakeReviewQueueReader(detail_error=error))

    with pytest.raises(ReviewNotFoundError) as raised:
        use_case.execute(ReviewDetailQuery(review_id="review-1", company_id=7))

    assert raised.value is error


def test_get_review_item_use_case_propagates_review_persistence_error() -> None:
    error = ReviewPersistenceError("safe persistence failure")
    use_case = GetReviewItemUseCase(review_queue_reader=FakeReviewQueueReader(detail_error=error))

    with pytest.raises(ReviewPersistenceError) as raised:
        use_case.execute(ReviewDetailQuery(review_id="review-1", company_id=7))

    assert raised.value is error


def test_get_review_item_use_case_translates_unexpected_error_safely() -> None:
    sensitive = RuntimeError("driver error token=secret select * from workbench_review_items")
    use_case = GetReviewItemUseCase(review_queue_reader=FakeReviewQueueReader(detail_error=sensitive))

    with pytest.raises(ReviewQueryError) as raised:
        use_case.execute(ReviewDetailQuery(review_id="review-1", company_id=7))

    assert str(raised.value) == "Review item query failed."
    assert "secret" not in str(raised.value)
    assert "select *" not in str(raised.value).lower()
    assert raised.value.__cause__ is sensitive


def test_review_query_use_case_outputs_remain_immutable() -> None:
    item = _review_item("review-1")
    result = ReviewQueueResult(items=(item,), total_count=1, limit=1)

    with pytest.raises(FrozenInstanceError):
        item.review_id = "changed"
    with pytest.raises(FrozenInstanceError):
        result.total_count = 2


def test_review_query_use_cases_are_exported_from_workbench_package() -> None:
    import app.application.workbench as workbench

    assert workbench.ListReviewQueueUseCase is ListReviewQueueUseCase
    assert workbench.GetReviewItemUseCase is GetReviewItemUseCase


def test_review_query_use_cases_do_not_import_infrastructure_or_provider_boundaries() -> None:
    source = _query_use_case_source()
    forbidden = (
        "sqlalchemy",
        "app.models",
        "app.db",
        "app.persistence",
        "app.connectors",
        "app.erp",
        "odoo",
        "uyumsoft",
        "fastapi",
        "requests",
        "httpx",
        "soap",
        "zeep",
    )

    for token in forbidden:
        assert token not in source


def test_review_query_use_cases_do_not_write_or_execute_workflows() -> None:
    source = _query_use_case_source()
    forbidden = (
        "reviewdecisioncommand",
        "selected_workflow",
        "vendorbillwriter",
        "decisionengine",
        "workflowstrategy",
        "create_review_item",
        "create_draft",
        "account.move",
        "action_post",
        "unlink",
        "commit",
        "rollback",
        "flush",
    )

    for token in forbidden:
        assert token not in source


def test_review_query_use_cases_do_not_use_ai_fuzzy_matching_or_in_memory_query_logic() -> None:
    source = _query_use_case_source()
    forbidden = (
        "ai_advisor",
        "ollama",
        "fuzzy",
        "levenshtein",
        "embedding",
        "similarity",
        "sorted(",
        ".sort(",
        ".offset(",
        ".limit(",
        "where(",
        "select(",
    )

    for token in forbidden:
        assert token not in source


class FakeReviewQueueReader:
    def __init__(
        self,
        *,
        list_result: ReviewQueueResult | None = None,
        detail_result: ReviewItem | None = None,
        list_error: Exception | None = None,
        detail_error: Exception | None = None,
    ) -> None:
        self._list_result = list_result or ReviewQueueResult()
        self._detail_result = detail_result or _review_item("review-1")
        self._list_error = list_error
        self._detail_error = detail_error
        self.list_queries: tuple[ReviewQueueQuery, ...] = ()
        self.detail_queries: tuple[ReviewDetailQuery, ...] = ()

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        self.list_queries = (*self.list_queries, query)
        if self._list_error is not None:
            raise self._list_error
        return self._list_result

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        self.detail_queries = (*self.detail_queries, query)
        if self._detail_error is not None:
            raise self._detail_error
        return self._detail_result


def _review_item(review_id: str) -> ReviewItem:
    return ReviewItem(
        review_id=review_id,
        invoice_id=f"invoice-{review_id}",
        invoice_number="INV-1",
        supplier_tax_number="1234567890",
        supplier_name="Supplier Display",
        invoice_date=date(2026, 8, 3),
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
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        updated_at=datetime(2026, 8, 3, tzinfo=UTC),
        version=1,
    )


def _query_use_case_source() -> str:
    return Path("app/application/workbench/query_use_cases.py").read_text(encoding="utf-8").lower()
