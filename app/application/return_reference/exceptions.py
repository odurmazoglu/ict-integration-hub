from __future__ import annotations

from app.application.exceptions import ApplicationError


class ReturnReferenceError(ApplicationError):
    error_category = "return_reference_error"


class ReturnReferenceNotApplicableError(ReturnReferenceError):
    """The review's source invoice is not a return invoice; there is no original to look up."""

    error_category = "return_reference_not_applicable"


class ReturnReferenceLookupUnavailableError(ReturnReferenceError):
    """A validation dependency (company identity) could not be read; nothing was concluded."""

    error_category = "return_reference_lookup_unavailable"


class OutboxLookupUnavailableError(ReturnReferenceError):
    """One provider outbox search did not complete (raised by the outbox lookup port)."""

    error_category = "outbox_lookup_unavailable"


class ReturnReferenceSourceDocumentError(ReturnReferenceError):
    """The stored source document could not be read (raised by the document port)."""

    error_category = "return_reference_source_document_error"
