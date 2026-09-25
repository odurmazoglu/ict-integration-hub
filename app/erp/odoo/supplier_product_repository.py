"""Read-only, supplier-scoped ``product.supplierinfo`` access for deterministic matching.

P0-PROD-19A-3. Structurally read-only: it holds only an :class:`OdooReadOnlyAdapter`
(``search_read`` only) and never the JSON-2 client. Every domain is an exact identity
domain, company-scoped to ``[company_id, False]``, and bounded by the caller's ``limit``.
Any record outside the requested identity is treated as a malformed response and fails
closed. Which row/variant counts as a match is decided by the matcher, not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.models import ProductVariant, SupplierProductCode
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

SUPPLIERINFO_MODEL = "product.supplierinfo"
PRODUCT_VARIANT_MODEL = "product.product"
SUPPLIERINFO_FIELDS = ["id", "partner_id", "product_code", "product_tmpl_id", "product_id", "company_id"]
PRODUCT_VARIANT_FIELDS = ["id", "product_tmpl_id", "active", "company_id"]

SAFE_MALFORMED_SUPPLIERINFO = "Odoo product.supplierinfo returned a record outside the requested identity."
SAFE_MALFORMED_VARIANT = "Odoo product.product returned a variant outside the requested template."


class OdooSupplierProductRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_supplier_product_codes(
        self,
        *,
        partner_id: int,
        product_code: str,
        company_id: int,
        limit: int,
    ) -> Sequence[SupplierProductCode]:
        _require_positive(partner_id, "partner_id")
        _require_positive(company_id, "company_id")
        _require_positive(limit, "limit")
        if not isinstance(product_code, str) or not product_code or product_code != product_code.strip():
            raise ValueError("product_code must be a non-empty, whitespace-normalized string.")
        records = self._adapter.search_read(
            model=SUPPLIERINFO_MODEL,
            domain=[
                ["partner_id", "=", partner_id],
                ["product_code", "=", product_code],
                ["company_id", "in", [company_id, False]],
            ],
            fields=SUPPLIERINFO_FIELDS,
            limit=limit,
        )
        _require_bounded(records, limit, SAFE_MALFORMED_SUPPLIERINFO)
        return tuple(
            _supplier_product_code(record, partner_id=partner_id, product_code=product_code, company_id=company_id)
            for record in records
        )

    def find_template_variants(
        self,
        *,
        product_tmpl_id: int,
        company_id: int,
        variant_id: int | None,
        limit: int,
    ) -> Sequence[ProductVariant]:
        _require_positive(product_tmpl_id, "product_tmpl_id")
        _require_positive(company_id, "company_id")
        _require_positive(limit, "limit")
        domain: list[Any] = [
            ["product_tmpl_id", "=", product_tmpl_id],
            ["company_id", "in", [company_id, False]],
        ]
        if variant_id is not None:
            _require_positive(variant_id, "variant_id")
            domain.append(["id", "=", variant_id])
        records = self._adapter.search_read(
            model=PRODUCT_VARIANT_MODEL,
            domain=domain,
            fields=PRODUCT_VARIANT_FIELDS,
            limit=limit,
        )
        _require_bounded(records, limit, SAFE_MALFORMED_VARIANT)
        return tuple(
            _product_variant(record, product_tmpl_id=product_tmpl_id, variant_id=variant_id, company_id=company_id)
            for record in records
        )


def _supplier_product_code(
    record: dict[str, Any],
    *,
    partner_id: int,
    product_code: str,
    company_id: int,
) -> SupplierProductCode:
    record_id = _positive_or_none(record.get("id"))
    product_tmpl_id = _positive_or_none(many2one_id(record.get("product_tmpl_id")))
    raw_product_id = record.get("product_id")
    product_id = None if raw_product_id is False or raw_product_id is None else many2one_id(raw_product_id)
    record_company_id = _company_id(record.get("company_id"))
    if (
        record_id is None
        or many2one_id(record.get("partner_id")) != partner_id
        or record.get("product_code") != product_code
        or product_tmpl_id is None
        or (raw_product_id not in (False, None) and _positive_or_none(product_id) is None)
        or record_company_id not in (company_id, None)
    ):
        raise ErpRepositoryResponseError(SAFE_MALFORMED_SUPPLIERINFO)
    return SupplierProductCode(
        id=record_id,
        partner_id=partner_id,
        product_code=product_code,
        product_tmpl_id=product_tmpl_id,
        product_id=product_id,
        company_id=record_company_id,
    )


def _product_variant(
    record: dict[str, Any],
    *,
    product_tmpl_id: int,
    variant_id: int | None,
    company_id: int,
) -> ProductVariant:
    record_id = _positive_or_none(record.get("id"))
    record_company_id = _company_id(record.get("company_id"))
    active = record.get("active")
    if (
        record_id is None
        or (variant_id is not None and record_id != variant_id)
        or many2one_id(record.get("product_tmpl_id")) != product_tmpl_id
        or record_company_id not in (company_id, None)
        or not isinstance(active, bool)
    ):
        raise ErpRepositoryResponseError(SAFE_MALFORMED_VARIANT)
    return ProductVariant(id=record_id, product_tmpl_id=product_tmpl_id, active=active, company_id=record_company_id)


def _require_bounded(records: Sequence[dict[str, Any]], limit: int, safe_message: str) -> None:
    if len(records) > limit or any(not isinstance(record, dict) for record in records):
        raise ErpRepositoryResponseError(safe_message)


def _company_id(value: Any) -> int | None:
    if value is False or value is None:
        return None
    company_id = many2one_id(value)
    if company_id is None:
        return -1  # malformed; never equal to a requested company or None
    return company_id


def _positive_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _require_positive(value: Any, name: str) -> None:
    if _positive_or_none(value) is None:
        raise ValueError(f"{name} must be a positive integer.")
