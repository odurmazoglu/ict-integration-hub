"""PR D: read-only Odoo lookups for Workbench new-product creation.

Only ``search_read`` on the already allowlisted ``product.product`` / ``product.template`` /
``product.supplierinfo`` models; this module has no path to an Odoo write. Text comparisons use ``=ilike`` with
the LIKE wildcards escaped, i.e. an exact, case-insensitive match -- never a pattern.
"""

from __future__ import annotations

from typing import Any

from app.application.workbench.product_line_creation import CreatedProductSnapshot
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

PRODUCT_MODEL = "product.product"
TEMPLATE_MODEL = "product.template"
SUPPLIERINFO_MODEL = "product.supplierinfo"
#: Duplicate checks only need to know *that* (and a few of which) records exist.
DUPLICATE_LOOKUP_LIMIT = 5
#: A single-variant product has one; anything above this is reported as "several".
VARIANT_LOOKUP_LIMIT = 5
SAFE_CATALOG_READ_ERROR = "Odoo product catalog returned an unsafe response."


class OdooProductCatalogReader:
    """Duplicate prevention: Internal Reference and name lookups, company scope incl. shared."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_variant_ids_by_default_code(self, default_code: str, *, company_id: int) -> tuple[int, ...]:
        records = self._adapter.search_read(
            model=PRODUCT_MODEL,
            domain=[
                ["default_code", "=ilike", escape_like(default_code.strip())],
                ["active", "in", [True, False]],
                ["company_id", "in", [company_id, False]],
            ],
            fields=["id"],
            limit=DUPLICATE_LOOKUP_LIMIT,
        )
        return _ids(records)

    def find_template_ids_by_name(self, name: str, *, company_id: int) -> tuple[int, ...]:
        records = self._adapter.search_read(
            model=TEMPLATE_MODEL,
            domain=[
                ["name", "=ilike", escape_like(name.strip())],
                ["company_id", "in", [company_id, False]],
            ],
            fields=["id"],
            limit=DUPLICATE_LOOKUP_LIMIT,
        )
        return _ids(records)

    def find_supplierinfo_ids_by_product_code(self, product_code: str, *, company_id: int) -> tuple[int, ...]:
        records = self._adapter.search_read(
            model=SUPPLIERINFO_MODEL,
            domain=[
                ["product_code", "=ilike", escape_like(product_code.strip())],
                ["company_id", "in", [company_id, False]],
            ],
            fields=["id"],
            limit=DUPLICATE_LOOKUP_LIMIT,
        )
        return _ids(records)


class OdooCreatedProductReader:
    """Recovery: read one template and its variants to compare with a recorded intent."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def read_template(self, template_id: int) -> CreatedProductSnapshot | None:
        templates = self._adapter.search_read(
            model=TEMPLATE_MODEL,
            domain=[["id", "=", template_id], ["active", "in", [True, False]]],
            fields=["id", "name", "default_code", "type", "uom_id", "categ_id", "is_storable"],
            limit=2,
        )
        if not templates:
            return None
        if len(templates) > 1 or templates[0].get("id") != template_id:
            raise ErpRepositoryResponseError(SAFE_CATALOG_READ_ERROR)
        template = templates[0]
        variants = self._adapter.search_read(
            model=PRODUCT_MODEL,
            domain=[["product_tmpl_id", "=", template_id], ["active", "in", [True, False]]],
            fields=["id"],
            limit=VARIANT_LOOKUP_LIMIT,
        )
        is_storable = template.get("is_storable")
        return CreatedProductSnapshot(
            template_id=template_id,
            name=_text(template.get("name")),
            default_code=_text(template.get("default_code")),
            product_type=_text(template.get("type")),
            uom_id=many2one_id(template.get("uom_id")),
            categ_id=many2one_id(template.get("categ_id")),
            is_storable=is_storable if isinstance(is_storable, bool) else None,
            variant_ids=_ids(variants),
        )


def escape_like(value: str) -> str:
    """Escape LIKE wildcards so ``=ilike`` compares the literal text."""

    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ids(records: tuple[dict[str, Any], ...]) -> tuple[int, ...]:
    ids = []
    for record in records:
        value = record.get("id")
        if type(value) is not int or value <= 0:
            raise ErpRepositoryResponseError(SAFE_CATALOG_READ_ERROR)
        ids.append(value)
    return tuple(sorted(ids))


def _text(value: Any) -> str | None:
    if value is None or value is False:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["OdooCreatedProductReader", "OdooProductCatalogReader", "escape_like"]
