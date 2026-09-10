"""Shared builders for version-pinned Workbench review evidence.

One implementation of the deterministic "what evidence does this classification
produce for a review version?" logic, reused by both first-time import
(:class:`~app.application.use_cases.import_invoice.ImportInvoiceUseCase`) and
non-destructive reclassification
(:class:`~app.application.use_cases.reclassify_review.ReclassifyWorkbenchReviewUseCase`).

These functions manufacture no match results -- they only read the deterministic
``DecisionResult`` produced by the normal ``DecisionEngine`` and the source
``InternalInvoice``.
"""

from __future__ import annotations

from app.application.dto import DecisionResult
from app.application.workbench.evidence import ReviewClassificationEvidence, ReviewExecutionEvidence
from app.application.workflow import WorkflowType
from app.billing.builder import validate_vendor_bill_inputs
from app.domain.invoice import InternalInvoice


def build_review_classification_evidence(
    *,
    review_id: str,
    company_id: int,
    review_version: int,
    decision_result: DecisionResult,
) -> ReviewClassificationEvidence | None:
    """Immutable deterministic classification evidence pinned to ``review_version``."""

    if decision_result.classification_result is None:
        return None
    return ReviewClassificationEvidence.from_result(
        review_id=review_id,
        company_id=company_id,
        review_version=review_version,
        result=decision_result.classification_result,
    )


def build_review_execution_evidence(
    *,
    review_id: str,
    company_id: int,
    review_version: int,
    invoice: InternalInvoice,
    decision_result: DecisionResult,
) -> ReviewExecutionEvidence | None:
    """Immutable pre-decision Stage-1 evidence for a fully matched Vendor Bill candidate.

    Only produced when the deterministic execution inputs are canonically complete
    (``validate_vendor_bill_inputs``). Incomplete or ambiguous matches keep the
    existing fail-closed behavior: no executable Vendor Bill evidence is pinned.
    """

    if decision_result.workflow is not WorkflowType.VENDOR_BILL:
        return None
    partner_match = decision_result.partner_match
    product_match = decision_result.product_match
    tax_match = decision_result.tax_match
    if partner_match is None or product_match is None or tax_match is None:
        return None

    def _build(operating_expense_match: object | None) -> ReviewExecutionEvidence:
        return ReviewExecutionEvidence(
            review_id=review_id,
            company_id=company_id,
            review_version=review_version,
            source_invoice_id=invoice.header.ettn or invoice.header.invoice_uuid,
            invoice=invoice,
            partner_match=partner_match,
            product_match=product_match,
            tax_match=tax_match,
            operating_expense_match=operating_expense_match,
        )

    # Product mode wins: a valid deterministic product Vendor Bill is pinned as product evidence.
    if validate_vendor_bill_inputs(
        invoice,
        partner_match,
        product_match,
        tax_match,
        company_id=company_id,
    ).is_valid:
        return _build(None)

    # Operating-expense mode: pin the exact deterministic expense-account match when the
    # account-only Vendor Bill validation accepts it. Never re-resolve later.
    operating_expense_match = decision_result.operating_expense_match
    if operating_expense_match is None:
        return None
    if not validate_vendor_bill_inputs(
        invoice,
        partner_match,
        product_match,
        tax_match,
        company_id=company_id,
        operating_expense_match=operating_expense_match,
    ).is_valid:
        return None
    return _build(operating_expense_match)
