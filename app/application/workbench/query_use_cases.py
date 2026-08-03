from __future__ import annotations

from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.dto import ReviewItem, ReviewQueueResult
from app.application.workbench.exceptions import ReviewQueryError, WorkbenchContractError
from app.application.workbench.ports import ReviewQueueReader
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery


class ListReviewQueueUseCase:
    """Application boundary for listing Workbench review items."""

    def __init__(self, *, review_queue_reader: ReviewQueueReader) -> None:
        self._review_queue_reader = review_queue_reader

    def execute(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        if not isinstance(query, ReviewQueueQuery):
            raise WorkbenchContractError("ReviewQueueQuery is required.")
        return _translate_query_failure(
            lambda: self._review_queue_reader.list_review_items(query),
            "Review queue query failed.",
        )


class GetReviewItemUseCase:
    """Application boundary for retrieving one company-scoped Workbench review item."""

    def __init__(self, *, review_queue_reader: ReviewQueueReader) -> None:
        self._review_queue_reader = review_queue_reader

    def execute(self, query: ReviewDetailQuery) -> ReviewItem:
        if not isinstance(query, ReviewDetailQuery):
            raise WorkbenchContractError("ReviewDetailQuery is required.")
        return _translate_query_failure(
            lambda: self._review_queue_reader.get_review_item(query),
            "Review item query failed.",
        )


def _translate_query_failure[ResultT](operation: Callable[[], ResultT], fallback_message: str) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ReviewQueryError(fallback_message) from exc
