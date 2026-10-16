from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto.base import ApplicationDTO


class WorkflowType(StrEnum):
    """Canonical procurement workflow identifiers used across ICT IPP."""

    VENDOR_BILL = "vendor_bill"
    RFQ = "rfq"
    EXPENSE = "expense"
    ASSET = "asset"
    SUBSCRIPTION = "subscription"
    CUSTOMER_QUOTATION = "customer_quotation"
    MANUAL_REVIEW = "manual_review"


class ManualReviewReasonCode(StrEnum):
    """Canonical reason codes for deterministic Manual Review decisions."""

    SUPPLIER_TAX_NUMBER_MISSING = "SUPPLIER_TAX_NUMBER_MISSING"
    SUPPLIER_NOT_FOUND = "SUPPLIER_NOT_FOUND"
    SUPPLIER_AMBIGUOUS = "SUPPLIER_AMBIGUOUS"
    PRODUCT_IDENTIFIER_MISSING = "PRODUCT_IDENTIFIER_MISSING"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    PRODUCT_AMBIGUOUS = "PRODUCT_AMBIGUOUS"
    PRODUCT_MAPPING_INCOMPLETE = "PRODUCT_MAPPING_INCOMPLETE"
    TAX_NOT_FOUND = "TAX_NOT_FOUND"
    TAX_AMBIGUOUS = "TAX_AMBIGUOUS"
    TAX_MAPPING_INCOMPLETE = "TAX_MAPPING_INCOMPLETE"
    OPERATING_EXPENSE_MAPPING_REQUIRED = "OPERATING_EXPENSE_MAPPING_REQUIRED"
    OPERATING_EXPENSE_MAPPING_AMBIGUOUS = "OPERATING_EXPENSE_MAPPING_AMBIGUOUS"
    UNSUPPORTED_INVOICE_CONTENT = "UNSUPPORTED_INVOICE_CONTENT"
    #: Added in PR S0 (rollback compatibility); emitted by PR S. RETURN_INVOICE_NOT_SUPPORTED
    #: is never stripped; AMBIGUOUS_COMMERCIAL_PURCHASE yields only to an explicit
    #: review-scoped accounting resolution (see use_cases.effective_decision).
    RETURN_INVOICE_NOT_SUPPORTED = "RETURN_INVOICE_NOT_SUPPORTED"
    AMBIGUOUS_COMMERCIAL_PURCHASE = "AMBIGUOUS_COMMERCIAL_PURCHASE"
    #: Added in PR T W0 (rollback compatibility); emitted by the PR T W1 source-tax guard
    #: (``app.application.source_tax``). Both are hard blocks: never stripped, and a review
    #: carrying one never gets Stage-1 execution evidence. W0 is the rollback floor.
    WITHHOLDING_TAX_NOT_SUPPORTED = "WITHHOLDING_TAX_NOT_SUPPORTED"
    SOURCE_DOCUMENT_UNVERIFIED = "SOURCE_DOCUMENT_UNVERIFIED"


#: PR T W0: reasons that make a review non-executable in every mode, whatever its match facts.
SOURCE_TAX_SAFETY_REASON_CODES = frozenset(
    {
        ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED,
        ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED,
    }
)
#: PR T W1: ``ManualReviewReason.source`` of every reason derived from the original UBL
#: document's tax facts. Such a reason is never stripped and never gets Stage-1 evidence,
#: including an UNSUPPORTED_INVOICE_CONTENT reason for a non-0015 ``TaxTypeCode``.
SOURCE_TAX_REASON_SOURCE = "source_tax"


@dataclass(frozen=True, slots=True)
class ManualReviewReason(ApplicationDTO):
    """Safe structured reason explaining why an invoice needs review."""

    code: ManualReviewReasonCode
    message: str
    line_number: str | None = None
    tax_index: int | None = None
    candidate_count: int | None = None
    source: str | None = None
    details: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ManualReviewDecision(ApplicationDTO):
    """Structured Manual Review decision for future Workbench display."""

    reasons: tuple[ManualReviewReason, ...]
    summary: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class WorkflowDecision(ApplicationDTO):
    """Deterministic workflow selection output consumed by orchestration."""

    workflow: WorkflowType
    matched_rule: str | None = None
    explanation: str | None = None
    manual_review: ManualReviewDecision | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
