"""PR F / F2: bounded, read-only Odoo catalog snapshot for advisory product suggestions.

Exactly three paged ``search_read`` calls per snapshot -- ``product.product``,
``product.supplierinfo`` and ``product.category``, all already in the read-only model
allowlist -- never one call per line. ``display_name`` carries the variant attribute
values, so ``product.template.attribute.value`` is not needed. A read beyond a bound fails
closed (the tick then regenerates nothing); nothing here can write.
"""

from __future__ import annotations

from typing import Any

from app.advisory.product_suggestions import CatalogSnapshot, CatalogSupplierMapping, CatalogVariant
from app.erp.exceptions import ErpRepositoryError
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

MAX_VARIANTS = 5000
MAX_SUPPLIERINFO = 5000
MAX_CATEGORIES = 500

VARIANT_FIELDS = [
    "id",
    "display_name",
    "default_code",
    "product_tmpl_id",
    "categ_id",
    "company_id",
    "active",
    "purchase_ok",
]
SUPPLIERINFO_FIELDS = ["id", "partner_id", "product_code", "product_tmpl_id", "product_id", "company_id"]
CATEGORY_FIELDS = ["id", "complete_name"]


class AdvisoryCatalogBoundExceededError(ErpRepositoryError):
    """The Odoo catalog is larger than the advisory read bound; nothing is generated."""


class OdooAdvisoryCatalogReader:
    def __init__(
        self,
        *,
        adapter: OdooReadOnlyAdapter,
        max_variants: int = MAX_VARIANTS,
        max_supplierinfo: int = MAX_SUPPLIERINFO,
        max_categories: int = MAX_CATEGORIES,
    ) -> None:
        self._adapter = adapter
        self._max_variants = max_variants
        self._max_supplierinfo = max_supplierinfo
        self._max_categories = max_categories

    def read(self, *, company_id: int) -> CatalogSnapshot:
        variants = self._bounded(
            "product.product",
            [["active", "=", True], ["purchase_ok", "=", True]],
            VARIANT_FIELDS,
            self._max_variants,
        )
        supplierinfo = self._bounded(
            "product.supplierinfo",
            [["company_id", "in", [company_id, False]]],
            SUPPLIERINFO_FIELDS,
            self._max_supplierinfo,
        )
        categories = self._bounded("product.category", [], CATEGORY_FIELDS, self._max_categories)
        return CatalogSnapshot(
            variants=tuple(
                variant
                for variant in (_variant(record) for record in variants)
                if variant.company_id in (None, company_id)
            ),
            supplier_mappings=tuple(
                mapping for mapping in (_mapping(record) for record in supplierinfo) if mapping is not None
            ),
            category_paths=tuple(
                (int(record["id"]), str(record["complete_name"]))
                for record in categories
                if isinstance(record.get("complete_name"), str)
            ),
        )

    def _bounded(self, model: str, domain: list[Any], fields: list[str], bound: int) -> tuple[dict[str, Any], ...]:
        records = self._adapter.search_read_all(model=model, domain=domain, fields=fields, max_records=bound + 1)
        if len(records) > bound:
            raise AdvisoryCatalogBoundExceededError(f"Advisory catalog read of {model} exceeds {bound} records.")
        return records


def _variant(record: dict[str, Any]) -> CatalogVariant:
    template_id = many2one_id(record.get("product_tmpl_id"))
    if template_id is None:
        raise ErpRepositoryError("Odoo product.product returned a variant without a template.")
    return CatalogVariant(
        product_id=int(record["id"]),
        template_id=template_id,
        name=str(record.get("display_name") or ""),
        default_code=record["default_code"] if isinstance(record.get("default_code"), str) else None,
        active=bool(record.get("active")),
        purchase_ok=bool(record.get("purchase_ok")),
        category_id=many2one_id(record.get("categ_id")),
        company_id=many2one_id(record.get("company_id")),
    )


def _mapping(record: dict[str, Any]) -> CatalogSupplierMapping | None:
    partner_id = many2one_id(record.get("partner_id"))
    template_id = many2one_id(record.get("product_tmpl_id"))
    if partner_id is None or template_id is None:
        return None
    return CatalogSupplierMapping(
        supplierinfo_id=int(record["id"]),
        partner_id=partner_id,
        product_code=record["product_code"] if isinstance(record.get("product_code"), str) else None,
        template_id=template_id,
        product_id=many2one_id(record.get("product_id")),
        company_id=many2one_id(record.get("company_id")),
    )


__all__ = ["AdvisoryCatalogBoundExceededError", "OdooAdvisoryCatalogReader"]
