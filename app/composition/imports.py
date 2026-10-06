from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import partial

from sqlalchemy import Connection, Engine
from sqlalchemy.orm import Session

from app.application.decision import (
    DecisionEngine,
    ManualReviewStrategy,
    VendorBillReviewRecommendationStrategy,
    WorkflowStrategyResolver,
)
from app.application.expense_mapping import OperatingExpenseMatchingEngine
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
from app.application.workbench.dto import ReviewItem
from app.application.workbench.operator_guidance import OperatorGuidanceFacts
from app.application.workbench.projection_sync import WorkbenchProjectionSources, WorkbenchProjectionSynchronizer
from app.composition.resale_decision_gate import build_resale_decision_gate
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
from app.erp.odoo.selected_expense_account_reader import OdooSelectedAccountReader
from app.erp.odoo.selected_product_reader import OdooSelectedProductReader
from app.erp.odoo.supplier_product_repository import OdooSupplierProductRepository
from app.erp.odoo.tax_repository import OdooTaxRepository
from app.erp.odoo.workbench_projection_publisher import OdooWorkbenchProjectionAdapter
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
from app.matching import PartnerMatchingEngine, ProductMatchingEngine
from app.persistence import (
    SqlAlchemyExecutionRuntimeRepository,
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyImportHistory,
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewBillingEvidenceReader,
    SqlAlchemyReviewClassificationEvidenceReader,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyUnitOfWork,
)
from app.persistence.workbench_review_accounting_resolution_repository import (
    SqlAlchemyReviewAccountingResolutionRepository,
)
from app.persistence.workbench_review_purchase_purpose_resolution_repository import (
    SqlAlchemyReviewPurchasePurposeResolutionRepository,
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

    return ImportInvoiceUseCase(
        import_history=import_history,
        decision_engine=decision_engine,
        review_item_creation_service=ReviewItemCreationService(SqlAlchemyReviewRepository(session)),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        workbench_projection_synchronizer=build_runtime_workbench_projection_synchronizer(
            session=session, settings=settings, odoo_client=odoo_client
        ),
    )


def build_workbench_projection_synchronizer(
    *,
    session: Session | None = None,
    engine: Engine | None = None,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
    projection_adapter: OdooWorkbenchProjectionAdapter | None = None,
    mapping: OdooWorkbenchProjectionFieldMapping | None = None,
) -> WorkbenchProjectionSynchronizer:
    """The canonical OPS-UI-01A synchronizer, independent of the runtime publish flag.

    The caller's ``session`` is used only to learn which database to read (its
    engine, taken here in the composing thread, with no I/O). The synchronizer never
    touches that session: every sync opens its own read-only session on a fresh
    connection, reads the *committed* snapshot, and closes it -- in whichever thread
    runs the sync (including an ``asyncio.to_thread`` worker). A projection failure
    can therefore only discard that private read session, never the business
    transaction. Used directly by the reconcile CLI; runtime use cases get it through
    :func:`build_runtime_workbench_projection_synchronizer`, which honours the flag.
    """

    bound_engine = engine if engine is not None else _engine_of(session)
    resolved_mapping = mapping or OdooWorkbenchProjectionFieldMapping.from_environment()
    adapter = projection_adapter or OdooWorkbenchJson2ProjectionAdapter(
        client=odoo_client or OdooJson2Client.from_settings(settings)
    )

    eligible_asset_account_ids = tuple(sorted(settings.odoo_fixed_asset_account_ids))
    # ADR-0013 guidance is read only when at least one guidance field is mapped, so an
    # unconfigured deployment performs exactly the pre-ADR-0013 reads and writes.
    guidance_mapped = any(
        name is not None
        for name in (
            resolved_mapping.next_action,
            resolved_mapping.todo,
            resolved_mapping.completed,
            resolved_mapping.eligible_asset_accounts,
        )
    )

    @contextmanager
    def read_scope() -> Iterator[WorkbenchProjectionSources]:
        with open_read_only_session(bound_engine) as read_session:
            review_repository = SqlAlchemyReviewRepository(read_session)
            yield WorkbenchProjectionSources(
                guidance_facts_reader=(
                    partial(
                        _operator_guidance_facts,
                        purpose_repository=SqlAlchemyReviewPurchasePurposeResolutionRepository(read_session),
                        accounting_repository=SqlAlchemyReviewAccountingResolutionRepository(read_session),
                        eligible_asset_account_ids=eligible_asset_account_ids,
                    )
                    if guidance_mapped
                    else None
                ),
                review_reader=review_repository,
                accepted_decision_reader=review_repository,
                accepted_source_reader=SqlAlchemyExecutionSourceInvoiceReader(read_session),
                execution_snapshot_reader=SqlAlchemyExecutionRuntimeRepository(read_session),
                publisher=OdooWorkbenchProjectionPublisher(
                    adapter=adapter,
                    mapping=resolved_mapping,
                    classification_service=WorkbenchClassificationProjectionService(
                        SqlAlchemyReviewClassificationEvidenceReader(read_session)
                    ),
                ),
            )

    return WorkbenchProjectionSynchronizer(read_scope=read_scope)


def _operator_guidance_facts(
    review: ReviewItem,
    company_id: int,
    *,
    purpose_repository: SqlAlchemyReviewPurchasePurposeResolutionRepository,
    accounting_repository: SqlAlchemyReviewAccountingResolutionRepository,
    eligible_asset_account_ids: tuple[int, ...],
) -> OperatorGuidanceFacts:
    """ADR-0013 guidance facts, read from the same private read-only session."""

    purposes = purpose_repository.list_purchase_purpose_resolutions(review_id=review.review_id, company_id=company_id)
    current = next((item for item in purposes if item.review_version == review.version), None)
    return OperatorGuidanceFacts(
        current_purchase_purpose=current.purchase_purpose if current is not None else None,
        latest_purchase_purpose=purposes[-1].purchase_purpose if purposes else None,
        latest_accounting_resolution=accounting_repository.find_latest_accounting_resolution(
            review_id=review.review_id, company_id=company_id
        ),
        eligible_asset_account_ids=eligible_asset_account_ids,
    )


@contextmanager
def open_read_only_session(engine: Engine) -> Iterator[Session]:
    """A private Hub session on its own connection; PostgreSQL transactions are READ ONLY.

    Created, used and closed by the caller's thread. It only ever sees committed data
    (a separate connection), is always rolled back, and never shares state with any
    business/request session.
    """

    connection = engine.connect()
    try:
        if engine.dialect.name == "postgresql":
            connection = connection.execution_options(postgresql_readonly=True)
        read_session = Session(bind=connection, autoflush=False)
        try:
            yield read_session
        finally:
            read_session.rollback()
            read_session.close()
    finally:
        connection.close()


def _engine_of(session: Session | None) -> Engine:
    if session is None:
        raise ValueError("A session or engine is required to locate the Hub database.")
    bind = session.get_bind()
    return bind.engine if isinstance(bind, Connection) else bind


def build_runtime_workbench_projection_synchronizer(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> WorkbenchProjectionSynchronizer | None:
    """``None`` unless ``ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED``: disabled means no Odoo read or write."""

    if not settings.odoo_workbench_projection_publish_enabled:
        return None
    return build_workbench_projection_synchronizer(session=session, settings=settings, odoo_client=odoo_client)


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
        erp_reference_validator=build_workbench_erp_reference_validator(read_adapter),
        decision_submitter=build_odoo_decision_submitter(
            session=session, settings=settings, odoo_client=resolved_odoo_client, read_adapter=read_adapter
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


def build_workbench_erp_reference_validator(read_adapter: OdooReadOnlyAdapter) -> WorkbenchErpReferenceValidator:
    """Exact-id ERP reference validation for Odoo-sourced decision candidates."""

    return WorkbenchErpReferenceValidator(
        partner_repository=OdooPartnerReferenceRepository(adapter=read_adapter),
        company_repository=OdooCompanyReferenceRepository(adapter=read_adapter),
        sales_order_repository=OdooSalesOrderReferenceRepository(adapter=read_adapter),
        sales_order_line_repository=OdooSalesOrderLineReferenceRepository(adapter=read_adapter),
        purchase_order_repository=OdooPurchaseOrderReferenceRepository(adapter=read_adapter),
        customer_invoice_repository=OdooCustomerInvoiceReferenceRepository(adapter=read_adapter),
        opportunity_repository=OdooOpportunityReferenceRepository(adapter=read_adapter),
        analytic_account_repository=OdooAnalyticAccountReferenceRepository(adapter=read_adapter),
    )


def build_odoo_decision_submitter(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client,
    read_adapter: OdooReadOnlyAdapter,
) -> SubmitReviewDecisionUseCase:
    """The canonical decision use case as composed for Odoo-sourced decisions."""

    return SubmitReviewDecisionUseCase(
        review_decision_writer=SqlAlchemyReviewRepository(session),
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        billing_evidence_reader=SqlAlchemyReviewBillingEvidenceReader(session),
        selected_product_reader=OdooSelectedProductReader(
            product_repository=OdooProductRepository(adapter=read_adapter),
        ),
        selected_account_reader=OdooSelectedAccountReader(adapter=read_adapter),
        resale_decision_gate=build_resale_decision_gate(session=session, settings=settings, odoo_client=odoo_client),
        projection_synchronizer=build_runtime_workbench_projection_synchronizer(
            session=session, settings=settings, odoo_client=odoo_client
        ),
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
    decision_engine = _build_deterministic_decision_engine(
        session=session,
        provider=provider,
        read_adapter=read_adapter,
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


def build_odoo_read_repository_provider(
    *,
    read_adapter: OdooReadOnlyAdapter,
) -> StaticRepositoryProvider:
    """The sanctioned read-only Odoo repository provider used by deterministic matching."""

    return StaticRepositoryProvider(
        partner_repository=OdooPartnerRepository(adapter=read_adapter),
        product_repository=OdooProductRepository(adapter=read_adapter),
        tax_repository=OdooTaxRepository(adapter=read_adapter),
        currency_repository=OdooCurrencyRepository(adapter=read_adapter),
        company_repository=OdooCompanyRepository(adapter=read_adapter),
    )


def _build_deterministic_decision_engine(
    *,
    session: Session,
    provider: StaticRepositoryProvider,
    read_adapter: OdooReadOnlyAdapter,
) -> DecisionEngine:
    """The production deterministic ``DecisionEngine`` shared by import and reclassification.

    The operating-expense mapping table itself is the activation gate: with no
    enabled row the matcher returns NOT_FOUND and identifier-free invoices stay in
    Manual Review. No runtime feature flag is introduced.
    """

    operating_expense_matcher = OperatingExpenseMatchingEngine(SqlAlchemyOperatingExpenseMappingRepository(session))
    return DecisionEngine(
        rule_engine=DeterministicRuleEngine(
            partner_matcher=PartnerMatchingEngine(provider),
            product_matcher=ProductMatchingEngine(
                provider,
                supplier_product_repository=OdooSupplierProductRepository(adapter=read_adapter),
            ),
            tax_mapper=TaxMappingEngine(provider.tax_repository),
            operating_expense_matcher=operating_expense_matcher,
        ),
        strategy_resolver=WorkflowStrategyResolver(
            [
                VendorBillReviewRecommendationStrategy(),
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


def build_deterministic_decision_engine(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> DecisionEngine:
    """Public composer for the deterministic ``DecisionEngine`` from settings."""

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    provider = build_odoo_read_repository_provider(read_adapter=read_adapter)
    return _build_deterministic_decision_engine(
        session=session,
        provider=provider,
        read_adapter=read_adapter,
    )
