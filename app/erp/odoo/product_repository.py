from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.erp.models import Product
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

PRODUCT_FIELDS = ["id", "name", "default_code", "barcode", "active", "company_id"]


class OdooProductRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_by_default_code(self, default_code: str, *, company_id: int | None = None) -> Sequence[Product]:
        return self._find_by_unique_field("default_code", default_code, company_id=company_id)

    def find_by_barcode(self, barcode: str, *, company_id: int | None = None) -> Sequence[Product]:
        return self._find_by_unique_field("barcode", barcode, company_id=company_id)

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Product]:
        if not ids:
            return ()
        records = self._adapter.search_read_all(
            model="product.product",
            domain=[["id", "in", list(ids)]],
            fields=PRODUCT_FIELDS,
            max_records=len(ids),
        )
        return tuple(_product(record) for record in records)

    def _find_by_unique_field(self, field: str, value: str, *, company_id: int | None) -> Sequence[Product]:
        domain: list[Any] = [[field, "=", value]]
        if company_id is not None:
            domain.append(["company_id", "in", [company_id, False]])
        records = self._adapter.search_read_all(model="product.product", domain=domain, fields=PRODUCT_FIELDS)
        return tuple(_product(record) for record in records)


def _product(record: dict[str, Any]) -> Product:
    return Product(
        id=int(record["id"]),
        name=_optional_str(record.get("name")),
        default_code=_optional_str(record.get("default_code")),
        barcode=_optional_str(record.get("barcode")),
        active=bool(record.get("active", True)),
        company_id=many2one_id(record.get("company_id")),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
