"""Result model for return-invoice reference matching (PR R).

Everything here is informational. A lookup result is never persisted, never changes a
review, a decision or a classification, and never authorizes a Vendor Bill or a credit
note: the PR S return-invoice guards stay authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class ReturnReferenceStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    MISMATCH = "MISMATCH"
    REFERENCE_MISSING = "REFERENCE_MISSING"
    REFERENCE_UNPARSEABLE = "REFERENCE_UNPARSEABLE"
    INCOMPLETE_SEARCH = "INCOMPLETE_SEARCH"


class ReturnReferenceCheck(StrEnum):
    """Validation applied to the single exact-number candidate."""

    #: Exactly one Odoo company carries the return's buyer tax number, and it is the review's company.
    COMPANY_IDENTITY = "COMPANY_IDENTITY"
    #: The original's seller is the review company: the candidate comes from that company's own
    #: Uyumsoft outbox (the outbox list item carries no seller tax number) and COMPANY_IDENTITY holds.
    SELLER_IDENTITY = "SELLER_IDENTITY"
    #: The original's buyer (``TargetTcknVkn``) is the return's seller.
    BUYER_IDENTITY = "BUYER_IDENTITY"
    CURRENCY = "CURRENCY"
    #: The original's issue date equals the reference ``IssueDate`` and is not after the return.
    ORIGINAL_DATE = "ORIGINAL_DATE"
    #: The original's ``InvoiceTipType`` is an allowed original type for the return type code.
    INVOICE_TYPE = "INVOICE_TYPE"
    #: The original's current provider status is final and valid (``Approved``).
    INVOICE_STATUS = "INVOICE_STATUS"


class ReturnReferenceIssue(StrEnum):
    SOURCE_DOCUMENT_MISSING = "SOURCE_DOCUMENT_MISSING"
    SOURCE_DOCUMENT_AMBIGUOUS = "SOURCE_DOCUMENT_AMBIGUOUS"
    SOURCE_DOCUMENT_UNREADABLE = "SOURCE_DOCUMENT_UNREADABLE"
    SOURCE_DOCUMENT_HASH_MISMATCH = "SOURCE_DOCUMENT_HASH_MISMATCH"
    SOURCE_DOCUMENT_IDENTITY_MISMATCH = "SOURCE_DOCUMENT_IDENTITY_MISMATCH"
    XML_INVALID = "XML_INVALID"
    INVOICE_NUMBER_MISSING = "INVOICE_NUMBER_MISSING"
    INVOICE_NUMBER_FORMAT_INVALID = "INVOICE_NUMBER_FORMAT_INVALID"
    ISSUE_DATE_MISSING = "ISSUE_DATE_MISSING"
    ISSUE_DATE_INVALID = "ISSUE_DATE_INVALID"
    ACTIVE_SEARCH_FAILED = "ACTIVE_SEARCH_FAILED"
    ARCHIVED_SEARCH_FAILED = "ARCHIVED_SEARCH_FAILED"
    PROVIDER_RESULT_TRUNCATED = "PROVIDER_RESULT_TRUNCATED"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"


@dataclass(frozen=True, slots=True)
class OutboxInvoiceCandidate:
    """One provider outbox invoice, already mapped with the verified field semantics."""

    invoice_number: str | None
    ettn: str | None
    invoice_type: str | None
    scenario: str | None
    status: str | None
    buyer_tax_number: str | None
    currency: str | None
    issue_date: date | None
    archived: bool | None


@dataclass(frozen=True, slots=True)
class OutboxLookupPage:
    candidates: tuple[OutboxInvoiceCandidate, ...]
    provider_total: int | None


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: ReturnReferenceCheck
    passed: bool


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    candidate: OutboxInvoiceCandidate
    checks: tuple[CheckResult, ...]

    @property
    def failed_checks(self) -> tuple[ReturnReferenceCheck, ...]:
        return tuple(result.check for result in self.checks if not result.passed)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    archived: bool
    completed: bool
    provider_total: int | None = None
    returned: int = 0
    exact: int = 0


@dataclass(frozen=True, slots=True)
class ReferenceResult:
    position: int
    invoice_number: str | None
    issue_date: date | None
    document_type_code: str | None
    status: ReturnReferenceStatus
    issues: tuple[ReturnReferenceIssue, ...] = ()
    failed_checks: tuple[ReturnReferenceCheck, ...] = ()
    searches: tuple[SearchOutcome, ...] = ()
    candidates: tuple[CandidateEvaluation, ...] = ()


@dataclass(frozen=True, slots=True)
class ReturnReferenceLookupResult:
    review_id: str
    company_id: int
    review_version: int
    return_invoice_type_code: str
    status: ReturnReferenceStatus
    issues: tuple[ReturnReferenceIssue, ...] = ()
    references: tuple[ReferenceResult, ...] = ()
    provider_calls: int = 0
    company_identity_verified: bool | None = None
