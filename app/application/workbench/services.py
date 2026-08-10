from __future__ import annotations

from app.application.workbench.dto import ReviewItem
from app.application.workbench.evidence import ReviewExecutionEvidence
from app.application.workbench.ports import ReviewItemWriter


class ReviewItemCreationService:
    """Application service for idempotent creation of pending review items."""

    def __init__(self, writer: ReviewItemWriter) -> None:
        self._writer = writer

    def create_pending_review_item(self, item: ReviewItem, *, company_id: int, idempotency_key: str) -> ReviewItem:
        return self._writer.create_review_item(item, company_id=company_id, idempotency_key=idempotency_key)

    def create_pending_review_item_with_execution_evidence(
        self,
        item: ReviewItem,
        *,
        company_id: int,
        idempotency_key: str,
        evidence: ReviewExecutionEvidence,
    ) -> ReviewItem:
        return self._writer.create_review_item_with_execution_evidence(
            item,
            company_id=company_id,
            idempotency_key=idempotency_key,
            evidence=evidence,
        )
