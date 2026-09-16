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


class SupplierResolutionError(ApplicationError):
    """Safe base error for explicit supplier-resolution policy validation and persistence."""

    error_category = "supplier_resolution_error"


class SupplierResolutionContractError(SupplierResolutionError):
    """Safe error raised when a SupplierResolution is structurally invalid."""

    error_category = "supplier_resolution_contract_error"


class SupplierResolutionNotFoundError(SupplierResolutionError):
    """Safe error raised when no supplier resolution exists for the review version."""

    error_category = "supplier_resolution_not_found"


class SupplierResolutionPartnerNotFoundError(SupplierResolutionError):
    """Safe error raised when a MATCH_EXISTING selected partner cannot be read."""

    error_category = "supplier_resolution_partner_not_found"


class SupplierResolutionPartnerInactiveError(SupplierResolutionError):
    """Safe error raised when a MATCH_EXISTING selected partner is archived/inactive."""

    error_category = "supplier_resolution_partner_inactive"


class SupplierResolutionPartnerMismatchError(SupplierResolutionError):
    """Safe error raised when a MATCH_EXISTING selected partner fails exact VAT identity."""

    error_category = "supplier_resolution_partner_mismatch"


class SupplierResolutionDataIntegrityError(SupplierResolutionError):
    """Safe error raised when persisted supplier-resolution evidence cannot hydrate."""

    error_category = "supplier_resolution_data_integrity_error"


class SupplierResolutionConflictError(SupplierResolutionError):
    """Safe error raised when a different supplier resolution already exists for the review version."""

    error_category = "supplier_resolution_conflict"


class SupplierResolutionRaceError(SupplierResolutionError):
    """Safe error raised when a concurrent request won the reservation for this review version.

    The losing request must not proceed to any Odoo write. A retry re-enters through
    the pre-check and either resumes the (now committed) reservation or conflicts.
    """

    error_category = "supplier_resolution_race"


class SupplierResolutionOneOffVendorNotHubOwnedError(SupplierResolutionError):
    """Safe error raised when ONE_OFF_VENDOR's exact-VAT lookup matches an existing Odoo
    partner the Hub never created/owns via ONE_OFF_VENDOR (P0-PROD-08H).

    Fails closed rather than silently adopting a pre-existing permanent supplier (or
    any other partner not provably created by this lifecycle) as retirement-eligible.
    Use MATCH_EXISTING or CREATE_PERMANENT_SUPPLIER for this vendor instead.
    """

    error_category = "supplier_resolution_one_off_vendor_not_hub_owned"


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


class ProductRemediationError(ApplicationError):
    """Safe base error for the CREATE_NEW_PRODUCT remediation orchestration (P0-PROD-07G)."""

    error_category = "product_remediation_error"


class ProductRemediationContractError(ProductRemediationError):
    """Safe error raised when a CreateNewProductCommand is structurally invalid."""

    error_category = "product_remediation_contract_error"


class ProductRemediationEligibilityError(ProductRemediationError):
    """Safe error raised when the review/line is not eligible for CREATE_NEW_PRODUCT.

    Covers a stale ``expected_version``, a review that is no longer pending, a line
    that no longer carries PRODUCT_NOT_FOUND, and a source line with no usable
    ``seller_item_code`` -- every case where v1 must fail closed before any write.
    """

    error_category = "product_remediation_eligibility_error"


class ProductRemediationSupplierUnresolvedError(ProductRemediationError):
    """Safe error raised when no accepted supplier resolution yields a resolved partner_id."""

    error_category = "product_remediation_supplier_unresolved"


class ProductRemediationConflictError(ProductRemediationError):
    """Safe error raised when persisted state disagrees with the current request.

    Covers both a different reservation already recorded for this exact review
    line, and a supplier-product identity claim/supplierinfo that resolves to a
    different product than expected -- both are terminal, not retryable.
    """

    error_category = "product_remediation_conflict"


class ProductRemediationRaceError(ProductRemediationError):
    """Safe error raised when a concurrent request currently owns this identity claim.

    The identity (company_id, resolved_supplier_partner_id, seller_item_code) is
    being created by another in-flight request right now; this loser must not
    create a second product. Retryable: a later retry will either observe the
    identity resolved (and reuse it) or race again.
    """

    error_category = "product_remediation_race"


class ProductRemediationIdentityAmbiguousError(ProductRemediationError):
    """Safe error raised when an existing supplierinfo identity is ambiguous/inconsistent."""

    error_category = "product_remediation_identity_ambiguous"


class ProductRemediationDataIntegrityError(ProductRemediationError):
    """Safe error raised when persisted product remediation state cannot hydrate into contracts."""

    error_category = "product_remediation_data_integrity_error"


class OneOffVendorRetirementError(ApplicationError):
    """Safe base error for the ONE_OFF_VENDOR archive-last lifecycle (P0-PROD-08H)."""

    error_category = "one_off_vendor_retirement_error"


class OneOffVendorRetirementContractError(OneOffVendorRetirementError):
    """Safe error raised when an ArchiveOneOffVendorCommand is structurally invalid."""

    error_category = "one_off_vendor_retirement_contract_error"


class OneOffVendorRetirementConflictError(OneOffVendorRetirementError):
    """Safe error raised when a different retirement row already exists for this review version."""

    error_category = "one_off_vendor_retirement_conflict"


class OneOffVendorRetirementDataIntegrityError(OneOffVendorRetirementError):
    """Safe error raised when persisted retirement state cannot hydrate into contracts."""

    error_category = "one_off_vendor_retirement_data_integrity_error"
