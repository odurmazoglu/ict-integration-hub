from __future__ import annotations

from app.application.exceptions import ApplicationError
from app.application.workflow import ManualReviewReasonCode


class SourceTaxError(ApplicationError):
    error_category = "source_tax_error"


class SourceTaxFactsUnavailableError(SourceTaxError):
    """The source document could not be read *right now* (storage or database failure).

    Retryable: nothing was concluded about the invoice, so no blocking reason is ever
    persisted for it -- the operation that needed the facts fails without writing.
    """

    error_category = "source_tax_facts_unavailable"


class SourceTaxSafetyBlockedError(SourceTaxError):
    """The original invoice document forbids a Vendor Bill (withholding, unsupported tax
    content, or a document that cannot be verified)."""

    error_category = "source_tax_safety_blocked"

    def __init__(self, safe_message: str, *, reason_codes: tuple[ManualReviewReasonCode, ...]) -> None:
        super().__init__(safe_message)
        self.reason_codes = reason_codes
