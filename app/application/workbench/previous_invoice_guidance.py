"""V1 previous-invoice guidance for recurring suppliers (presentation only).

Shows the operator what was recorded for the same supplier's most recent *completed*
invoice -- a Direct Vendor Bill decision whose draft bill was created, together with the
purchase purpose and accounting resolution recorded for that review up to its decision.

This is historical information, never a recommendation:

* nothing here selects, pre-fills or applies a purpose, account, category or workflow;
* no supplier-level accounting mapping is read or written (``operating_expense_mappings``
  is untouched), and the history itself is never cached -- it is recomputed from committed
  Hub rows on every projection sync;
* a supplier with differing earlier classifications is flagged as mixed-purpose, and every
  previous classification is shown with the same generic, supplier-independent warning
  that it is reference information only.

The supplier is identified by the review's exact source VKN, never by name. No supplier
is special-cased.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.workbench.accounting_resolution import ReviewAccountingResolution
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.purchase_purpose import PurchasePurpose

#: How many completed previous invoices are inspected (most recent first).
PREVIOUS_INVOICE_SCAN_LIMIT = 10


@dataclass(frozen=True, slots=True)
class PreviousInvoiceDecision(ApplicationDTO):
    """One completed previous invoice of the supplier and what was recorded for it."""

    review_id: str
    invoice_number: str | None
    invoice_date: date | None
    supplier_name: str | None
    decided_at: datetime
    purchase_purpose: PurchasePurpose
    accounting_resolution: ReviewAccountingResolution

    def __post_init__(self) -> None:
        if not isinstance(self.review_id, str) or not self.review_id.strip():
            raise WorkbenchContractError("review_id is required.")
        if not isinstance(self.decided_at, datetime) or self.decided_at.utcoffset() is None:
            raise WorkbenchContractError("decided_at must be a timezone-aware datetime.")
        if not isinstance(self.purchase_purpose, PurchasePurpose):
            raise WorkbenchContractError("A canonical PurchasePurpose is required.")
        if not isinstance(self.accounting_resolution, ReviewAccountingResolution):
            raise WorkbenchContractError("A ReviewAccountingResolution is required.")
        if self.accounting_resolution.review_id != self.review_id:
            raise WorkbenchContractError("The accounting resolution belongs to another review.")


class PreviousInvoiceDecisionReader(Protocol):
    """Read-only port over committed Hub rows (no Odoo call)."""

    def list_completed_supplier_decisions(
        self,
        *,
        company_id: int,
        supplier_tax_number: str,
        exclude_review_id: str,
        limit: int = PREVIOUS_INVOICE_SCAN_LIMIT,
    ) -> tuple[PreviousInvoiceDecision, ...]: ...


@dataclass(frozen=True, slots=True)
class PreviousInvoiceGuidance(ApplicationDTO):
    """What the operator is shown: the most recent completed invoice plus safety flags."""

    latest: PreviousInvoiceDecision
    completed_count: int
    #: Distinct (purpose, treatment, account, category, asset account, model) among them.
    distinct_classification_count: int
    #: Odoo display names for ``latest``'s account / depreciation model (``None`` = unavailable).
    #: Filled by the composition only when the block is actually shown.
    account_label: str | None = None
    depreciation_model_label: str | None = None

    @property
    def mixed_purpose(self) -> bool:
        return self.distinct_classification_count > 1


def build_previous_invoice_guidance(decisions: Sequence[PreviousInvoiceDecision]) -> PreviousInvoiceGuidance | None:
    """``None`` when the supplier has no completed previous invoice with both decisions.

    Hub rows only: the Odoo account labels are left empty here and resolved by the
    composition only when the block is actually shown.
    """

    if not decisions:
        return None
    ordered = sorted(decisions, key=_recency, reverse=True)
    return PreviousInvoiceGuidance(
        latest=ordered[0],
        completed_count=len({decision.review_id for decision in ordered}),
        distinct_classification_count=len({_classification(decision) for decision in ordered}),
    )


def _recency(decision: PreviousInvoiceDecision) -> tuple[date, datetime, str]:
    return (decision.invoice_date or date.min, decision.decided_at, decision.review_id)


def _classification(decision: PreviousInvoiceDecision) -> tuple[object, ...]:
    resolution = decision.accounting_resolution
    return (
        decision.purchase_purpose,
        resolution.treatment_type,
        resolution.expense_account_id,
        resolution.expense_category,
        resolution.asset_account_id,
        resolution.depreciation_model_id,
    )


__all__ = [
    "PREVIOUS_INVOICE_SCAN_LIMIT",
    "PreviousInvoiceDecision",
    "PreviousInvoiceDecisionReader",
    "PreviousInvoiceGuidance",
    "build_previous_invoice_guidance",
]
