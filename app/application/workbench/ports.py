from __future__ import annotations

from typing import Protocol

from app.application.workbench.dto import ReviewItem, ReviewQueueResult
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery


class ReviewQueueReader(Protocol):
    """Read-only port for future Workbench review queue adapters."""

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        pass

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass
