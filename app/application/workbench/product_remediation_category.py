"""CREATE_NEW_PRODUCT category support, incl. RESALE product creation (P0-PROD-18E-2).

Lets the existing CREATE_NEW_PRODUCT remediation create a product in an explicitly
approved Odoo ``product.category``. This module only *decides*; the orchestration in
``product_remediation_use_cases`` persists and writes.

Rules:

* ``categ_id`` is an exact Odoo id supplied by the operator -- never a name/code, and
  never inferred from the product name, supplier, SKU, or category hierarchy.
* Only a review whose *current-version* purchase purpose is RESALE gets RESALE rules.
  An explicit current-version purpose always wins. A RESALE purpose recorded only for
  another review version fails closed (P0-PROD-18E-2B): it is never carried forward,
  and never degrades into ordinary non-RESALE remediation either -- the purpose must
  be recorded again for the current version. A review with no purpose at any version,
  or only historical non-RESALE purposes, keeps the ordinary non-RESALE behaviour.
* RESALE requires ``categ_id``, an exact member of ``RESALE_PRODUCT_CATEGORY_IDS``
  (an empty allowlist rejects; approving a parent never approves a child), a
  non-storable product, and a category whose configured purchase account is valid in
  Odoo. No account id/code/name/type is required or preferred -- Odoo's category
  configuration stays the accounting source of truth.
* Validation is read-only and happens before any Odoo write, reusing P0-PROD-18D's
  purchase-account discovery. After creation, the product is re-read through that same
  discovery and, under RESALE, evaluated by the P0-PROD-18E-1A eligibility policy.
  Nothing here pins an account, evaluates fiscal positions, or changes Odoo categories.

This capability is not a way to auto-create structured product variants; the
operator explicitly asks for one simple product per review line, exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from app.application.commands.product_remediation import ValidatedProductCategory
from app.application.workbench.exceptions import (
    ProductRemediationCategoryError,
    ProductRemediationStalePurchasePurposeError,
    ProductRemediationVerificationError,
    PurchaseAccountProductNotFoundError,
)
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountConfiguration,
    GetProductPurchaseAccountQuery,
    ListCategoryPurchaseAccountsQuery,
    PurchaseAccountStatus,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import (
    ProductPurchaseAccountResolver,
    PurchasePurposeHistoryReader,
)
from app.application.workbench.resale_product_eligibility import (
    evaluate_resale_product_eligibility,
    normalize_resale_category_ids,
)


class CategoryPurchaseAccountLister(Protocol):
    """Read-only port: P0-PROD-18D's company-scoped category purchase-account listing."""

    def execute(self, query: ListCategoryPurchaseAccountsQuery) -> tuple[CategoryPurchaseAccountConfiguration, ...]:
        pass


class ProductRemediationCategoryPolicy:
    """Fail-closed category decisions for one CREATE_NEW_PRODUCT review line."""

    def __init__(
        self,
        *,
        purpose_reader: PurchasePurposeHistoryReader,
        category_lister: CategoryPurchaseAccountLister,
        product_account_resolver: ProductPurchaseAccountResolver,
        approved_category_ids: Iterable[int],
    ) -> None:
        self._purpose_reader = purpose_reader
        self._category_lister = category_lister
        self._product_account_resolver = product_account_resolver
        self._approved_category_ids = normalize_resale_category_ids(approved_category_ids)

    def purpose_is_resale(self, *, review_id: str, company_id: int, review_version: int) -> bool:
        """True only for a RESALE purpose recorded for exactly ``review_version``.

        Raises ``ProductRemediationStalePurchasePurposeError`` when ``review_version`` has
        no purpose but another version has RESALE (same rule as the 18E-1B decision gate).
        """

        resolutions = self._purpose_reader.list_purchase_purpose_resolutions(
            review_id=review_id,
            company_id=company_id,
        )
        current = [resolution for resolution in resolutions if resolution.review_version == review_version]
        if len(current) > 1:
            raise ProductRemediationCategoryError("More than one purchase purpose exists for this review version.")
        if current:
            return current[0].purchase_purpose is PurchasePurpose.RESALE
        if any(resolution.purchase_purpose is PurchasePurpose.RESALE for resolution in resolutions):
            raise ProductRemediationStalePurchasePurposeError(
                "A RESALE purchase purpose exists only for another review version; record the purchase purpose "
                "again for the current review version."
            )
        return False

    def validate_before_write(
        self,
        *,
        company_id: int,
        categ_id: int | None,
        is_storable: bool,
        resale: bool,
    ) -> ValidatedProductCategory | None:
        """Validate the requested category read-only; ``None`` keeps Odoo's default category."""

        if resale:
            self._require_resale_request(categ_id=categ_id, is_storable=is_storable)
        if categ_id is None:
            return None
        category = self._find_category(company_id=company_id, categ_id=categ_id)
        if resale and category.purchase_account.status is not PurchaseAccountStatus.VALID:
            raise ProductRemediationCategoryError(
                "The RESALE category has no valid purchase account configured in Odoo "
                f"({category.purchase_account.status.value})."
            )
        return ValidatedProductCategory(categ_id=categ_id)

    def verify_created_product(
        self,
        *,
        company_id: int,
        product_id: int,
        product_template_id: int,
        categ_id: int | None,
        is_storable: bool,
        resale: bool,
    ) -> None:
        """Re-read a product this workflow created and fail closed on any drift."""

        try:
            resolution = self._product_account_resolver.execute(
                GetProductPurchaseAccountQuery(company_id=company_id, product_id=product_id)
            )
        except PurchaseAccountProductNotFoundError as exc:
            raise ProductRemediationVerificationError(
                "The created product could not be read back for this company."
            ) from exc
        if resolution.product_id != product_id or resolution.product_template_id != product_template_id:
            raise ProductRemediationVerificationError("The created product read back with a different identity.")
        if resolution.product_company_id not in (None, company_id):
            raise ProductRemediationVerificationError("The created product belongs to another company.")
        actual_categ_id = resolution.category.category_id if resolution.category is not None else None
        if categ_id is not None and actual_categ_id != categ_id:
            raise ProductRemediationVerificationError("The created product is not in the reserved category.")
        if resolution.is_storable is not None and resolution.is_storable is not is_storable:
            raise ProductRemediationVerificationError("The created product storability does not match the request.")
        if not resale:
            return
        eligibility = evaluate_resale_product_eligibility(
            resolution,
            company_id=company_id,
            approved_category_ids=self._approved_category_ids,
        )
        if not eligibility.eligible:
            raise ProductRemediationVerificationError(
                "The created product is not eligible for RESALE: "
                + ",".join(blocker.value for blocker in eligibility.blockers)
                + "."
            )

    def _require_resale_request(self, *, categ_id: int | None, is_storable: bool) -> None:
        if not self._approved_category_ids:
            raise ProductRemediationCategoryError("No product category is approved for RESALE.")
        if categ_id is None:
            raise ProductRemediationCategoryError("RESALE product creation requires an explicit categ_id.")
        if categ_id not in self._approved_category_ids:
            raise ProductRemediationCategoryError("categ_id is not an approved RESALE product category.")
        if is_storable is not False:
            raise ProductRemediationCategoryError("RESALE product creation supports non-storable products only.")

    def _find_category(self, *, company_id: int, categ_id: int) -> CategoryPurchaseAccountConfiguration:
        categories = self._category_lister.execute(ListCategoryPurchaseAccountsQuery(company_id=company_id))
        matches = [category for category in categories if category.category_id == categ_id]
        if len(matches) != 1:
            raise ProductRemediationCategoryError("categ_id is not an existing Odoo product category.")
        return matches[0]


__all__ = [
    "CategoryPurchaseAccountLister",
    "ProductRemediationCategoryPolicy",
]
