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


class WorkbenchCandidateReadError(ApplicationError):
    """Safe base error for reading Workbench decision candidates from an ERP UI projection."""

    error_category = "workbench_candidate_read_error"


class WorkbenchCandidateNotFoundError(WorkbenchCandidateReadError):
    """Safe error raised when a ready Workbench decision candidate cannot be found."""

    error_category = "workbench_candidate_not_found"


class WorkbenchCandidateDataError(WorkbenchCandidateReadError):
    """Safe error raised when candidate projection data cannot hydrate into contracts."""

    error_category = "workbench_candidate_data_error"


class WorkbenchCandidateUnsupportedDecisionError(WorkbenchCandidateDataError):
    """Safe error raised when Odoo carries a decision unsupported by canonical Hub contracts."""

    error_category = "workbench_candidate_unsupported_decision"


class WorkbenchCandidateAmbiguityError(WorkbenchCandidateReadError):
    """Safe error raised when more than one matching candidate projection exists."""

    error_category = "workbench_candidate_ambiguity"


class WorkbenchProjectionPublishError(ApplicationError):
    """Safe error raised when publishing a Workbench projection to an ERP UI fails."""

    error_category = "workbench_projection_publish_error"


class WorkbenchSubmissionOrchestrationError(ApplicationError):
    """Safe base error for Odoo Workbench decision submission orchestration failures."""

    error_category = "workbench_submission_orchestration_error"


class WorkbenchSubmissionCompanyMismatchError(WorkbenchSubmissionOrchestrationError):
    """Safe error raised when a candidate escapes the requested company scope."""

    error_category = "workbench_submission_company_mismatch"


class WorkbenchErpReferenceValidationError(ApplicationError):
    """Safe base error for Workbench ERP reference validation failures."""

    error_category = "workbench_erp_reference_validation_error"


class WorkbenchErpReferenceNotFoundError(WorkbenchErpReferenceValidationError):
    """Safe error raised when a referenced ERP record cannot be found."""

    error_category = "workbench_erp_reference_not_found"


class WorkbenchErpReferenceCompanyMismatchError(WorkbenchErpReferenceValidationError):
    """Safe error raised when a referenced ERP record is outside the requested company scope."""

    error_category = "workbench_erp_reference_company_mismatch"


class WorkbenchErpReferenceTypeError(WorkbenchErpReferenceValidationError):
    """Safe error raised when a referenced ERP record has an unsupported type."""

    error_category = "workbench_erp_reference_type_error"


class WorkbenchErpReferenceRelationshipError(WorkbenchErpReferenceValidationError):
    """Safe error raised when deterministic ERP reference relationships conflict."""

    error_category = "workbench_erp_reference_relationship_error"


class WorkbenchErpReferenceUnsupportedError(WorkbenchErpReferenceValidationError):
    """Safe error raised when semantic validation for a non-null reference is unsupported."""

    error_category = "workbench_erp_reference_unsupported"
