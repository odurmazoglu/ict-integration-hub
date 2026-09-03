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
    UNSUPPORTED_INVOICE_CONTENT = "UNSUPPORTED_INVOICE_CONTENT"


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
