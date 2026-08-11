from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.decision_rule_repository import (
    OdooDecisionRuleDataError,
    OdooDecisionRuleReadError,
    OdooDecisionRuleRepository,
)
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.provider import OdooRepositoryProvider
from app.erp.odoo.tax_repository import OdooTaxRepository
from app.erp.odoo.workbench_billing_authoring_reader import (
    OdooWorkbenchBillingAuthoringReader,
    OdooWorkbenchBillingFieldMapping,
)
from app.erp.odoo.workbench_candidate_reader import (
    OdooWorkbenchAllocationFieldMapping,
    OdooWorkbenchDecisionCandidateReader,
    OdooWorkbenchFieldMapping,
    OdooWorkbenchParentFieldMapping,
)
from app.erp.odoo.workbench_reference_repositories import (
    OdooAnalyticAccountReferenceRepository,
    OdooCompanyReferenceRepository,
    OdooCurrencyReferenceRepository,
    OdooCustomerInvoiceReferenceRepository,
    OdooOpportunityReferenceRepository,
    OdooPartnerReferenceRepository,
    OdooProductReferenceRepository,
    OdooPurchaseOrderReferenceRepository,
    OdooSalesOrderLineReferenceRepository,
    OdooSalesOrderReferenceRepository,
    OdooSalesTaxReferenceRepository,
)

__all__ = [
    "OdooAnalyticAccountReferenceRepository",
    "OdooWorkbenchBillingAuthoringReader",
    "OdooWorkbenchBillingFieldMapping",
    "OdooCompanyRepository",
    "OdooCompanyReferenceRepository",
    "OdooCurrencyReferenceRepository",
    "OdooCurrencyRepository",
    "OdooCustomerInvoiceReferenceRepository",
    "OdooDecisionRuleDataError",
    "OdooDecisionRuleReadError",
    "OdooDecisionRuleRepository",
    "OdooOpportunityReferenceRepository",
    "OdooPartnerRepository",
    "OdooPartnerReferenceRepository",
    "OdooProductReferenceRepository",
    "OdooProductRepository",
    "OdooPurchaseOrderReferenceRepository",
    "OdooReadOnlyAdapter",
    "OdooRepositoryProvider",
    "OdooSalesOrderLineReferenceRepository",
    "OdooSalesOrderReferenceRepository",
    "OdooSalesTaxReferenceRepository",
    "OdooTaxRepository",
    "OdooWorkbenchAllocationFieldMapping",
    "OdooWorkbenchDecisionCandidateReader",
    "OdooWorkbenchFieldMapping",
    "OdooWorkbenchParentFieldMapping",
]
