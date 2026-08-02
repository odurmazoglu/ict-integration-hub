from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.application.queries import Query
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import WorkflowType

DEFAULT_REVIEW_QUEUE_LIMIT = 50
MAX_REVIEW_QUEUE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ReviewQueueQuery(Query):
    """Read-only query for review-required Workbench items."""

    company_id: int
    status: ReviewStatus = ReviewStatus.PENDING_REVIEW
    limit: int = DEFAULT_REVIEW_QUEUE_LIMIT
    offset: int = 0
    created_from: datetime | None = None
    created_to: datetime | None = None
    supplier_tax_number: str | None = None
    workflow: WorkflowType | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        if type(self.limit) is not int or self.limit <= 0:
            raise WorkbenchContractError("limit must be positive.")
        if self.limit > MAX_REVIEW_QUEUE_LIMIT:
            raise WorkbenchContractError("limit exceeds maximum review queue size.")
        if type(self.offset) is not int or self.offset < 0:
            raise WorkbenchContractError("offset must be zero or greater.")
        if self.created_from is not None and self.created_to is not None and self.created_from > self.created_to:
            raise WorkbenchContractError("created_from must not be after created_to.")
        if self.supplier_tax_number is not None and not self.supplier_tax_number.strip():
            raise WorkbenchContractError("supplier_tax_number filter must be exact and non-empty.")


@dataclass(frozen=True, slots=True)
class ReviewDetailQuery(Query):
    """Read-only query for one Workbench review item."""

    review_id: str
    company_id: int

    def __post_init__(self) -> None:
        if self.review_id is None or not self.review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
