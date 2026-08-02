from __future__ import annotations

from app.application.exceptions import ApplicationError


class WorkbenchContractError(ApplicationError):
    """Safe validation error for Import Workbench application contracts."""

    error_category = "workbench_contract_error"


class ReviewPersistenceError(ApplicationError):
    """Safe base error for Import Workbench review persistence failures."""

    error_category = "review_persistence_error"


class ReviewNotFoundError(ReviewPersistenceError):
    """Safe error raised when a company-scoped review item cannot be found."""

    error_category = "review_not_found"


class ReviewIdempotencyConflictError(ReviewPersistenceError):
    """Safe error raised when an idempotency key is reused for different content."""

    error_category = "review_idempotency_conflict"


class ReviewDataIntegrityError(ReviewPersistenceError):
    """Safe error raised when persisted review data cannot hydrate into contracts."""

    error_category = "review_data_integrity_error"
