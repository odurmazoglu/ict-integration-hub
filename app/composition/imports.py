from __future__ import annotations

import logging
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
from app.application.effective_supplier import AcceptedSupplierReader, EffectiveSupplierResolver
from app.application.exceptions.base import ApplicationError
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
from app.application.workbench.exceptions import ReviewNotFoundError, WorkbenchContractError
from app.application.workbench.fixed_asset_lookup import FixedAssetAccountingReader
from app.application.workbench.operator_guidance import (
    OperatorGuidanceFacts,
    ResolvedProductLine,
    UnmatchedProductLine,
    resolve_accounting_labels,
)
from app.application.workbench.ports import SelectedProductReader
from app.application.workbench.product_line_projection import ProductLineReadFacts
from app.application.workbench.product_remediation import normalize_seller_item_code
from app.application.workbench.projection_sync import WorkbenchProjectionSources, WorkbenchProjectionSynchronizer
from app.application.workflow import ManualReviewReasonCode
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
from app.erp.odoo.fixed_asset_accounting_reader import OdooFixedAssetAccountingReader
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.selected_expense_account_reader import OdooSelectedAccountReader
from app.erp.odoo.selected_product_reader import OdooSelectedProductReader
from app.erp.odoo.supplier_product_repository import OdooSupplierProductRepository
from app.erp.odoo.tax_repository import OdooTaxRepository
from app.erp.odoo.workbench_operator_request_reader import OdooOperatorRequestFieldMapping
from app.erp.odoo.workbench_product_line_publisher import (
    OdooWorkbenchProductLineFieldMapping,
    OdooWorkbenchProductLinePublisher,
)
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
from app.matching import PartnerMatchingEngine, ProductMatchingEngine, ProductMatchStatus
from app.persistence import (
    SqlAlchemyExecutionRuntimeRepository,
    SqlAlchemyExecutionSourceInvoiceReader,
    SqlAlchemyImportHistory,
    SqlAlchemyOperatingExpenseMappingRepository,
    SqlAlchemyReviewBillingEvidenceReader,
    SqlAlchemyReviewClassificationEvidenceReader,
    SqlAlchemyReviewExecutionEvidenceReader,
    SqlAlchemyReviewRepository,
    SqlAlchemyReviewSourceInvoiceEvidenceReader,
    SqlAlchemyReviewSupplierRemediationEffectRepository,
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
    accounting_label_reader: FixedAssetAccountingReader | None = None,
    product_mapping_enabled: bool | None = None,
    product_line_mapping: OdooWorkbenchProductLineFieldMapping | None = None,
    product_lines_enabled: bool | None = None,
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

    # The "Ürün Eşleştir" guidance appears only together with its request fields (same switch
    # as the request handler), so the Studio selection value always exists before it is written.
    product_mapping = (
        _product_mapping_requests_enabled(settings) if product_mapping_enabled is None else product_mapping_enabled
    )

    # Accounting labels come from the existing read-only fixed-asset/account reference port.
    label_reader = accounting_label_reader
    if guidance_mapped and label_reader is None:
        label_reader = OdooFixedAssetAccountingReader(
            adapter=OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
        )

    # Resolved product lines ("Tamamlananlar") follow the same switch as the product guidance.
    product_name_reader = (
        OdooSelectedProductReader(
            product_repository=OdooProductRepository(
                adapter=OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
            )
        )
        if guidance_mapped and product_mapping
        else None
    )

    # PR B: child rows only when explicitly enabled. Disabled means no child mapping is
    # read, no product line facts are read and no child model is ever called. Enabled
    # with an invalid contract fails here, exactly like an incomplete parent mapping.
    lines_enabled = (
        settings.odoo_workbench_product_line_projection_enabled
        if product_lines_enabled is None
        else product_lines_enabled
    )
    product_line_publisher = (
        OdooWorkbenchProductLinePublisher(
            adapter=adapter,
            mapping=product_line_mapping or OdooWorkbenchProductLineFieldMapping.from_environment(),
        )
        if lines_enabled
        else None
    )

    @contextmanager
    def read_scope() -> Iterator[WorkbenchProjectionSources]:
        with open_read_only_session(bound_engine) as read_session:
            review_repository = SqlAlchemyReviewRepository(read_session)
            yield WorkbenchProjectionSources(
                product_line_reader=(
                    partial(
                        _product_line_read_facts,
                        source_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(read_session),
                        evidence_reader=review_repository,
                    )
                    if product_line_publisher is not None
                    else None
                ),
                guidance_facts_reader=(
                    partial(
                        _operator_guidance_facts,
                        purpose_repository=SqlAlchemyReviewPurchasePurposeResolutionRepository(read_session),
                        accounting_repository=SqlAlchemyReviewAccountingResolutionRepository(read_session),
                        eligible_asset_account_ids=eligible_asset_account_ids,
                        label_reader=label_reader,
                        source_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(read_session),
                        product_mapping_enabled=product_mapping,
                        execution_evidence_reader=review_repository,
                        product_name_reader=product_name_reader,
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
                    product_line_publisher=product_line_publisher,
                ),
            )

    return WorkbenchProjectionSynchronizer(read_scope=read_scope)


def _product_line_read_facts(
    review: ReviewItem,
    company_id: int,
    evidence_version: int | None,
    *,
    source_reader: SqlAlchemyReviewSourceInvoiceEvidenceReader,
    evidence_reader: SqlAlchemyReviewRepository,
) -> ProductLineReadFacts:
    """PR B committed facts for one review, from the same private read-only session.

    The source lines are the immutable source invoice (the evidence's own pinned
    invoice when a pre-feature review has no source row). The Stage-1 execution
    evidence of ``evidence_version`` is the product matching that ran under the
    review's effective supplier (PR #211); ``None`` when that version has none.
    """

    evidence = None
    if evidence_version is not None:
        try:
            evidence = evidence_reader.get_review_execution_evidence(
                review_id=review.review_id, company_id=company_id, review_version=evidence_version
            )
        except ReviewNotFoundError:
            evidence = None
    try:
        source_lines = source_reader.get(review_id=review.review_id, company_id=company_id).invoice.lines
    except ReviewNotFoundError:
        source_lines = evidence.invoice.lines if evidence is not None else ()
    if evidence is None:
        return ProductLineReadFacts(source_lines=source_lines)
    evidence_lines = {}
    for item in evidence.product_match.line_results:
        number = (item.line_number or "").strip()
        if not number or number in evidence_lines:
            raise WorkbenchContractError("Execution evidence product lines are blank or repeated.")
        evidence_lines[number] = item.result
    return ProductLineReadFacts(
        source_lines=source_lines,
        supplier_match=evidence.partner_match,
        evidence_lines=evidence_lines,
        evidence_product_mode=evidence.operating_expense_match is None and evidence.fixed_asset_accounting is None,
    )


def _operator_guidance_facts(
    review: ReviewItem,
    company_id: int,
    *,
    purpose_repository: SqlAlchemyReviewPurchasePurposeResolutionRepository,
    accounting_repository: SqlAlchemyReviewAccountingResolutionRepository,
    eligible_asset_account_ids: tuple[int, ...],
    label_reader: FixedAssetAccountingReader | None = None,
    source_reader: SqlAlchemyReviewSourceInvoiceEvidenceReader | None = None,
    product_mapping_enabled: bool = False,
    execution_evidence_reader: SqlAlchemyReviewRepository | None = None,
    product_name_reader: SelectedProductReader | None = None,
) -> OperatorGuidanceFacts:
    """ADR-0013 guidance facts, read from the same private read-only session."""

    purposes = purpose_repository.list_purchase_purpose_resolutions(review_id=review.review_id, company_id=company_id)
    current = next((item for item in purposes if item.review_version == review.version), None)
    resolution = accounting_repository.find_latest_accounting_resolution(
        review_id=review.review_id, company_id=company_id
    )
    return OperatorGuidanceFacts(
        current_purchase_purpose=current.purchase_purpose if current is not None else None,
        latest_purchase_purpose=purposes[-1].purchase_purpose if purposes else None,
        latest_accounting_resolution=resolution,
        accounting_labels=(
            resolve_accounting_labels(resolution, label_reader)
            if resolution is not None and label_reader is not None
            else None
        ),
        eligible_asset_account_ids=eligible_asset_account_ids,
        product_mapping_enabled=product_mapping_enabled,
        unmatched_product_lines=(
            _unmatched_product_lines(review, company_id, source_reader)
            if product_mapping_enabled and source_reader is not None
            else ()
        ),
        resolved_product_lines=(
            _resolved_product_lines(review, company_id, source_reader, execution_evidence_reader, product_name_reader)
            if product_mapping_enabled
            and source_reader is not None
            and execution_evidence_reader is not None
            and product_name_reader is not None
            else ()
        ),
    )


def _unmatched_product_lines(
    review: ReviewItem, company_id: int, source_reader: SqlAlchemyReviewSourceInvoiceEvidenceReader
) -> tuple[UnmatchedProductLine, ...]:
    wanted = {
        (reason.line_number or "").strip()
        for reason in review.review_reasons
        if reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND and (reason.line_number or "").strip()
    }
    if not wanted:
        return ()
    try:
        source = source_reader.get(review_id=review.review_id, company_id=company_id)
    except ApplicationError as exc:
        # Presentation only: without the line list the guidance stays at "Teknik Destek Gerekli".
        logging.getLogger(__name__).warning(
            "workbench.operator_guidance.product_lines_unavailable",
            extra={"review_id": review.review_id, "error": getattr(exc, "safe_message", None) or str(exc)},
        )
        return ()
    return tuple(
        UnmatchedProductLine(
            line_number=(line.line_number or "").strip(),
            seller_item_code=normalize_seller_item_code(line.seller_item_code),
            description=(line.description or "").strip() or None,
            quantity=format(line.quantity.normalize(), "f") if line.quantity is not None else None,
            unit_code=(line.unit_code or "").strip() or None,
        )
        for line in source.invoice.lines
        if (line.line_number or "").strip() in wanted
    )


def _resolved_product_lines(
    review: ReviewItem,
    company_id: int,
    source_reader: SqlAlchemyReviewSourceInvoiceEvidenceReader,
    execution_evidence_reader: SqlAlchemyReviewRepository,
    product_name_reader: SelectedProductReader,
) -> tuple[ResolvedProductLine, ...]:
    """Lines the *current* version's Stage-1 execution evidence matches to a product.

    That evidence is what reclassification just computed (product matching under the
    review's effective supplier) and what a decision would execute. No evidence for the
    current version -> nothing is shown; nothing historical is inferred.
    """

    log = logging.getLogger(__name__)
    try:
        evidence = execution_evidence_reader.get_review_execution_evidence(
            review_id=review.review_id, company_id=company_id, review_version=review.version
        )
        source = source_reader.get(review_id=review.review_id, company_id=company_id)
    except ApplicationError as exc:
        if not isinstance(exc, ReviewNotFoundError):
            log.warning(
                "workbench.operator_guidance.resolved_product_lines_unavailable",
                extra={"review_id": review.review_id, "error": getattr(exc, "safe_message", None) or str(exc)},
            )
        return ()
    unresolved = {
        (reason.line_number or "").strip()
        for reason in review.review_reasons
        if reason.code is ManualReviewReasonCode.PRODUCT_NOT_FOUND
    }
    matched = {
        (item.line_number or "").strip(): item.result.product_id
        for item in evidence.product_match.line_results
        if item.result.status is ProductMatchStatus.MATCHED
        and type(item.result.product_id) is int
        and (item.line_number or "").strip() not in unresolved
    }
    if not matched:
        return ()
    names: dict[int, str] = {}
    try:
        names = {
            product.id: product.name
            for product in product_name_reader.find_products_by_ids(tuple(dict.fromkeys(matched.values())))
            if product.name
        }
    except ApplicationError as exc:
        # Presentation only: an unreadable name degrades to "okunamadı", never fails the projection.
        log.warning(
            "workbench.operator_guidance.product_label_unavailable",
            extra={"review_id": review.review_id, "error": getattr(exc, "safe_message", None) or str(exc)},
        )
    return tuple(
        ResolvedProductLine(
            line_number=number,
            seller_item_code=normalize_seller_item_code(line.seller_item_code),
            description=(line.description or "").strip() or None,
            product_name=names.get(matched[number]),
        )
        for line in source.invoice.lines
        if (number := (line.line_number or "").strip()) in matched
    )


def _product_mapping_requests_enabled(settings: Settings) -> bool:
    if not settings.odoo_workbench_operator_requests_enabled:
        return False
    try:
        return OdooOperatorRequestFieldMapping.from_environment().product_mapping_enabled
    except WorkbenchContractError:
        return False


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


def build_supplier_partner_reader(
    *,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> OdooPartnerRepository:
    """Read-only partner reader that proves an accepted supplier resolution's partner (PR A)."""

    return OdooPartnerRepository(
        adapter=OdooReadOnlyAdapter(client=odoo_client or OdooJson2Client.from_settings(settings))
    )


def build_effective_supplier_resolver(
    *,
    session: Session,
    settings: Settings,
    odoo_client: OdooJson2Client | None = None,
) -> EffectiveSupplierResolver:
    """The effective-supplier resolver for product-remediation use cases (PR A).

    Uses the same ``PartnerMatchingEngine`` over the same read-only provider as the
    deterministic ``DecisionEngine``, so a use case and its follow-up reclassification
    agree on the supplier by construction.
    """

    resolved_odoo_client = odoo_client or OdooJson2Client.from_settings(settings)
    read_adapter = OdooReadOnlyAdapter(client=resolved_odoo_client)
    provider = build_odoo_read_repository_provider(read_adapter=read_adapter)
    return EffectiveSupplierResolver(
        partner_matcher=PartnerMatchingEngine(provider),
        accepted_supplier_reader=AcceptedSupplierReader(
            effect_reader=SqlAlchemyReviewSupplierRemediationEffectRepository(session),
            partner_reader=OdooPartnerRepository(adapter=read_adapter),
        ),
    )
