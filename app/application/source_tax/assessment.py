"""Pure source-document verification and the review reasons it implies (PR T W1)."""

from __future__ import annotations

import hashlib

from app.application.source_tax.model import SourceDocumentIssue, SourceTaxAssessment
from app.application.workflow import SOURCE_TAX_REASON_SOURCE, ManualReviewReason, ManualReviewReasonCode
from app.domain.invoice.exceptions import InvoiceDomainError
from app.domain.invoice.tax_facts import SourceTaxFacts, extract_source_tax_facts


def assess_source_document(
    content: bytes,
    *,
    expected_sha256: str | None,
    expected_invoice_uuid: str | None,
) -> SourceTaxAssessment:
    """Trust the document's tax facts only once its bytes and identity are proven.

    The SHA-256 must equal the hash recorded when the document was stored, and the
    document's ``cbc:UUID`` must equal the review's source-invoice UUID exactly.
    """

    if not expected_sha256 or hashlib.sha256(content).hexdigest() != expected_sha256.strip().lower():
        return SourceTaxAssessment.unverified(SourceDocumentIssue.HASH_MISMATCH)
    try:
        facts = extract_source_tax_facts(content)
    except InvoiceDomainError:
        return SourceTaxAssessment.unverified(SourceDocumentIssue.DOCUMENT_INVALID)
    expected = (expected_invoice_uuid or "").strip()
    if facts.invoice_uuid is None or not expected:
        return SourceTaxAssessment.unverified(SourceDocumentIssue.UUID_MISSING)
    if facts.invoice_uuid != expected:
        return SourceTaxAssessment.unverified(SourceDocumentIssue.UUID_MISMATCH)
    return SourceTaxAssessment.verified(facts)


def source_tax_review_reasons(assessment: SourceTaxAssessment) -> tuple[ManualReviewReason, ...]:
    """The Manual Review reasons ``assessment`` forces; empty only for a verified,
    withholding-free, VAT-only (0015) document."""

    if assessment.facts is None:
        return (_unverified_reason(assessment.issue or SourceDocumentIssue.FACTS_NOT_SUPPLIED),)
    facts = assessment.facts
    reasons: list[ManualReviewReason] = []
    if facts.has_withholding:
        reasons.append(_withholding_reason(facts))
    if facts.has_unsupported_tax_content:
        reasons.append(_unsupported_tax_reason(facts))
    return tuple(reasons)


def _unverified_reason(issue: SourceDocumentIssue) -> ManualReviewReason:
    return ManualReviewReason(
        code=ManualReviewReasonCode.SOURCE_DOCUMENT_UNVERIFIED,
        message="The original invoice document could not be verified; its tax content cannot be checked.",
        source=SOURCE_TAX_REASON_SOURCE,
        details=(("issue", issue.value),),
    )


def _withholding_reason(facts: SourceTaxFacts) -> ManualReviewReason:
    details = (
        ("invoice_type_code", facts.invoice_type_code or ""),
        ("invoice_withholding_tax_totals", str(facts.invoice_withholding_total_count)),
        ("line_withholding_tax_totals", str(facts.line_withholding_total_count)),
    )
    return ManualReviewReason(
        code=ManualReviewReasonCode.WITHHOLDING_TAX_NOT_SUPPORTED,
        message="The invoice declares tax withholding (tevkifat); a Vendor Bill is never created for it.",
        source=SOURCE_TAX_REASON_SOURCE,
        details=tuple((key, value) for key, value in details if value),
    )


def _unsupported_tax_reason(facts: SourceTaxFacts) -> ManualReviewReason:
    details = (
        ("unsupported_tax_type_codes", ",".join(facts.unsupported_tax_type_codes)),
        ("tax_subtotals_without_type_code", str(facts.tax_subtotals_without_type_code or "")),
    )
    return ManualReviewReason(
        code=ManualReviewReasonCode.UNSUPPORTED_INVOICE_CONTENT,
        message="The invoice carries a tax other than VAT (TaxTypeCode 0015); it is not supported.",
        source=SOURCE_TAX_REASON_SOURCE,
        details=tuple((key, value) for key, value in details if value),
    )
