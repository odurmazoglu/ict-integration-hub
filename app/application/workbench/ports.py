from __future__ import annotations

from typing import Protocol

from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewItem, ReviewQueueResult
from app.application.workbench.queries import ReviewDetailQuery, ReviewQueueQuery


class ReviewQueueReader(Protocol):
    """Read-only port for future Workbench review queue adapters."""

    def list_review_items(self, query: ReviewQueueQuery) -> ReviewQueueResult:
        pass

    def get_review_item(self, query: ReviewDetailQuery) -> ReviewItem:
        pass


class ReviewItemWriter(Protocol):
    """Write port for idempotent creation of pending Workbench review items."""

    def create_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        pass


class ReviewDecisionWriter(Protocol):
    """Write port for explicit user decision submission against a pending review item."""

    def submit_review_decision(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        pass
