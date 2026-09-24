"""Read-only discovery of Odoo product/category purchase-account configuration (P0-PROD-18D).

Answers, before any RESALE write path exists, *which* account Odoo's own product and
category configuration would give a product-backed Vendor Bill line. Odoo stays the
source of truth: nothing here decides, pins, or writes an account.

The semantics mirror Odoo's ``product.template._get_product_accounts()`` for the
purchase side, and deliberately stop where that stops being provable from
configuration alone:

* a product-level ``property_account_expense_id`` override, when set, is what Odoo
  uses -- even if that account is unusable, so an unusable override never silently
  falls back to the category;
* otherwise the product category's ``property_account_expense_categ_id`` is used;
* fiscal-position account mapping depends on the partner/bill and is never evaluated
  here (``FiscalPositionMapping.NOT_EVALUATED``), so the result is at most the
  *pre-fiscal-position* account;
* storable goods may be redirected to stock-valuation accounts by Odoo's inventory
  accounting, which this reader does not model -- they fail closed as not determinable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError

#: Upper bound for the category listing. A catalog larger than this is refused rather
#: than silently truncated.
MAX_DISCOVERY_CATEGORIES = 500


class PurchaseAccountStatus(StrEnum):
    """State of one configured purchase-account reference."""

    #: The referenced account is readable, scoped to the company, and not deprecated.
    VALID = "valid"
    #: No account is configured on this record.
    NOT_CONFIGURED = "not_configured"
    #: An account is referenced but is not readable for this company (missing,
    #: archived, or owned by another company).
    UNAVAILABLE = "unavailable"
    #: The referenced account is deprecated.
    DEPRECATED = "deprecated"


class PurchaseAccountSource(StrEnum):
    """Which configuration Odoo would take the pre-fiscal-position account from."""

    PRODUCT_OVERRIDE = "product_override"
    CATEGORY = "category"


class FiscalPositionMapping(StrEnum):
    """Fiscal-position handling. Only one value exists on purpose -- see module docstring."""

    NOT_EVALUATED = "not_evaluated"


class PurchaseAccountBlocker(StrEnum):
    """Why a product's pre-fiscal-position purchase account is not determinable."""

    PRODUCT_INACTIVE = "product_inactive"
    PRODUCT_CATEGORY_MISSING = "product_category_missing"
    PRODUCT_OVERRIDE_ACCOUNT_UNAVAILABLE = "product_override_account_unavailable"
    PRODUCT_OVERRIDE_ACCOUNT_DEPRECATED = "product_override_account_deprecated"
    CATEGORY_ACCOUNT_NOT_CONFIGURED = "category_account_not_configured"
    CATEGORY_ACCOUNT_UNAVAILABLE = "category_account_unavailable"
    CATEGORY_ACCOUNT_DEPRECATED = "category_account_deprecated"
    STOCK_VALUATION_NOT_EVALUATED = "stock_valuation_not_evaluated"


# --------------------------------------------------------------------------- reader records


@dataclass(frozen=True, slots=True)
class PurchaseAccountRecord(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``account.account``."""

    id: int
    code: str
    name: str
    account_type: str
    company_ids: tuple[int, ...]
    #: ``None`` when this Odoo version exposes no ``deprecated`` field.
    deprecated: bool | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "id must be a positive integer.")
        _require_text(self.code, "code is required.")
        _require_text(self.name, "name is required.")
        _require_text(self.account_type, "account_type is required.")
        object.__setattr__(self, "company_ids", tuple(self.company_ids))
        for company_id in self.company_ids:
            _require_positive_int(company_id, "company_ids must contain positive integers.")
        if self.deprecated is not None and not isinstance(self.deprecated, bool):
            raise WorkbenchContractError("deprecated must be a boolean when set.")


@dataclass(frozen=True, slots=True)
class CategoryPurchaseAccountRecord(ApplicationDTO):
    """Minimal read-only projection of an Odoo ``product.category``."""

    id: int
    name: str
    expense_account_id: int | None
    complete_name: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.id, "id must be a positive integer.")
        _require_text(self.name, "name is required.")
        _require_optional_positive_int(self.expense_account_id, "expense_account_id must be positive when set.")


@dataclass(frozen=True, slots=True)
class ProductPurchaseAccountRecord(ApplicationDTO):
    """Minimal read-only projection of one Odoo product variant and its template."""

    product_id: int
    product_template_id: int
    name: str
    active: bool
    #: ``None`` for a product shared across companies.
    company_id: int | None
    product_type: str
    category_id: int | None
    override_account_id: int | None
    #: ``None`` when this Odoo version exposes no ``is_storable`` field.
    is_storable: bool | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.product_id, "product_id must be a positive integer.")
        _require_positive_int(self.product_template_id, "product_template_id must be a positive integer.")
        _require_text(self.name, "name is required.")
        if not isinstance(self.active, bool):
            raise WorkbenchContractError("active must be a boolean.")
        _require_optional_positive_int(self.company_id, "company_id must be positive when set.")
        _require_text(self.product_type, "product_type is required.")
        _require_optional_positive_int(self.category_id, "category_id must be positive when set.")
        _require_optional_positive_int(self.override_account_id, "override_account_id must be positive when set.")
        if self.is_storable is not None and not isinstance(self.is_storable, bool):
            raise WorkbenchContractError("is_storable must be a boolean when set.")


# --------------------------------------------------------------------------- discovery results


@dataclass(frozen=True, slots=True)
class PurchaseAccountView(ApplicationDTO):
    """An account as presented to the operator."""

    account_id: int
    code: str
    name: str
    account_type: str
    deprecated: bool | None


@dataclass(frozen=True, slots=True)
class ResolvedPurchaseAccount(ApplicationDTO):
    """One configured account reference and its evaluated status."""

    status: PurchaseAccountStatus
    configured_account_id: int | None = None
    account: PurchaseAccountView | None = None


@dataclass(frozen=True, slots=True)
class CategoryPurchaseAccountConfiguration(ApplicationDTO):
    """A category's *configured* purchase account -- not a product's final account."""

    category_id: int
    category_name: str
    category_complete_name: str | None
    purchase_account: ResolvedPurchaseAccount


@dataclass(frozen=True, slots=True)
class ProductPurchaseAccountResolution(ApplicationDTO):
    """What Odoo configuration says one product's purchase account is, before fiscal positions."""

    product_id: int
    product_template_id: int
    product_name: str
    product_active: bool
    product_company_id: int | None
    product_type: str
    is_storable: bool | None
    category: CategoryPurchaseAccountConfiguration | None
    product_override: ResolvedPurchaseAccount
    #: Set only when ``pre_fiscal_position_account_determinable`` -- never a best guess.
    pre_fiscal_position_account: PurchaseAccountView | None
    pre_fiscal_position_account_source: PurchaseAccountSource | None
    pre_fiscal_position_account_determinable: bool
    blockers: tuple[PurchaseAccountBlocker, ...] = field(default_factory=tuple)
    fiscal_position_mapping: FiscalPositionMapping = FiscalPositionMapping.NOT_EVALUATED

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.pre_fiscal_position_account_determinable != (not self.blockers):
            raise WorkbenchContractError("Determinability must match the absence of blockers.")
        if self.pre_fiscal_position_account_determinable and (
            self.pre_fiscal_position_account is None or self.pre_fiscal_position_account_source is None
        ):
            raise WorkbenchContractError("A determinable resolution requires an account and its source.")
        if not self.pre_fiscal_position_account_determinable and (
            self.pre_fiscal_position_account is not None or self.pre_fiscal_position_account_source is not None
        ):
            raise WorkbenchContractError("An undeterminable resolution must not present an account.")


# --------------------------------------------------------------------------- queries


@dataclass(frozen=True, slots=True)
class ListCategoryPurchaseAccountsQuery(ApplicationDTO):
    company_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")


@dataclass(frozen=True, slots=True)
class GetProductPurchaseAccountQuery(ApplicationDTO):
    company_id: int
    product_id: int

    def __post_init__(self) -> None:
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.product_id, "product_id must be a positive integer.")


# --------------------------------------------------------------------------- pure resolution rules


def resolve_account_reference(
    account_id: int | None,
    *,
    company_id: int,
    accounts_by_id: dict[int, PurchaseAccountRecord],
) -> ResolvedPurchaseAccount:
    """Evaluate one configured account reference against the company-scoped accounts read."""

    if account_id is None:
        return ResolvedPurchaseAccount(status=PurchaseAccountStatus.NOT_CONFIGURED)
    record = accounts_by_id.get(account_id)
    if record is None or record.id != account_id or company_id not in record.company_ids:
        return ResolvedPurchaseAccount(status=PurchaseAccountStatus.UNAVAILABLE, configured_account_id=account_id)
    view = PurchaseAccountView(
        account_id=record.id,
        code=record.code,
        name=record.name,
        account_type=record.account_type,
        deprecated=record.deprecated,
    )
    status = PurchaseAccountStatus.DEPRECATED if record.deprecated else PurchaseAccountStatus.VALID
    return ResolvedPurchaseAccount(status=status, configured_account_id=account_id, account=view)


def category_configuration(
    category: CategoryPurchaseAccountRecord,
    *,
    company_id: int,
    accounts_by_id: dict[int, PurchaseAccountRecord],
) -> CategoryPurchaseAccountConfiguration:
    return CategoryPurchaseAccountConfiguration(
        category_id=category.id,
        category_name=category.name,
        category_complete_name=category.complete_name,
        purchase_account=resolve_account_reference(
            category.expense_account_id, company_id=company_id, accounts_by_id=accounts_by_id
        ),
    )


_OVERRIDE_BLOCKERS = {
    PurchaseAccountStatus.UNAVAILABLE: PurchaseAccountBlocker.PRODUCT_OVERRIDE_ACCOUNT_UNAVAILABLE,
    PurchaseAccountStatus.DEPRECATED: PurchaseAccountBlocker.PRODUCT_OVERRIDE_ACCOUNT_DEPRECATED,
}
_CATEGORY_BLOCKERS = {
    PurchaseAccountStatus.NOT_CONFIGURED: PurchaseAccountBlocker.CATEGORY_ACCOUNT_NOT_CONFIGURED,
    PurchaseAccountStatus.UNAVAILABLE: PurchaseAccountBlocker.CATEGORY_ACCOUNT_UNAVAILABLE,
    PurchaseAccountStatus.DEPRECATED: PurchaseAccountBlocker.CATEGORY_ACCOUNT_DEPRECATED,
}


def resolve_product_purchase_account(
    product: ProductPurchaseAccountRecord,
    *,
    category: CategoryPurchaseAccountRecord | None,
    company_id: int,
    accounts_by_id: dict[int, PurchaseAccountRecord],
) -> ProductPurchaseAccountResolution:
    """Apply Odoo's purchase-account precedence to one product, failing closed on any doubt."""

    blockers: list[PurchaseAccountBlocker] = []
    if not product.active:
        blockers.append(PurchaseAccountBlocker.PRODUCT_INACTIVE)
    if product.is_storable is True or (product.is_storable is None and product.product_type != "service"):
        blockers.append(PurchaseAccountBlocker.STOCK_VALUATION_NOT_EVALUATED)

    category_config = (
        category_configuration(category, company_id=company_id, accounts_by_id=accounts_by_id)
        if category is not None
        else None
    )
    override = resolve_account_reference(
        product.override_account_id, company_id=company_id, accounts_by_id=accounts_by_id
    )

    candidate: ResolvedPurchaseAccount | None
    source: PurchaseAccountSource | None
    if override.status is not PurchaseAccountStatus.NOT_CONFIGURED:
        # Odoo uses a set override as-is; an unusable one never falls back to the category.
        candidate, source = override, PurchaseAccountSource.PRODUCT_OVERRIDE
        if override.status in _OVERRIDE_BLOCKERS:
            blockers.append(_OVERRIDE_BLOCKERS[override.status])
    elif category_config is None:
        candidate, source = None, None
        blockers.append(PurchaseAccountBlocker.PRODUCT_CATEGORY_MISSING)
    else:
        candidate, source = category_config.purchase_account, PurchaseAccountSource.CATEGORY
        if candidate.status in _CATEGORY_BLOCKERS:
            blockers.append(_CATEGORY_BLOCKERS[candidate.status])

    determinable = not blockers
    return ProductPurchaseAccountResolution(
        product_id=product.product_id,
        product_template_id=product.product_template_id,
        product_name=product.name,
        product_active=product.active,
        product_company_id=product.company_id,
        product_type=product.product_type,
        is_storable=product.is_storable,
        category=category_config,
        product_override=override,
        pre_fiscal_position_account=candidate.account if determinable and candidate is not None else None,
        pre_fiscal_position_account_source=source if determinable else None,
        pre_fiscal_position_account_determinable=determinable,
        blockers=tuple(blockers),
    )


def _require_positive_int(value: object, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_optional_positive_int(value: object, message: str) -> None:
    if value is not None:
        _require_positive_int(value, message)


def _require_text(value: object, message: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


__all__ = [
    "MAX_DISCOVERY_CATEGORIES",
    "CategoryPurchaseAccountConfiguration",
    "CategoryPurchaseAccountRecord",
    "FiscalPositionMapping",
    "GetProductPurchaseAccountQuery",
    "ListCategoryPurchaseAccountsQuery",
    "ProductPurchaseAccountRecord",
    "ProductPurchaseAccountResolution",
    "PurchaseAccountBlocker",
    "PurchaseAccountRecord",
    "PurchaseAccountSource",
    "PurchaseAccountStatus",
    "PurchaseAccountView",
    "ResolvedPurchaseAccount",
    "category_configuration",
    "resolve_account_reference",
    "resolve_product_purchase_account",
]
