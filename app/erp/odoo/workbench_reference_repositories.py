from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.application.workbench.erp_references import (
    AnalyticAccountReference,
    CompanyReference,
    CurrencyReference,
    CustomerInvoiceReference,
    OpportunityReference,
    PartnerReference,
    ProductReference,
    PurchaseOrderReference,
    SalesOrderLineReference,
    SalesOrderReference,
    SalesTaxReference,
)
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id


class OdooPartnerReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_partners_by_ids(self, ids: tuple[int, ...]) -> tuple[PartnerReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="res.partner",
            ids=ids,
            fields=["id", "company_id", "commercial_partner_id", "active"],
        )
        return tuple(
            PartnerReference(
                id=int(record["id"]),
                company_id=many2one_id(record.get("company_id")),
                commercial_partner_id=many2one_id(record.get("commercial_partner_id")),
                active=bool(record.get("active", True)),
            )
            for record in records
        )


class OdooCompanyReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_companies_by_ids(self, ids: tuple[int, ...]) -> tuple[CompanyReference, ...]:
        records = _read_by_ids(self._adapter, model="res.company", ids=ids, fields=["id"])
        return tuple(CompanyReference(id=int(record["id"])) for record in records)


class OdooSalesOrderReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_sales_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="sale.order",
            ids=ids,
            fields=["id", "company_id", "partner_id"],
        )
        return tuple(
            SalesOrderReference(
                id=int(record["id"]),
                company_id=_required_many2one_id(record.get("company_id")),
                partner_id=many2one_id(record.get("partner_id")),
            )
            for record in records
        )


class OdooSalesOrderLineReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_sales_order_lines_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesOrderLineReference, ...]:
        records = _read_by_ids(self._adapter, model="sale.order.line", ids=ids, fields=["id", "order_id"])
        return tuple(
            SalesOrderLineReference(id=int(record["id"]), order_id=_required_many2one_id(record.get("order_id")))
            for record in records
        )


class OdooPurchaseOrderReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_purchase_orders_by_ids(self, ids: tuple[int, ...]) -> tuple[PurchaseOrderReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="purchase.order",
            ids=ids,
            fields=["id", "company_id", "partner_id"],
        )
        return tuple(
            PurchaseOrderReference(
                id=int(record["id"]),
                company_id=_required_many2one_id(record.get("company_id")),
                partner_id=many2one_id(record.get("partner_id")),
            )
            for record in records
        )


class OdooCustomerInvoiceReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_customer_invoices_by_ids(self, ids: tuple[int, ...]) -> tuple[CustomerInvoiceReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="account.move",
            ids=ids,
            fields=["id", "company_id", "partner_id", "move_type"],
        )
        return tuple(
            CustomerInvoiceReference(
                id=int(record["id"]),
                company_id=_required_many2one_id(record.get("company_id")),
                partner_id=many2one_id(record.get("partner_id")),
                move_type=_required_text(record.get("move_type")),
            )
            for record in records
        )


class OdooOpportunityReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_opportunities_by_ids(self, ids: tuple[int, ...]) -> tuple[OpportunityReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="crm.lead",
            ids=ids,
            fields=["id", "company_id", "partner_id"],
        )
        return tuple(
            OpportunityReference(
                id=int(record["id"]),
                company_id=many2one_id(record.get("company_id")),
                partner_id=many2one_id(record.get("partner_id")),
            )
            for record in records
        )


class OdooAnalyticAccountReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_analytic_accounts_by_ids(self, ids: tuple[int, ...]) -> tuple[AnalyticAccountReference, ...]:
        records = _read_by_ids(self._adapter, model="account.analytic.account", ids=ids, fields=["id", "company_id"])
        return tuple(
            AnalyticAccountReference(id=int(record["id"]), company_id=many2one_id(record.get("company_id")))
            for record in records
        )


class OdooProductReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_products_by_ids(self, ids: tuple[int, ...]) -> tuple[ProductReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="product.product",
            ids=ids,
            fields=["id", "company_id", "active"],
        )
        return tuple(
            ProductReference(
                id=int(record["id"]),
                company_id=many2one_id(record.get("company_id")),
                active=bool(record.get("active", True)),
            )
            for record in records
        )


class OdooSalesTaxReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_sales_taxes_by_ids(self, ids: tuple[int, ...]) -> tuple[SalesTaxReference, ...]:
        records = _read_by_ids(
            self._adapter,
            model="account.tax",
            ids=ids,
            fields=["id", "company_id", "active", "type_tax_use"],
        )
        return tuple(
            SalesTaxReference(
                id=int(record["id"]),
                company_id=many2one_id(record.get("company_id")),
                active=bool(record.get("active", True)),
                usage_type=record.get("type_tax_use") if isinstance(record.get("type_tax_use"), str) else None,
            )
            for record in records
        )


class OdooCurrencyReferenceRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_currencies_by_codes(self, codes: tuple[str, ...]) -> tuple[CurrencyReference, ...]:
        if not codes:
            return ()
        records = self._adapter.search_read_all(
            model="res.currency",
            domain=[["name", "in", list(codes)]],
            fields=["id", "name", "active"],
            max_records=len(codes),
        )
        return tuple(
            CurrencyReference(
                id=int(record["id"]),
                code=_required_text(record.get("name")),
                active=bool(record.get("active", True)),
            )
            for record in records
        )


def _read_by_ids(
    adapter: OdooReadOnlyAdapter,
    *,
    model: str,
    ids: Sequence[int],
    fields: list[str],
) -> tuple[dict[str, Any], ...]:
    if not ids:
        return ()
    return adapter.search_read_all(
        model=model,
        domain=[["id", "in", list(ids)]],
        fields=fields,
        max_records=len(ids),
    )


def _required_many2one_id(value: Any) -> int:
    reference_id = many2one_id(value)
    if reference_id is None:
        raise ValueError("required many2one id is missing")
    return reference_id


def _required_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required text is missing")
    return value
