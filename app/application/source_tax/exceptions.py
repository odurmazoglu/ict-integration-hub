from __future__ import annotations

from app.application.exceptions import ApplicationError
from app.application.source_tax.model import SourceDocumentIssue
from app.application.workflow import ManualReviewReasonCode


class SourceTaxError(ApplicationError):
    error_category = "source_tax_error"


class SourceTaxFactsUnavailableError(SourceTaxError):
    """The source document could not be read *right now* (storage or database failure).

    Retryable: nothing was concluded about the invoice, so no blocking reason is ever
    persisted for it -- the operation that needed the facts fails without writing.
    """

    error_category = "source_tax_facts_unavailable"


class SourceDocumentUnreadableError(SourceTaxError):
    """The stored document can deterministically never be read (raised by the content port).

    Not retryable: the review is classified as an unverified source document with ``issue``.
    """

    error_category = "source_document_unreadable"

    def __init__(self, safe_message: str, *, issue: SourceDocumentIssue) -> None:
        super().__init__(safe_message)
        self.issue = issue


class SourceTaxSafetyBlockedError(SourceTaxError):
    """The original invoice document forbids a Vendor Bill (withholding, unsupported tax
    content, or a document that cannot be verified)."""

    error_category = "source_tax_safety_blocked"

    def __init__(self, safe_message: str, *, reason_codes: tuple[ManualReviewReasonCode, ...]) -> None:
        super().__init__(safe_message)
        self.reason_codes = reason_codes
