from __future__ import annotations

from dataclasses import dataclass

from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.tax_repository import OdooTaxRepository


@dataclass(frozen=True, slots=True)
class OdooRepositoryProvider:
    partner_repository: OdooPartnerRepository
    product_repository: OdooProductRepository
    tax_repository: OdooTaxRepository
    currency_repository: OdooCurrencyRepository
    company_repository: OdooCompanyRepository

    @classmethod
    def from_adapter(cls, adapter: OdooReadOnlyAdapter) -> OdooRepositoryProvider:
        return cls(
            partner_repository=OdooPartnerRepository(adapter=adapter),
            product_repository=OdooProductRepository(adapter=adapter),
            tax_repository=OdooTaxRepository(adapter=adapter),
            currency_repository=OdooCurrencyRepository(adapter=adapter),
            company_repository=OdooCompanyRepository(adapter=adapter),
        )
