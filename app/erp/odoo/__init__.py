from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.provider import OdooRepositoryProvider
from app.erp.odoo.tax_repository import OdooTaxRepository

__all__ = [
    "OdooCompanyRepository",
    "OdooCurrencyRepository",
    "OdooPartnerRepository",
    "OdooProductRepository",
    "OdooReadOnlyAdapter",
    "OdooRepositoryProvider",
    "OdooTaxRepository",
]
