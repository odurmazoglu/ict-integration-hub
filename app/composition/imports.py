from __future__ import annotations

from sqlalchemy.orm import Session

from app.application.decision import DecisionEngine, ManualReviewStrategy, VendorBillStrategy, WorkflowStrategyResolver
from app.application.ports import InvoiceImportHistory
from app.application.rules import DeterministicRuleEngine, InvoiceDecisionRuleEngine, OdooDecisionRuleFieldMapping
from app.application.use_cases import ImportInvoiceUseCase
from app.application.workbench import (
    ReviewItemCreationService,
    SubmitReviewDecisionUseCase,
    WorkbenchClassificationProjectionService,
    WorkbenchDecisionIngestionWorkflow,
    WorkbenchErpReferenceValidator,
    WorkbenchProjectionPublisher,
)
from app.billing import VendorBillBuilder
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings
from app.erp.odoo import (
    OdooDecisionRuleRepository,
    OdooWorkbenchDecisionCandidateReader,
    OdooWorkbenchFieldMapping,
    OdooWorkbenchJson2ProjectionAdapter,
    OdooWorkbenchProjectionFieldMapping,
    OdooWorkbenchProjectionPublisher,
)
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.company_repository import OdooCompanyRepository
from app.erp.odoo.currency_repository import OdooCurrencyRepository
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.tax_repository import OdooTaxRepository
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
from app.erp.provider import StaticRepositoryProvider
from app.erp.write import AccountMoveRepository, OdooVendorBillWritePolicy, OdooVendorBillWriter
from app.matching import PartnerMatchingEngine, ProductMatchingEngine
from app.persistence import (
    SqlAlchemyImportHistory,
    SqlAlchemyReviewBillingEvidenceReader,
    SqlAlchemyReviewClassificationEvidenceReader,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.services.document_service import InvoiceDocumentService
from app.services.document_storage import DocumentStorage
from app.services.uyumsoft_canonical_import import ExactCompanyResolver, UyumsoftCanonicalInvoiceImporter
from app.tax_mapping import TaxMappingEngine


def build_import_invoice_use_case(
    *,
    import_history: InvoiceImportHistory,
    decision_engine: DecisionEngine,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> ImportInvoiceUseCase:
    """Compose import runtime with optional best-effort Odoo Workbench projection publishing."""

    publisher = (
        build_odoo_workbench_projection_publisher(session=session, settings=settings, odoo_client=odoo_client)
        if settings.odoo_workbench_projection_publish_enabled
        else None
    )
    return ImportInvoiceUseCase(
        import_history=import_history,
        decision_engine=decision_engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        workbench_projection_publisher=publisher,
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def build_odoo_workbench_projection_publisher(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchProjectionPublisher:
    """Build the Odoo publisher with historical classification projection from Hub persistence."""

    adapter = OdooWorkbenchJson2ProjectionAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    classification_service = WorkbenchClassificationProjectionService(
        SqlAlchemyReviewClassificationEvidenceReader(session)
    )
    return OdooWorkbenchProjectionPublisher(
        adapter=adapter,
        mapping=OdooWorkbenchProjectionFieldMapping.from_environment(),
        classification_service=classification_service,
    )


def build_odoo_workbench_decision_ingestion_workflow(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchDecisionIngestionWorkflow:
    """Compose explicit Odoo Workbench decision ingestion without workflow execution."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    projection_adapter = OdooWorkbenchJson2ProjectionAdapter(client=resolved_odoo_client)
    return WorkbenchDecisionIngestionWorkflow(
        candidate_reader=OdooWorkbenchDecisionCandidateReader(
            adapter=read_adapter,
            mapping=OdooWorkbenchFieldMapping.from_environment(),
        ),
        erp_reference_validator=WorkbenchErpReferenceValidator(
            partner_repository=OdooPartnerReferenceRepository(adapter=read_adapter),
            company_repository=OdooCompanyReferenceRepository(adapter=read_adapter),
            sales_order_repository=OdooSalesOrderReferenceRepository(adapter=read_adapter),
            sales_order_line_repository=OdooSalesOrderLineReferenceRepository(adapter=read_adapter),
            purchase_order_repository=OdooPurchaseOrderReferenceRepository(adapter=read_adapter),
            customer_invoice_repository=OdooCustomerInvoiceReferenceRepository(adapter=read_adapter),
            opportunity_repository=OdooOpportunityReferenceRepository(adapter=read_adapter),
            analytic_account_repository=OdooAnalyticAccountReferenceRepository(adapter=read_adapter),
        ),
        decision_submitter=SubmitReviewDecisionUseCase(
            review_decision_writer=SqlAlchemyReviewRepository(session),
            execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
            billing_evidence_reader=SqlAlchemyReviewBillingEvidenceReader(session),
        ),
        acknowledgement_publisher=OdooWorkbenchProjectionPublisher(
            adapter=projection_adapter,
            mapping=OdooWorkbenchProjectionFieldMapping.from_environment(),
            classification_service=WorkbenchClassificationProjectionService(
                SqlAlchemyReviewClassificationEvidenceReader(session)
            ),
        ),
        unit_of_work=SqlAlchemyUnitOfWork(session),
    )


def build_uyumsoft_canonical_invoice_importer(
    *,
    session: Session,
    settings: Settings,
    uyumsoft_client: UyumsoftSoapClient,
    storage: DocumentStorage,
    odoo_client: OdooJson2Client | None = None,
) -> UyumsoftCanonicalInvoiceImporter:
    """Compose Uyumsoft inbound document normalization into the canonical import use case."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    company_repository = OdooCompanyRepository(adapter=read_adapter)
    provider = StaticRepositoryProvider(
        partner_repository=OdooPartnerRepository(adapter=read_adapter),
        product_repository=OdooProductRepository(adapter=read_adapter),
        tax_repository=OdooTaxRepository(adapter=read_adapter),
        currency_repository=OdooCurrencyRepository(adapter=read_adapter),
        company_repository=company_repository,
    )
    decision_engine = DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=PartnerMatchingEngine(provider),
            product_matcher=ProductMatchingEngine(provider),
            tax_mapper=TaxMappingEngine(provider.tax_repository),
        ),
        strategy_resolver=WorkflowStrategyResolver(
            [
                VendorBillStrategy(
                    vendor_bill_builder=VendorBillBuilder(),
                    vendor_bill_writer=OdooVendorBillWriter(
                        repository=AccountMoveRepository(client=resolved_odoo_client),
                        policy=OdooVendorBillWritePolicy.from_settings(settings),
                    ),
                ),
                ManualReviewStrategy(),
            ]
        ),
        decision_rule_repository=OdooDecisionRuleRepository(
            adapter=read_adapter,
            mapping=OdooDecisionRuleFieldMapping(),
            currency_repository=provider.currency_repository,
        ),
        invoice_decision_rule_engine=InvoiceDecisionRuleEngine(),
    )

    def use_case_factory() -> ImportInvoiceUseCase:
        return build_import_invoice_use_case(
            import_history=SqlAlchemyImportHistory(session),
            decision_engine=decision_engine,
            session=session,
            settings=settings,
            odoo_client=resolved_odoo_client,
        )

    return UyumsoftCanonicalInvoiceImporter(
        document_service=InvoiceDocumentService(session=session, client=uyumsoft_client, storage=storage),
        storage=storage,
        company_resolver=ExactCompanyResolver(company_repository),
        import_use_case_factory=use_case_factory,
    )
