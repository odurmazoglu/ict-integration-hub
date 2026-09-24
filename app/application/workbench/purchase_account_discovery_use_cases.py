"""Application boundary for the read-only purchase-account discovery (P0-PROD-18D)."""

from __future__ import annotations

from app.application.workbench.exceptions import (
    PurchaseAccountCompanyContextError,
    PurchaseAccountDiscoveryError,
    PurchaseAccountProductNotFoundError,
    WorkbenchContractError,
)
from app.application.workbench.ports import PurchaseAccountDiscoveryReader
from app.application.workbench.purchase_account_discovery import (
    CategoryPurchaseAccountConfiguration,
    GetProductPurchaseAccountQuery,
    ListCategoryPurchaseAccountsQuery,
    ProductPurchaseAccountResolution,
    PurchaseAccountRecord,
    category_configuration,
    resolve_product_purchase_account,
)

COMPANY_CONTEXT_UNVERIFIED_MESSAGE = (
    "Odoo company-dependent accounting configuration cannot be attributed unambiguously to this company."
)


class ListCategoryPurchaseAccountsUseCase:
    """Every product category with its configured purchase account, for one company."""

    def __init__(self, *, reader: PurchaseAccountDiscoveryReader) -> None:
        self._reader = reader

    def execute(self, query: ListCategoryPurchaseAccountsQuery) -> tuple[CategoryPurchaseAccountConfiguration, ...]:
        if not isinstance(query, ListCategoryPurchaseAccountsQuery):
            raise WorkbenchContractError("A canonical ListCategoryPurchaseAccountsQuery is required.")
        _require_unambiguous_company_context(self._reader, company_id=query.company_id)
        categories = self._reader.list_categories()
        accounts_by_id = _accounts_by_id(
            self._reader,
            company_id=query.company_id,
            account_ids=tuple(c.expense_account_id for c in categories if c.expense_account_id is not None),
        )
        return tuple(
            category_configuration(category, company_id=query.company_id, accounts_by_id=accounts_by_id)
            for category in sorted(categories, key=lambda c: c.id)
        )


class GetProductPurchaseAccountUseCase:
    """One product's pre-fiscal-position purchase account, as Odoo configuration defines it."""

    def __init__(self, *, reader: PurchaseAccountDiscoveryReader) -> None:
        self._reader = reader

    def execute(self, query: GetProductPurchaseAccountQuery) -> ProductPurchaseAccountResolution:
        if not isinstance(query, GetProductPurchaseAccountQuery):
            raise WorkbenchContractError("A canonical GetProductPurchaseAccountQuery is required.")
        _require_unambiguous_company_context(self._reader, company_id=query.company_id)
        product = self._reader.find_product(company_id=query.company_id, product_id=query.product_id)
        if product is None or product.product_id != query.product_id:
            raise PurchaseAccountProductNotFoundError("The product was not found for this company.")
        if product.company_id not in (None, query.company_id):
            raise PurchaseAccountProductNotFoundError("The product was not found for this company.")
        category = (
            self._reader.find_category(category_id=product.category_id) if product.category_id is not None else None
        )
        if category is not None and category.id != product.category_id:
            raise PurchaseAccountDiscoveryError("The product category could not be read consistently.")
        account_ids = tuple(
            account_id
            for account_id in (product.override_account_id, category.expense_account_id if category else None)
            if account_id is not None
        )
        accounts_by_id = _accounts_by_id(self._reader, company_id=query.company_id, account_ids=account_ids)
        return resolve_product_purchase_account(
            product,
            category=category,
            company_id=query.company_id,
            accounts_by_id=accounts_by_id,
        )


def _require_unambiguous_company_context(reader: PurchaseAccountDiscoveryReader, *, company_id: int) -> None:
    # Company-dependent fields (property_account_expense_id/_categ_id) are read in the
    # integration user's Odoo company context, which the read-only client never sets
    # explicitly. That context is provably the requesting company only when it is the
    # one and only company the user can see.
    if reader.accessible_company_ids() != (company_id,):
        raise PurchaseAccountCompanyContextError(COMPANY_CONTEXT_UNVERIFIED_MESSAGE)


def _accounts_by_id(
    reader: PurchaseAccountDiscoveryReader,
    *,
    company_id: int,
    account_ids: tuple[int, ...],
) -> dict[int, PurchaseAccountRecord]:
    unique_ids = tuple(sorted(set(account_ids)))
    if not unique_ids:
        return {}
    records = reader.find_accounts(company_id=company_id, account_ids=unique_ids)
    return {record.id: record for record in records if record.id in unique_ids}


__all__ = [
    "COMPANY_CONTEXT_UNVERIFIED_MESSAGE",
    "GetProductPurchaseAccountUseCase",
    "ListCategoryPurchaseAccountsUseCase",
]
