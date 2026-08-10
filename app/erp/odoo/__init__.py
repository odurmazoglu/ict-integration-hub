from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.provider import OdooRepositoryProvider
from app.erp.odoo.tax_repository import OdooTaxRepository
from app.erp.odoo.workbench_candidate_reader import (
    OdooWorkbenchAllocationFieldMapping,
    OdooWorkbenchDecisionCandidateReader,
    OdooWorkbenchFieldMapping,
    OdooWorkbenchParentFieldMapping,
)
from app.erp.odoo.workbench_reference_repositories import (
    OdooAnalyticAccountReferenceRepository,
    OdooCompanyReferenceRepository,
    OdooCustomerInvoiceReferenceRepository,
    OdooOpportunityReferenceRepository,
    OdooPartnerReferenceRepository,
    OdooPurchaseOrderReferenceRepository,
    OdooSalesOrderLineReferenceRepository,
    OdooSalesOrderReferenceRepository,
)

__all__ = [
    "OdooAnalyticAccountReferenceRepository",
    "OdooCompanyRepository",
    "OdooCompanyReferenceRepository",
    "OdooCurrencyRepository",
    "OdooCustomerInvoiceReferenceRepository",
    "OdooOpportunityReferenceRepository",
    "OdooPartnerRepository",
    "OdooPartnerReferenceRepository",
    "OdooProductRepository",
    "OdooPurchaseOrderReferenceRepository",
    "OdooReadOnlyAdapter",
    "OdooRepositoryProvider",
    "OdooSalesOrderLineReferenceRepository",
    "OdooSalesOrderReferenceRepository",
    "OdooTaxRepository",
    "OdooWorkbenchAllocationFieldMapping",
    "OdooWorkbenchDecisionCandidateReader",
    "OdooWorkbenchFieldMapping",
    "OdooWorkbenchParentFieldMapping",
]
