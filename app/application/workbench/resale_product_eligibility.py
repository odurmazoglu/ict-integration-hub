"""Pure RESALE v1 product-eligibility policy (P0-PROD-18E-1A).

Decides whether one already-discovered Odoo product may back a RESALE purchase line.
It evaluates facts only -- a P0-PROD-18D ``ProductPurchaseAccountResolution`` and the
configured ``RESALE_PRODUCT_CATEGORY_IDS`` allowlist -- and never reads or writes Odoo
itself. Nothing here is wired into a workflow yet (P0-PROD-18E-1B does that).

Odoo stays the accounting source of truth. The policy approves *categories*, never
accounts: whatever valid account Odoo's category configuration resolves to is accepted,
so a future change of a category's account in Odoo needs no Hub change. No account
id, code, name or type is required or preferred here.

Fail closed: every required fact must be positively known. The approval is
deliberately scoped to the *pre-fiscal-position* account -- fiscal-position mapping is
never evaluated, so an eligible result never claims to know the final Vendor Bill
line account.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.purchase_account_discovery import (
    FiscalPositionMapping,
    ProductPurchaseAccountResolution,
    PurchaseAccountSource,
    PurchaseAccountStatus,
    PurchaseAccountView,
)


class ResaleProductBlocker(StrEnum):
    """Why a product is not eligible to back a RESALE purchase line."""

    RESALE_CATEGORY_ALLOWLIST_EMPTY = "resale_category_allowlist_empty"
    PRODUCT_UNKNOWN = "product_unknown"
    PRODUCT_INACTIVE = "product_inactive"
    PRODUCT_COMPANY_MISMATCH = "product_company_mismatch"
    PRODUCT_CATEGORY_MISSING = "product_category_missing"
    CATEGORY_NOT_APPROVED_FOR_RESALE = "category_not_approved_for_resale"
    PRODUCT_STORABLE = "product_storable"
    PRODUCT_STORABILITY_UNKNOWN = "product_storability_unknown"
    PRODUCT_ACCOUNT_OVERRIDE_CONFIGURED = "product_account_override_configured"
    CATEGORY_ACCOUNT_NOT_CONFIGURED = "category_account_not_configured"
    CATEGORY_ACCOUNT_UNAVAILABLE = "category_account_unavailable"
    CATEGORY_ACCOUNT_DEPRECATED = "category_account_deprecated"
    PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE = "pre_fiscal_position_account_not_determinable"
    PURCHASE_ACCOUNT_EVIDENCE_INCONSISTENT = "purchase_account_evidence_inconsistent"


_CATEGORY_ACCOUNT_BLOCKERS = {
    PurchaseAccountStatus.NOT_CONFIGURED: ResaleProductBlocker.CATEGORY_ACCOUNT_NOT_CONFIGURED,
    PurchaseAccountStatus.UNAVAILABLE: ResaleProductBlocker.CATEGORY_ACCOUNT_UNAVAILABLE,
    PurchaseAccountStatus.DEPRECATED: ResaleProductBlocker.CATEGORY_ACCOUNT_DEPRECATED,
}


@dataclass(frozen=True, slots=True)
class ResaleProductEligibility(ApplicationDTO):
    """Outcome of evaluating one product for RESALE v1.

    ``pre_fiscal_position_account`` is presented only when eligible and is Odoo's own
    category-configured account *before* any fiscal-position mapping -- never the final
    Vendor Bill line account. ``fiscal_position_mapping`` is always ``NOT_EVALUATED``.
    """

    eligible: bool
    product_id: int | None
    category_id: int | None
    pre_fiscal_position_account: PurchaseAccountView | None
    blockers: tuple[ResaleProductBlocker, ...] = field(default_factory=tuple)
    fiscal_position_mapping: FiscalPositionMapping = FiscalPositionMapping.NOT_EVALUATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.eligible != (not self.blockers):
            raise WorkbenchContractError("Eligibility must match the absence of blockers.")
        if self.eligible and (
            self.product_id is None or self.category_id is None or self.pre_fiscal_position_account is None
        ):
            raise WorkbenchContractError("An eligible result requires a product, category and account.")
        if not self.eligible and self.pre_fiscal_position_account is not None:
            raise WorkbenchContractError("An ineligible result must not present an account.")


def normalize_resale_category_ids(category_ids: Iterable[int]) -> frozenset[int]:
    """Validate an allowlist of exact positive category ids, collapsing duplicates."""

    if isinstance(category_ids, (str, bytes)):
        raise WorkbenchContractError("RESALE category ids must be a collection of integers.")
    normalized: set[int] = set()
    for category_id in category_ids:
        if type(category_id) is not int or category_id <= 0:
            raise WorkbenchContractError("RESALE category ids must be positive integers.")
        normalized.add(category_id)
    return frozenset(normalized)


def evaluate_resale_product_eligibility(
    resolution: ProductPurchaseAccountResolution | None,
    *,
    company_id: int,
    approved_category_ids: Iterable[int],
) -> ResaleProductEligibility:
    """Evaluate one discovered product against the RESALE v1 policy, failing closed.

    ``resolution`` is ``None`` when no product identity is known. ``approved_category_ids``
    is matched by exact id only -- no hierarchy, no names.
    """

    if type(company_id) is not int or company_id <= 0:
        raise WorkbenchContractError("company_id must be positive.")
    if resolution is not None and not isinstance(resolution, ProductPurchaseAccountResolution):
        raise WorkbenchContractError("A ProductPurchaseAccountResolution is required.")
    approved = normalize_resale_category_ids(approved_category_ids)

    blockers: list[ResaleProductBlocker] = []
    if not approved:
        blockers.append(ResaleProductBlocker.RESALE_CATEGORY_ALLOWLIST_EMPTY)
    if resolution is None:
        blockers.append(ResaleProductBlocker.PRODUCT_UNKNOWN)
        return _result(product_id=None, category_id=None, account=None, blockers=blockers)

    blockers.extend(_product_blockers(resolution, company_id=company_id))
    blockers.extend(_category_blockers(resolution, approved=approved))
    blockers.extend(_account_blockers(resolution))

    category_id = resolution.category.category_id if resolution.category is not None else None
    return _result(
        product_id=resolution.product_id,
        category_id=category_id,
        account=resolution.pre_fiscal_position_account,
        blockers=blockers,
    )


def _product_blockers(resolution: ProductPurchaseAccountResolution, *, company_id: int) -> list[ResaleProductBlocker]:
    blockers: list[ResaleProductBlocker] = []
    if resolution.product_active is not True:
        blockers.append(ResaleProductBlocker.PRODUCT_INACTIVE)
    if resolution.product_company_id not in (None, company_id):
        blockers.append(ResaleProductBlocker.PRODUCT_COMPANY_MISMATCH)
    if resolution.is_storable is None:
        blockers.append(ResaleProductBlocker.PRODUCT_STORABILITY_UNKNOWN)
    elif resolution.is_storable is not False:
        blockers.append(ResaleProductBlocker.PRODUCT_STORABLE)
    if resolution.product_override.status is not PurchaseAccountStatus.NOT_CONFIGURED:
        blockers.append(ResaleProductBlocker.PRODUCT_ACCOUNT_OVERRIDE_CONFIGURED)
    return blockers


def _category_blockers(
    resolution: ProductPurchaseAccountResolution, *, approved: frozenset[int]
) -> list[ResaleProductBlocker]:
    category = resolution.category
    if category is None:
        return [ResaleProductBlocker.PRODUCT_CATEGORY_MISSING]
    blockers: list[ResaleProductBlocker] = []
    if approved and category.category_id not in approved:
        blockers.append(ResaleProductBlocker.CATEGORY_NOT_APPROVED_FOR_RESALE)
    status = category.purchase_account.status
    if status is not PurchaseAccountStatus.VALID:
        blockers.append(_CATEGORY_ACCOUNT_BLOCKERS.get(status, ResaleProductBlocker.CATEGORY_ACCOUNT_UNAVAILABLE))
    return blockers


def _account_blockers(resolution: ProductPurchaseAccountResolution) -> list[ResaleProductBlocker]:
    if not resolution.pre_fiscal_position_account_determinable or resolution.pre_fiscal_position_account is None:
        return [ResaleProductBlocker.PRE_FISCAL_POSITION_ACCOUNT_NOT_DETERMINABLE]
    category = resolution.category
    category_account = category.purchase_account.account if category is not None else None
    if (
        resolution.pre_fiscal_position_account_source is not PurchaseAccountSource.CATEGORY
        or category_account is None
        or category_account != resolution.pre_fiscal_position_account
    ):
        return [ResaleProductBlocker.PURCHASE_ACCOUNT_EVIDENCE_INCONSISTENT]
    return []


def _result(
    *,
    product_id: int | None,
    category_id: int | None,
    account: PurchaseAccountView | None,
    blockers: list[ResaleProductBlocker],
) -> ResaleProductEligibility:
    unique_blockers = tuple(dict.fromkeys(blockers))
    eligible = not unique_blockers
    return ResaleProductEligibility(
        eligible=eligible,
        product_id=product_id,
        category_id=category_id,
        pre_fiscal_position_account=account if eligible else None,
        blockers=unique_blockers,
    )


__all__ = [
    "ResaleProductBlocker",
    "ResaleProductEligibility",
    "evaluate_resale_product_eligibility",
    "normalize_resale_category_ids",
]
