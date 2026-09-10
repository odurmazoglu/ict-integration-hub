from __future__ import annotations

from app.application.workbench.supplier_resolution import ResolutionPartnerRecord
from app.erp.odoo.partner_repository import OdooPartnerRepository


class OdooSupplierResolutionPartnerReader:
    """Read-only ``res.partner`` reader for MATCH_EXISTING supplier-resolution validation.

    Thin wrapper over the sanctioned read-only :class:`OdooPartnerRepository`; it
    performs no write and adds no new Odoo model access.
    """

    def __init__(self, *, partner_repository: OdooPartnerRepository) -> None:
        self._partner_repository = partner_repository

    def find_partner_by_id(self, partner_id: int) -> ResolutionPartnerRecord | None:
        if type(partner_id) is not int or isinstance(partner_id, bool) or partner_id <= 0:
            return None
        partners = self._partner_repository.find_by_ids((partner_id,))
        for partner in partners:
            if partner.id == partner_id:
                return ResolutionPartnerRecord(
                    id=partner.id,
                    name=partner.name,
                    vat=partner.tax_number,
                    active=partner.active,
                    company_id=partner.company_id,
                )
        return None
