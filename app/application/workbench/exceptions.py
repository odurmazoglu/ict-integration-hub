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


class ResaleDecisionEligibilityError(ReviewDecisionError):
    """Safe error raised when a RESALE Vendor Bill decision fails product eligibility (P0-PROD-18E-1B).

    Covers a RESALE purpose recorded only for another review version, account-only or
    identifier-free (operating-expense-shaped) decisions, unresolved/ambiguous product
    lines, and any P0-PROD-18E-1A product-eligibility blocker.
    """

    error_category = "resale_decision_eligibility_error"


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


class ProductRemediationCategoryError(ProductRemediationEligibilityError):
    """Safe error raised when a CREATE_NEW_PRODUCT category is rejected before any Odoo write (P0-PROD-18E-2).

    Covers a RESALE request without ``categ_id``, an empty RESALE allowlist, a
    ``categ_id`` that is not exactly allowlisted, a storable RESALE product, a category
    that does not exist in Odoo, a RESALE category without a valid configured purchase
    account, and an ambiguous current-version purchase purpose.
    """

    error_category = "product_remediation_category_rejected"


class ProductRemediationStalePurchasePurposeError(ProductRemediationEligibilityError):
    """Safe error raised when a RESALE purchase purpose exists only for another review version (P0-PROD-18E-2B).

    A purpose is never carried forward or inferred across review versions, and a
    stale RESALE purpose never silently degrades into ordinary non-RESALE product
    remediation: the purpose must be recorded again for the current review version.
    """

    error_category = "product_remediation_purchase_purpose_stale"


class ProductRemediationVerificationError(ProductRemediationError):
    """Safe error raised when a product this workflow created fails post-create verification (P0-PROD-18E-2).

    The Odoo product exists and its identity is persisted (``PRODUCT_CREATED``); it is
    never created again. Supplierinfo is not linked while verification fails, and a
    retry re-runs verification only.
    """

    error_category = "product_remediation_verification_failed"


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


class OperatingExpenseMappingWorkflowError(ApplicationError):
    """Safe base error for the operating-expense-mapping operator orchestration (P0-PROD-15P).

    Distinct from ``app.application.expense_mapping.exceptions.OperatingExpenseMappingError``,
    which covers the lower-level immutable mapping contract/persistence -- this family covers
    only this workflow's own review-linked orchestration (eligibility, supplier resolution,
    selected-account validation).
    """

    error_category = "operating_expense_mapping_workflow_error"


class OperatingExpenseMappingEligibilityError(OperatingExpenseMappingWorkflowError):
    """Safe error raised when the review is not eligible for an operating-expense mapping.

    Covers a stale ``expected_version``, a review that is no longer pending, and a review
    whose reasons no longer carry OPERATING_EXPENSE_MAPPING_REQUIRED.
    """

    error_category = "operating_expense_mapping_eligibility_error"


class OperatingExpenseMappingSupplierUnresolvedError(OperatingExpenseMappingWorkflowError):
    """Safe error raised when no accepted supplier resolution yields a resolved partner_id.

    Mirrors ``ProductRemediationSupplierUnresolvedError`` exactly: the vendor_partner_id an
    operating-expense mapping is keyed on is derived only from the review's own accepted
    ``SupplierRemediationEffect``, never inferred or accepted from the caller.
    """

    error_category = "operating_expense_mapping_supplier_unresolved"


class OperatingExpenseMappingAccountInvalidError(OperatingExpenseMappingWorkflowError):
    """Safe error raised when the selected expense account does not exist, is not an
    eligible operating-expense account, or is not scoped to the request's company."""

    error_category = "operating_expense_mapping_account_invalid"


class PurchasePurposeError(ApplicationError):
    """Safe base error for the review-scoped purchase-purpose orchestration (P0-PROD-15T)."""

    error_category = "purchase_purpose_error"


class PurchasePurposeEligibilityError(PurchasePurposeError):
    """Safe error raised when the review is not eligible to record a purchase purpose.

    Covers a stale ``expected_version``, a review that is no longer pending, and a
    review whose reasons carry no operating-expense-shaped blocker to explain.
    """

    error_category = "purchase_purpose_eligibility_error"


class PurchasePurposeConflictError(PurchasePurposeError):
    """Safe error raised when a different purchase-purpose resolution already exists
    for this exact review version."""

    error_category = "purchase_purpose_conflict_error"


class AccountingResolutionError(ApplicationError):
    """Safe base error for the review-scoped accounting-resolution orchestration (P0-PROD-15T)."""

    error_category = "accounting_resolution_error"


class AccountingResolutionEligibilityError(AccountingResolutionError):
    """Safe error raised when the review is not eligible for an accounting resolution.

    Covers a stale ``expected_version``, a review that is no longer pending, and a
    review whose reasons carry no operating-expense-shaped blocker to resolve.
    """

    error_category = "accounting_resolution_eligibility_error"


class AccountingResolutionPurposeRequiredError(AccountingResolutionError):
    """Safe error raised when no accepted PurchasePurposeResolution exists yet for this
    exact review version -- purpose must be recorded before an accounting treatment."""

    error_category = "accounting_resolution_purpose_required"


class AccountingResolutionPurposeUnsupportedError(AccountingResolutionError):
    """Safe error raised when the recorded purchase purpose (e.g. RESALE,
    CUSTOMER_PROJECT) has no implemented accounting treatment yet. Never silently
    reinterpreted as a plain expense."""

    error_category = "accounting_resolution_purpose_unsupported"


class AccountingResolutionConflictError(AccountingResolutionError):
    """Safe error raised when a different accounting resolution already exists for
    this exact review version."""

    error_category = "accounting_resolution_conflict_error"


class ExecutionEvidenceRecoveryError(ApplicationError):
    """Safe base error for the review-scoped execution-evidence recovery/repair
    operation (P0-PROD-15Z)."""

    error_category = "execution_evidence_recovery_error"


class ExecutionEvidenceRecoveryEligibilityError(ExecutionEvidenceRecoveryError):
    """Safe error raised when the review is not eligible for execution-evidence
    recovery: not pending review, not currently an execution-capable workflow
    (vendor_bill), or still carrying an actionable manual-review reason. Recovery
    only ever materializes derived evidence for an already fully-resolved review --
    it never makes an unresolved review ready."""

    error_category = "execution_evidence_recovery_eligibility_error"


class ExecutionEvidenceRecoverySourceMissingError(ExecutionEvidenceRecoveryError):
    """Safe error raised when the review's immutable source invoice evidence is
    missing -- recovery has nothing to recompute execution evidence from."""

    error_category = "execution_evidence_recovery_source_missing"


class ExecutionEvidenceRecoveryMismatchError(ExecutionEvidenceRecoveryError):
    """Safe error raised when recomputing the review's effective classification
    right now does not reproduce the persisted current reasons/workflow exactly.
    Recovery fails closed rather than silently accepting business state that has
    drifted since the review last advanced."""

    error_category = "execution_evidence_recovery_mismatch"


class ExecutionEvidenceRecoveryBuildError(ExecutionEvidenceRecoveryError):
    """Safe error raised when the recomputed effective decision, despite matching
    the review's persisted reasons/workflow, still cannot produce a buildable
    execution-evidence result."""

    error_category = "execution_evidence_recovery_build_error"


class ExecutionEvidenceRecoveryConflictError(ExecutionEvidenceRecoveryError):
    """Safe error raised when execution evidence already exists for this review
    version and materially conflicts with the freshly recomputed evidence. Recovery
    never overwrites existing evidence silently."""

    error_category = "execution_evidence_recovery_conflict"


class PurchaseAccountDiscoveryError(ApplicationError):
    """Safe base error for the read-only purchase-account discovery (P0-PROD-18D)."""

    error_category = "purchase_account_discovery_error"


class PurchaseAccountProductNotFoundError(PurchaseAccountDiscoveryError):
    """Safe error raised when the product does not exist or is not visible to this company."""

    error_category = "purchase_account_product_not_found"


class PurchaseAccountCompanyContextError(PurchaseAccountDiscoveryError):
    """Safe error raised when Odoo's company context for company-dependent accounting
    fields cannot be proven to be exactly the requesting company. Discovery fails
    closed rather than presenting another company's configuration."""

    error_category = "purchase_account_company_context_unverified"
