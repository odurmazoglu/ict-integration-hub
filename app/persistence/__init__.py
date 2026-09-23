"""Persistence adapters for ICT IPP application ports."""

from app.persistence.accepted_billing_evidence_reader import SqlAlchemyAcceptedBillingEvidenceReader
from app.persistence.execution_runtime_repository import SqlAlchemyExecutionRuntimeRepository
from app.persistence.execution_source_invoice_reader import SqlAlchemyExecutionSourceInvoiceReader
from app.persistence.import_history import SqlAlchemyImportHistory
from app.persistence.operating_expense_mapping_repository import SqlAlchemyOperatingExpenseMappingRepository
from app.persistence.quotation_scenario_evidence_repository import SqlAlchemyQuotationScenarioEvidenceRepository
from app.persistence.review_billing_evidence_reader import SqlAlchemyReviewBillingEvidenceReader
from app.persistence.review_classification_evidence_reader import SqlAlchemyReviewClassificationEvidenceReader
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.persistence.vendor_bill_execution_evidence_reader import SqlAlchemyVendorBillExecutionEvidenceReader
from app.persistence.workbench_review_accounting_resolution_repository import (
    SqlAlchemyReviewAccountingResolutionRepository,
)
from app.persistence.workbench_review_one_off_vendor_retirement_repository import (
    SqlAlchemyReviewOneOffVendorRetirementRepository,
)
from app.persistence.workbench_review_product_identity_claim_repository import (
    SqlAlchemyReviewProductIdentityClaimRepository,
)
from app.persistence.workbench_review_product_remediation_reservation_repository import (
    SqlAlchemyReviewProductRemediationReservationRepository,
)
from app.persistence.workbench_review_purchase_purpose_resolution_repository import (
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
)
from app.persistence.workbench_review_repository import SqlAlchemyReviewRepository
from app.persistence.workbench_review_source_invoice_reader import SqlAlchemyReviewSourceInvoiceEvidenceReader
from app.persistence.workbench_review_supplier_remediation_effect_reader import (
    SqlAlchemyReviewSupplierRemediationEffectRepository,
)
from app.persistence.workbench_review_supplier_resolution_reader import SqlAlchemyReviewSupplierResolutionRepository
from app.persistence.write_authorization_repository import SqlAlchemyWriteAuthorizationRepository

__all__ = [
    "SqlAlchemyAcceptedBillingEvidenceReader",
    "SqlAlchemyExecutionRuntimeRepository",
    "SqlAlchemyExecutionSourceInvoiceReader",
    "SqlAlchemyImportHistory",
    "SqlAlchemyOperatingExpenseMappingRepository",
    "SqlAlchemyQuotationScenarioEvidenceRepository",
    "SqlAlchemyReviewAccountingResolutionRepository",
    "SqlAlchemyReviewBillingEvidenceReader",
    "SqlAlchemyReviewClassificationEvidenceReader",
    "SqlAlchemyReviewExecutionEvidenceReader",
    "SqlAlchemyReviewOneOffVendorRetirementRepository",
    "SqlAlchemyReviewProductIdentityClaimRepository",
    "SqlAlchemyReviewProductRemediationReservationRepository",
    "SqlAlchemyReviewPurchasePurposeResolutionRepository",
    "SqlAlchemyReviewSourceInvoiceEvidenceReader",
    "SqlAlchemyReviewSupplierRemediationEffectRepository",
    "SqlAlchemyReviewSupplierResolutionRepository",
    "SqlAlchemyWriteAuthorizationRepository",
    "SqlAlchemyUnitOfWork",
    "SqlAlchemyReviewRepository",
    "SqlAlchemyVendorBillExecutionEvidenceReader",
]
