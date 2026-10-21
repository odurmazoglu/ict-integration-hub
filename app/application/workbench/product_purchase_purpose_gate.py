"""Purchase-purpose requirement at Vendor Bill decision acceptance for product-coded invoices.

A product-coded invoice (at least one line carries a deterministic product identifier)
can be booked two very different ways: to the product category's purchase account
(RESALE) or to an explicitly selected internal expense account (INTERNAL_USE). The
decision path must never pick one silently, so a fresh product-coded Vendor Bill
decision is refused unless the operator recorded the purchase purpose for the exact
review version being decided.

Scope is deliberately narrow:

* only the *current-version* purpose counts; a purpose recorded for any other version
  never carries over;
* purpose is never inferred from the product, its category or the supplier;
* only RESALE and INTERNAL_USE are accepted, because only they have product-invoice
  accounting safeguards (``ResaleDecisionGate`` / ``InternalUseDecisionGate``); every
  other purpose fails closed instead of falling back to the product category's account;
* identifier-free (operating-expense / fixed-asset shaped) invoices are untouched.

The gate only reads Hub persistence. The safeguard pin check that follows it lives in
``SubmitReviewDecisionUseCase``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.exceptions import ProductPurchasePurposeDecisionError, WorkbenchContractError
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import PurchasePurposeHistoryReader

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice

#: Purposes with product-invoice accounting safeguards at decision acceptance.
PRODUCT_INVOICE_PURPOSES = frozenset({PurchasePurpose.RESALE, PurchasePurpose.INTERNAL_USE})


class ProductPurchasePurposeGate:
    """Fail-closed: a product-coded Vendor Bill decision needs an explicit, supported purpose."""

    def __init__(self, *, purpose_reader: PurchasePurposeHistoryReader) -> None:
        self._purpose_reader = purpose_reader

    def require(self, command: ReviewDecisionCommand, evidence: ExecutionSourceInvoice) -> PurchasePurpose | None:
        """``None`` for an identifier-free invoice; otherwise the current-version purpose
        (RESALE or INTERNAL_USE), or ``ProductPurchasePurposeDecisionError``."""

        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        if not invoice_has_product_identifier(evidence.invoice):
            return None
        resolutions = self._purpose_reader.list_purchase_purpose_resolutions(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        current = [resolution for resolution in resolutions if resolution.review_version == command.expected_version]
        if len(current) > 1:
            raise ProductPurchasePurposeDecisionError("More than one purchase purpose exists for this review version.")
        if not current:
            raise ProductPurchasePurposeDecisionError(
                "Product-coded Vendor Bill decisions require an explicitly recorded purchase purpose (RESALE or "
                "INTERNAL_USE) for this review version; record the purchase purpose first. Purpose is never "
                "inferred from the product or the supplier."
            )
        purpose = current[0].purchase_purpose
        if purpose not in PRODUCT_INVOICE_PURPOSES:
            raise ProductPurchasePurposeDecisionError(
                f"Purchase purpose {purpose.name} is not supported for product-coded Vendor Bill decisions; "
                "only RESALE or INTERNAL_USE have product-invoice accounting safeguards."
            )
        return purpose


__all__ = ["PRODUCT_INVOICE_PURPOSES", "ProductPurchasePurposeGate"]
