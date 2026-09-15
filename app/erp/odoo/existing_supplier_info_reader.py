from __future__ import annotations

from app.application.workbench.product_remediation import ExistingSupplierInfo
from app.erp.write.odoo_supplierinfo_writer import OdooSupplierInfoRepository


class OdooExistingSupplierInfoReader:
    """Read-only ``product.supplierinfo`` reader for the CREATE_NEW_PRODUCT natural-identity pre-check.

    Thin wrapper over the sanctioned :class:`OdooSupplierInfoRepository` read path
    (``find_by_partner_and_code``, the same lookup ``OdooSupplierInfoWriter`` uses
    before every write); it performs no write and adds no new Odoo model access.
    """

    def __init__(self, *, repository: OdooSupplierInfoRepository) -> None:
        self._repository = repository

    async def find_existing(
        self,
        *,
        partner_id: int,
        product_code: str,
        company_id: int,
    ) -> tuple[ExistingSupplierInfo, ...]:
        records = await self._repository.find_by_partner_and_code(
            partner_id=partner_id,
            product_code=product_code,
            company_id=company_id,
        )
        return tuple(
            ExistingSupplierInfo(
                id=record.id,
                partner_id=record.partner_id,
                product_tmpl_id=record.product_tmpl_id,
                product_id=record.product_id,
                product_code=record.product_code,
                company_id=record.company_id,
            )
            for record in records
        )
