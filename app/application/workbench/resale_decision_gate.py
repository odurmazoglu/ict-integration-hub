"""RESALE eligibility gate at Vendor Bill decision acceptance (P0-PROD-18E-1B).

Purchase purpose says *why* an invoice was bought; it never carries a product. The
product identity only exists here, at decision acceptance, after the operator's
explicit ``LineResolution.selected_product_id`` overrides have been applied to the
pinned product-match evidence. So this is where a RESALE review's products are
checked against the pure P0-PROD-18E-1A policy, using P0-PROD-18D's read-only
purchase-account discovery for each distinct product.

Scope is deliberately narrow:

* only a review whose *current-version* purchase purpose is RESALE is gated; a RESALE
  purpose recorded for any other review version never carries over and fails closed;
* every invoice line must be product-backed by a concrete existing Odoo product --
  account-only lines, identifier-free (operating-expense-shaped) invoices, and
  unresolved/ambiguous product matches are all rejected, never substituted;
* the check covers only the *pre-fiscal-position* account. Nothing is pinned,
  persisted, or sent to Odoo, fiscal positions are not evaluated, and the final
  Vendor Bill line account is not claimed to be known (P0-PROD-18F).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.exceptions import (
    PurchaseAccountProductNotFoundError,
    ResaleDecisionEligibilityError,
    WorkbenchContractError,
)
from app.application.workbench.purchase_account_discovery import (
    GetProductPurchaseAccountQuery,
    ProductPurchaseAccountResolution,
)
from app.application.workbench.purchase_purpose import PurchasePurpose, PurchasePurposeResolution
from app.application.workbench.resale_product_eligibility import (
    ResaleProductEligibility,
    evaluate_resale_product_eligibility,
    normalize_resale_category_ids,
)
from app.matching import ProductMatchStatus

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice


class PurchasePurposeHistoryReader(Protocol):
    """Read-only port: every purchase-purpose resolution recorded for one review."""

    def list_purchase_purpose_resolutions(
        self,
        *,
        review_id: str,
        company_id: int,
    ) -> tuple[PurchasePurposeResolution, ...]:
        pass


class ProductPurchaseAccountResolver(Protocol):
    """Read-only port: P0-PROD-18D's per-product purchase-account discovery."""

    def execute(self, query: GetProductPurchaseAccountQuery) -> ProductPurchaseAccountResolution:
        pass


class ResaleDecisionGate:
    """Fail-closed RESALE product-eligibility check for one fresh Vendor Bill decision."""

    def __init__(
        self,
        *,
        purpose_reader: PurchasePurposeHistoryReader,
        product_account_resolver: ProductPurchaseAccountResolver,
        approved_category_ids: Iterable[int],
    ) -> None:
        self._purpose_reader = purpose_reader
        self._product_account_resolver = product_account_resolver
        self._approved_category_ids = normalize_resale_category_ids(approved_category_ids)

    def enforce(self, command: ReviewDecisionCommand, evidence: ExecutionSourceInvoice) -> None:
        """Raise ``ResaleDecisionEligibilityError`` unless the decision may proceed.

        A no-op for a review with no RESALE purpose at any version, or whose
        current-version purpose is not RESALE.
        """

        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        if not self._current_purpose_is_resale(command):
            return
        _require_product_shaped_decision(command, evidence)
        product_by_line = _resolved_product_by_line(evidence)
        eligibility_by_product = {
            product_id: self._evaluate(command.company_id, product_id)
            for product_id in dict.fromkeys(product_by_line.values())
        }
        failures = [
            _line_failure(line_number, product_id, eligibility_by_product[product_id])
            for line_number, product_id in product_by_line.items()
            if not eligibility_by_product[product_id].eligible
        ]
        if failures:
            raise ResaleDecisionEligibilityError(
                "RESALE decision rejected: product not eligible for RESALE -- " + "; ".join(failures) + "."
            )

    def _current_purpose_is_resale(self, command: ReviewDecisionCommand) -> bool:
        resolutions = self._purpose_reader.list_purchase_purpose_resolutions(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        current = [resolution for resolution in resolutions if resolution.review_version == command.expected_version]
        if len(current) > 1:
            raise ResaleDecisionEligibilityError("More than one purchase purpose exists for this review version.")
        if current:
            return current[0].purchase_purpose is PurchasePurpose.RESALE
        if any(resolution.purchase_purpose is PurchasePurpose.RESALE for resolution in resolutions):
            raise ResaleDecisionEligibilityError(
                "A RESALE purchase purpose exists only for another review version; record the purchase purpose "
                "again for the current review version."
            )
        return False

    def _evaluate(self, company_id: int, product_id: int) -> ResaleProductEligibility:
        try:
            resolution: ProductPurchaseAccountResolution | None = self._product_account_resolver.execute(
                GetProductPurchaseAccountQuery(company_id=company_id, product_id=product_id)
            )
        except PurchaseAccountProductNotFoundError:
            resolution = None
        return evaluate_resale_product_eligibility(
            resolution,
            company_id=company_id,
            approved_category_ids=self._approved_category_ids,
        )


def _require_product_shaped_decision(command: ReviewDecisionCommand, evidence: ExecutionSourceInvoice) -> None:
    if any(resolution.account_only for resolution in command.line_resolutions):
        raise ResaleDecisionEligibilityError("RESALE decisions do not allow account-only line resolutions.")
    if not invoice_has_product_identifier(evidence.invoice):
        raise ResaleDecisionEligibilityError(
            "RESALE requires a product-shaped invoice; operating-expense treatment cannot substitute for products."
        )


def _resolved_product_by_line(evidence: ExecutionSourceInvoice) -> dict[str | None, int]:
    results_by_line: dict[str | None, list] = {}
    for line_result in evidence.product_match.line_results:
        results_by_line.setdefault(line_result.line_number, []).append(line_result.result)

    product_by_line: dict[str | None, int] = {}
    unresolved: list[str] = []
    for line in evidence.invoice.lines:
        results = results_by_line.get(line.line_number, [])
        if (
            len(results) == 1
            and results[0].status is ProductMatchStatus.MATCHED
            and type(results[0].product_id) is int
            and results[0].product_id > 0
        ):
            product_by_line[line.line_number] = results[0].product_id
        else:
            unresolved.append(str(line.line_number))
    if unresolved:
        raise ResaleDecisionEligibilityError(
            "RESALE requires every invoice line to resolve to exactly one existing Odoo product; unresolved lines: "
            + ", ".join(unresolved)
            + "."
        )
    return product_by_line


def _line_failure(line_number: str | None, product_id: int, eligibility: ResaleProductEligibility) -> str:
    blockers = ",".join(blocker.value for blocker in eligibility.blockers)
    return f"line {line_number} (product {product_id}): {blockers}"


__all__ = [
    "ProductPurchaseAccountResolver",
    "PurchasePurposeHistoryReader",
    "ResaleDecisionGate",
]
