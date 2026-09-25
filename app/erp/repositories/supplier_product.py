from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.erp.models import ProductVariant, SupplierProductCode


class SupplierProductRepository(Protocol):
    """Read-only, supplier-scoped ``product.supplierinfo`` lookups for deterministic matching."""

    def find_supplier_product_codes(
        self,
        *,
        partner_id: int,
        product_code: str,
        company_id: int,
        limit: int,
    ) -> Sequence[SupplierProductCode]:
        pass

    def find_template_variants(
        self,
        *,
        product_tmpl_id: int,
        company_id: int,
        variant_id: int | None,
        limit: int,
    ) -> Sequence[ProductVariant]:
        pass
