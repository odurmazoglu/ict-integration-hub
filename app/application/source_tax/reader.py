"""Read side of the PR T W1 source-tax guard.

The review's original UBL document is located exactly as PR R does it (the review's
import idempotency key -> provider metadata -> ``invoice_documents``), read from
document storage, and verified (:func:`assess_source_document`) before any fact is
trusted. Read-only: nothing here persists anything.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.application.source_tax.assessment import assess_source_document, source_tax_review_reasons
from app.application.source_tax.exceptions import (
    SourceDocumentUnreadableError,
    SourceTaxFactsUnavailableError,
    SourceTaxSafetyBlockedError,
)
from app.application.source_tax.exemption import DISABLED_VAT_EXEMPTION_POLICY, VatExemptionTaxPolicy
from app.application.source_tax.model import SourceDocumentIssue, SourceTaxAssessment
from app.application.workbench.exceptions import ReviewNotFoundError, ReviewPersistenceError
from app.domain.invoice import InternalInvoice
from app.domain.invoice.tax_facts import SourceTaxFacts
from app.tax_mapping.result import InvoiceTaxMappingResult

logger = logging.getLogger(__name__)


class SourceDocument(Protocol):
    storage_key: str
    content_sha256: str


class SourceDocumentLocator(Protocol):
    """PR R's review -> stored UBL document locator (import idempotency key based)."""

    def find_source_documents(self, *, review_id: str, company_id: int) -> tuple[SourceDocument, ...]:
        pass


class SourceDocumentContentReader(Protocol):
    """Raises :class:`SourceDocumentUnreadableError` when the document can deterministically
    never be read, and :class:`SourceTaxFactsUnavailableError` when storage is unavailable."""

    def read(self, storage_key: str) -> bytes:
        pass


class SourceTaxAssessmentReader(Protocol):
    def assess(self, *, review_id: str, company_id: int, invoice_uuid: str | None) -> SourceTaxAssessment:
        pass


class StoredSourceTaxFactsReader:
    """The verified tax facts of one review's original UBL document."""

    def __init__(
        self, *, document_locator: SourceDocumentLocator, document_reader: SourceDocumentContentReader
    ) -> None:
        self._document_locator = document_locator
        self._document_reader = document_reader

    def assess(self, *, review_id: str, company_id: int, invoice_uuid: str | None) -> SourceTaxAssessment:
        assessment = self._assess(review_id=review_id, company_id=company_id, invoice_uuid=invoice_uuid)
        logger.info(
            "source_tax_assessment_completed",
            extra={
                "review_id": review_id,
                "company_id": company_id,
                "verified": assessment.is_verified,
                "issue": assessment.issue.value if assessment.issue is not None else None,
                "reason_codes": [reason.code.value for reason in source_tax_review_reasons(assessment)],
            },
        )
        return assessment

    def _assess(self, *, review_id: str, company_id: int, invoice_uuid: str | None) -> SourceTaxAssessment:
        try:
            documents = self._document_locator.find_source_documents(review_id=review_id, company_id=company_id)
        except ReviewNotFoundError:
            raise
        except ReviewPersistenceError as exc:
            raise SourceTaxFactsUnavailableError("Source invoice document could not be located right now.") from exc
        if not documents:
            return SourceTaxAssessment.unverified(SourceDocumentIssue.DOCUMENT_MISSING)
        if len(documents) > 1:
            return SourceTaxAssessment.unverified(SourceDocumentIssue.DOCUMENT_AMBIGUOUS)
        document = documents[0]
        try:
            content = self._document_reader.read(document.storage_key)
        except SourceDocumentUnreadableError as exc:
            return SourceTaxAssessment.unverified(exc.issue)
        except SourceTaxFactsUnavailableError:
            logger.warning(
                "source_tax_document_unreadable",
                extra={"review_id": review_id, "company_id": company_id},
            )
            raise
        return assess_source_document(
            content,
            expected_sha256=document.content_sha256,
            expected_invoice_uuid=invoice_uuid,
        )


class SourceTaxSafetyCheck:
    """Fail-closed Vendor Bill backstop: decision acceptance, preview and every execution
    attempt (retry and resume included) re-verify the original document right before use.

    Independent of the review's persisted reasons, so no operator resolution, review-scoped
    accounting decision or stale Stage-1 evidence can route around it.
    """

    def __init__(
        self,
        *,
        facts_reader: SourceTaxAssessmentReader,
        exemption_policy: VatExemptionTaxPolicy = DISABLED_VAT_EXEMPTION_POLICY,
    ) -> None:
        self._facts_reader = facts_reader
        # PR T T2-A: the default (empty configuration) blocks every exemption-pinned line.
        self._exemption_policy = exemption_policy

    def ensure_vendor_bill_tax_allowed(
        self,
        *,
        review_id: str,
        company_id: int,
        invoice: InternalInvoice,
        tax_match: InvoiceTaxMappingResult,
    ) -> SourceTaxFacts:
        """:meth:`ensure_vendor_bill_allowed`, then (PR T T2-A) every exemption-pinned line
        re-proven from those same freshly verified facts and a fresh Odoo tax read."""

        facts = self.ensure_vendor_bill_allowed(review_id=review_id, company_id=company_id, invoice=invoice)
        self._exemption_policy.ensure_pinned_exemptions_allowed(
            review_id=review_id,
            company_id=company_id,
            invoice=invoice,
            tax_match=tax_match,
            facts=facts,
        )
        return facts

    def ensure_vendor_bill_allowed(
        self, *, review_id: str, company_id: int, invoice: InternalInvoice
    ) -> SourceTaxFacts:
        """The verified facts, or :class:`SourceTaxSafetyBlockedError` /
        :class:`SourceTaxFactsUnavailableError` (retryable)."""

        invoice_uuid = invoice.header.invoice_uuid if isinstance(invoice, InternalInvoice) else None
        assessment = self._facts_reader.assess(review_id=review_id, company_id=company_id, invoice_uuid=invoice_uuid)
        reasons = source_tax_review_reasons(assessment)
        if reasons or assessment.facts is None:
            codes = tuple(reason.code for reason in reasons)
            logger.warning(
                "source_tax_vendor_bill_blocked",
                extra={
                    "review_id": review_id,
                    "company_id": company_id,
                    "reason_codes": [code.value for code in codes],
                },
            )
            raise SourceTaxSafetyBlockedError(
                "The original invoice document does not allow a Vendor Bill: " + ", ".join(codes) + ".",
                reason_codes=codes,
            )
        return assessment.facts
