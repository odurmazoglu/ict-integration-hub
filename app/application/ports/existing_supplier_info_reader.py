from __future__ import annotations

from typing import Protocol

from app.application.workbench.product_remediation import ExistingSupplierInfo


class ExistingSupplierInfoReader(Protocol):
    """Port for the narrow, read-only capability of finding existing Odoo ``product.supplierinfo``.

    Used only for the CREATE_NEW_PRODUCT natural-identity pre-check (read-before-write):
    a hit here means Odoo already has a supplierinfo for this exact vendor/product-code
    identity (from prior manual data entry, or an earlier uncertain remote outcome) and
    the existing product must be reused rather than creating another one.
    """

    async def find_existing(
        self,
        *,
        partner_id: int,
        product_code: str,
        company_id: int,
    ) -> tuple[ExistingSupplierInfo, ...]:
        pass
