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


class ReviewQueryError(ApplicationError):
    """Safe error raised when a Workbench review query use case fails unexpectedly."""

    error_category = "review_query_error"


class ReviewDecisionError(ApplicationError):
    """Safe base error for Import Workbench decision submission failures."""

    error_category = "review_decision_error"


class ReviewVersionConflictError(ReviewDecisionError):
    """Safe error raised when a review decision expected_version is stale."""

    error_category = "review_version_conflict"


class ReviewStateConflictError(ReviewDecisionError):
    """Safe error raised when a review item is no longer pending for decision submission."""

    error_category = "review_state_conflict"


class ReviewDecisionIdempotencyConflictError(ReviewDecisionError):
    """Safe error raised when a decision idempotency key is reused for different content."""

    error_category = "review_decision_idempotency_conflict"


class ReviewDecisionDataIntegrityError(ReviewDecisionError):
    """Safe error raised when persisted decision data cannot hydrate into contracts."""

    error_category = "review_decision_data_integrity_error"
