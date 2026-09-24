from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.security import (
    DevelopmentHeaderRequestContextResolver,
    DisabledRequestContextResolver,
    OidcJwtRequestContextResolver,
    RequestContext,
    RequestContextResolver,
    RequestMetadata,
)
from app.application.execution import (
    RunAcceptedDecisionExecutionUseCase,
    WorkbenchAcceptedDecisionExecutionDispatcher,
    WorkbenchVendorBillExecutionWorkflow,
)
from app.application.execution.vendor_bill_preview import (
    PreviewVendorBillRequest,
    PreviewVendorBillUseCase,
    VendorBillPreview,
)
from app.application.quotation import WorkbenchQuotationScenarioEvidenceWorkflow
from app.application.workbench import (
    CreateNewProductUseCase,
    GetReviewItemUseCase,
    ListReviewQueueUseCase,
    ResolveWorkbenchSupplierUseCase,
    ReviewDecisionWriter,
    ReviewQueueReader,
    SubmitReviewDecisionUseCase,
    WorkbenchDecisionIngestionWorkflow,
)
from app.application.workbench.accounting_resolution import (
    ReviewAccountingResolutionSubmissionResult,
    SubmitReviewAccountingResolutionCommand,
)
from app.application.workbench.accounting_resolution_use_cases import SubmitReviewAccountingResolutionUseCase
from app.application.workbench.execution_evidence_recovery import (
    RebuildExecutionEvidenceCommand,
    RebuildExecutionEvidenceResult,
)
from app.application.workbench.execution_evidence_recovery_use_cases import RebuildReviewExecutionEvidenceUseCase
from app.application.workbench.execution_status_use_cases import GetWorkbenchExecutionStatusUseCase
from app.application.workbench.expense_account_lookup import (
    ExpenseAccountCandidate,
    ListExpenseAccountCandidatesQuery,
)
from app.application.workbench.expense_account_use_cases import ListExpenseAccountCandidatesUseCase
from app.application.workbench.operating_expense_mapping_command import (
    OperatingExpenseMappingSubmissionResult,
    SubmitOperatingExpenseMappingCommand,
)
from app.application.workbench.operating_expense_mapping_use_cases import SubmitOperatingExpenseMappingUseCase
from app.application.workbench.product_remediation import CreateNewProductCommand, CreateNewProductResult
from app.application.workbench.purchase_purpose import (
    PurchasePurposeSubmissionResult,
    SubmitPurchasePurposeCommand,
)
from app.application.workbench.purchase_purpose_use_cases import SubmitPurchasePurposeUseCase
from app.application.workbench.retirement_recovery import (
    GetOneOffVendorRetirementUseCase,
    RecoverOneOffVendorRetirementWorkflow,
)
from app.application.workbench.review_evidence import ReviewEvidenceReader
from app.application.workbench.supplier_remediation import (
    ResolveWorkbenchSupplierCommand,
    SupplierRemediationResult,
)
from app.application.workbench.vendor_bill_readback import GetVendorBillReadbackUseCase
from app.application.workbench.write_authorization_use_cases import (
    CreateWriteAuthorizationUseCase,
    ListWriteAuthorizationsUseCase,
    RevokeWriteAuthorizationUseCase,
)
from app.composition import (
    build_create_new_product_use_case,
    build_get_vendor_bill_readback_use_case,
    build_get_workbench_execution_status_use_case,
    build_odoo_workbench_decision_ingestion_workflow,
    build_resolve_workbench_supplier_use_case,
    build_uyumsoft_canonical_invoice_importer,
    build_vendor_bill_execution_use_case,
    build_vendor_bill_preview_use_case,
    build_workbench_accepted_decision_execution_dispatcher,
    build_workbench_quotation_scenario_evidence_workflow,
    build_workbench_vendor_bill_execution_workflow,
)
from app.composition.operating_expense_mapping import (
    build_list_expense_account_candidates_use_case,
    build_submit_operating_expense_mapping_use_case,
)
from app.composition.purchase_purpose_and_accounting_resolution import (
    build_rebuild_review_execution_evidence_use_case,
    build_submit_purchase_purpose_use_case,
    build_submit_review_accounting_resolution_use_case,
)
from app.composition.supplier_remediation import (
    build_get_one_off_vendor_retirement_use_case,
    build_recover_one_off_vendor_retirement_workflow,
)
from app.composition.write_authorization import (
    build_create_write_authorization_use_case,
    build_list_write_authorizations_use_case,
    build_revoke_write_authorization_use_case,
)
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.erp.odoo.adapter import OdooReadOnlyAdapter
from app.erp.odoo.partner_repository import OdooPartnerRepository
from app.erp.odoo.product_repository import OdooProductRepository
from app.erp.odoo.selected_expense_account_reader import OdooSelectedAccountReader
from app.erp.odoo.selected_product_reader import OdooSelectedProductReader
from app.persistence.review_billing_evidence_reader import SqlAlchemyReviewBillingEvidenceReader
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.persistence.unit_of_work import SqlAlchemyUnitOfWork
from app.persistence.workbench_review_repository import SqlAlchemyReviewRepository
from app.persistence.workbench_review_source_invoice_reader import SqlAlchemyReviewSourceInvoiceEvidenceReader
from app.services.document_storage import DocumentStorage, LocalDocumentStorage
from app.services.uyumsoft_canonical_import import UyumsoftCanonicalInvoiceImporter


def get_db_session() -> Generator:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSessionDep = Annotated[Session, Depends(get_db_session)]


def get_request_context_resolver(settings: SettingsDep) -> RequestContextResolver:
    if settings.ipp_auth_mode == "oidc_jwt":
        return OidcJwtRequestContextResolver(settings)
    if settings.ipp_auth_mode == "development_headers":
        return DevelopmentHeaderRequestContextResolver(settings)
    return DisabledRequestContextResolver()


RequestContextResolverDep = Annotated[RequestContextResolver, Depends(get_request_context_resolver)]


def get_request_context(
    request: Request,
    resolver: RequestContextResolverDep,
) -> RequestContext:
    return resolver.resolve(RequestMetadata(headers=request.headers))


RequestContextDep = Annotated[RequestContext, Depends(get_request_context)]


def get_odoo_client(settings: SettingsDep) -> OdooJson2Client:
    return OdooJson2Client.from_settings(settings)


def get_uyumsoft_client(settings: SettingsDep) -> UyumsoftSoapClient:
    return UyumsoftSoapClient.from_settings(settings)


def get_document_storage(settings: SettingsDep) -> DocumentStorage:
    return LocalDocumentStorage(settings.document_storage_root)


OdooClientDep = Annotated[OdooJson2Client, Depends(get_odoo_client)]
UyumsoftClientDep = Annotated[UyumsoftSoapClient, Depends(get_uyumsoft_client)]
DocumentStorageDep = Annotated[DocumentStorage, Depends(get_document_storage)]


def get_uyumsoft_canonical_importer(
    session: DbSessionDep,
    settings: SettingsDep,
    client: UyumsoftClientDep,
    storage: DocumentStorageDep,
    odoo_client: OdooClientDep,
) -> UyumsoftCanonicalInvoiceImporter:
    return build_uyumsoft_canonical_invoice_importer(
        session=session,
        settings=settings,
        uyumsoft_client=client,
        storage=storage,
        odoo_client=odoo_client,
    )


UyumsoftCanonicalImporterDep = Annotated[
    UyumsoftCanonicalInvoiceImporter,
    Depends(get_uyumsoft_canonical_importer),
]


def get_review_repository(session: DbSessionDep) -> SqlAlchemyReviewRepository:
    return SqlAlchemyReviewRepository(session)


ReviewRepositoryDep = Annotated[SqlAlchemyReviewRepository, Depends(get_review_repository)]


def get_review_queue_reader(repository: ReviewRepositoryDep) -> ReviewQueueReader:
    return repository


def get_review_decision_writer(repository: ReviewRepositoryDep) -> ReviewDecisionWriter:
    return repository


ReviewQueueReaderDep = Annotated[ReviewQueueReader, Depends(get_review_queue_reader)]
ReviewDecisionWriterDep = Annotated[ReviewDecisionWriter, Depends(get_review_decision_writer)]


def get_list_review_queue_use_case(reader: ReviewQueueReaderDep) -> ListReviewQueueUseCase:
    return ListReviewQueueUseCase(review_queue_reader=reader)


def get_review_evidence_reader(session: DbSessionDep, odoo_client: OdooClientDep) -> ReviewEvidenceReader:
    adapter = OdooReadOnlyAdapter(client=odoo_client)
    return ReviewEvidenceReader(
        source_reader=SqlAlchemyReviewSourceInvoiceEvidenceReader(session),
        execution_reader=SqlAlchemyReviewRepository(session),
        partner_repository=OdooPartnerRepository(adapter=adapter),
    )


ReviewEvidenceReaderDep = Annotated[ReviewEvidenceReader, Depends(get_review_evidence_reader)]


def get_review_item_use_case(
    reader: ReviewQueueReaderDep,
    evidence_reader: ReviewEvidenceReaderDep,
) -> GetReviewItemUseCase:
    return GetReviewItemUseCase(review_queue_reader=reader, evidence_reader=evidence_reader)


def get_submit_review_decision_use_case(
    writer: ReviewDecisionWriterDep,
    session: DbSessionDep,
    odoo_client: OdooClientDep,
) -> SubmitReviewDecisionUseCase:
    read_adapter = OdooReadOnlyAdapter(client=odoo_client)
    return SubmitReviewDecisionUseCase(
        review_decision_writer=writer,
        unit_of_work=SqlAlchemyUnitOfWork(session),
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        billing_evidence_reader=SqlAlchemyReviewBillingEvidenceReader(session),
        selected_product_reader=OdooSelectedProductReader(
            product_repository=OdooProductRepository(adapter=read_adapter),
        ),
        selected_account_reader=OdooSelectedAccountReader(adapter=read_adapter),
    )


ListReviewQueueUseCaseDep = Annotated[ListReviewQueueUseCase, Depends(get_list_review_queue_use_case)]
GetReviewItemUseCaseDep = Annotated[GetReviewItemUseCase, Depends(get_review_item_use_case)]
SubmitReviewDecisionUseCaseDep = Annotated[
    SubmitReviewDecisionUseCase,
    Depends(get_submit_review_decision_use_case),
]


def get_workbench_decision_ingestion_workflow(
    session: DbSessionDep,
    settings: SettingsDep,
) -> WorkbenchDecisionIngestionWorkflow:
    return _LazyWorkbenchDecisionIngestionWorkflow(session=session, settings=settings)


class _LazyWorkbenchDecisionIngestionWorkflow:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._workflow: WorkbenchDecisionIngestionWorkflow | None = None

    def sync_ready_decisions(
        self,
        *,
        company_id: int,
        limit: int = 50,
        trace_id: str | None = None,
    ):
        return self._get_workflow().sync_ready_decisions(company_id=company_id, limit=limit, trace_id=trace_id)

    def _get_workflow(self) -> WorkbenchDecisionIngestionWorkflow:
        if self._workflow is None:
            self._workflow = build_odoo_workbench_decision_ingestion_workflow(
                session=self._session,
                settings=self._settings,
            )
        return self._workflow


WorkbenchDecisionIngestionWorkflowDep = Annotated[
    WorkbenchDecisionIngestionWorkflow,
    Depends(get_workbench_decision_ingestion_workflow),
]


def get_vendor_bill_execution_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> RunAcceptedDecisionExecutionUseCase:
    return build_vendor_bill_execution_use_case(session=session, settings=settings)


VendorBillExecutionUseCaseDep = Annotated[
    RunAcceptedDecisionExecutionUseCase,
    Depends(get_vendor_bill_execution_use_case),
]


def get_workbench_vendor_bill_execution_workflow(
    session: DbSessionDep,
    settings: SettingsDep,
) -> WorkbenchVendorBillExecutionWorkflow:
    return _LazyWorkbenchVendorBillExecutionWorkflow(session=session, settings=settings)


class _LazyWorkbenchVendorBillExecutionWorkflow:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._workflow: WorkbenchVendorBillExecutionWorkflow | None = None

    def execute(self, **kwargs):
        return self._get_workflow().execute(**kwargs)

    def _get_workflow(self) -> WorkbenchVendorBillExecutionWorkflow:
        if self._workflow is None:
            self._workflow = build_workbench_vendor_bill_execution_workflow(
                session=self._session,
                settings=self._settings,
            )
        return self._workflow


WorkbenchVendorBillExecutionWorkflowDep = Annotated[
    WorkbenchVendorBillExecutionWorkflow,
    Depends(get_workbench_vendor_bill_execution_workflow),
]


def get_workbench_accepted_decision_execution_dispatcher(
    session: DbSessionDep,
    settings: SettingsDep,
) -> WorkbenchAcceptedDecisionExecutionDispatcher:
    return _LazyWorkbenchAcceptedDecisionExecutionDispatcher(session=session, settings=settings)


class _LazyWorkbenchAcceptedDecisionExecutionDispatcher:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._dispatcher: WorkbenchAcceptedDecisionExecutionDispatcher | None = None

    def execute(self, **kwargs):
        return self._get_dispatcher().execute(**kwargs)

    def _get_dispatcher(self) -> WorkbenchAcceptedDecisionExecutionDispatcher:
        if self._dispatcher is None:
            self._dispatcher = build_workbench_accepted_decision_execution_dispatcher(
                session=self._session,
                settings=self._settings,
            )
        return self._dispatcher


WorkbenchAcceptedDecisionExecutionDispatcherDep = Annotated[
    WorkbenchAcceptedDecisionExecutionDispatcher,
    Depends(get_workbench_accepted_decision_execution_dispatcher),
]


def get_workbench_quotation_scenario_evidence_workflow(
    session: DbSessionDep,
    settings: SettingsDep,
) -> WorkbenchQuotationScenarioEvidenceWorkflow:
    return _LazyWorkbenchQuotationScenarioEvidenceWorkflow(session=session, settings=settings)


class _LazyWorkbenchQuotationScenarioEvidenceWorkflow:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._workflow: WorkbenchQuotationScenarioEvidenceWorkflow | None = None

    def capture(self, **kwargs):
        return self._get_workflow().capture(**kwargs)

    def _get_workflow(self) -> WorkbenchQuotationScenarioEvidenceWorkflow:
        if self._workflow is None:
            self._workflow = build_workbench_quotation_scenario_evidence_workflow(
                session=self._session,
                settings=self._settings,
            )
        return self._workflow


WorkbenchQuotationScenarioEvidenceWorkflowDep = Annotated[
    WorkbenchQuotationScenarioEvidenceWorkflow,
    Depends(get_workbench_quotation_scenario_evidence_workflow),
]


def get_resolve_workbench_supplier_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> ResolveWorkbenchSupplierUseCase:
    return _LazyResolveWorkbenchSupplierUseCase(session=session, settings=settings)


class _LazyResolveWorkbenchSupplierUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: ResolveWorkbenchSupplierUseCase | None = None

    async def execute(self, command: ResolveWorkbenchSupplierCommand) -> SupplierRemediationResult:
        return await self._get_use_case().execute(command)

    def _get_use_case(self) -> ResolveWorkbenchSupplierUseCase:
        if self._use_case is None:
            self._use_case = build_resolve_workbench_supplier_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


ResolveWorkbenchSupplierUseCaseDep = Annotated[
    ResolveWorkbenchSupplierUseCase,
    Depends(get_resolve_workbench_supplier_use_case),
]


def get_list_expense_account_candidates_use_case(
    settings: SettingsDep,
) -> ListExpenseAccountCandidatesUseCase:
    return _LazyListExpenseAccountCandidatesUseCase(settings=settings)


class _LazyListExpenseAccountCandidatesUseCase:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._use_case: ListExpenseAccountCandidatesUseCase | None = None

    def execute(self, query: ListExpenseAccountCandidatesQuery) -> tuple[ExpenseAccountCandidate, ...]:
        return self._get_use_case().execute(query)

    def _get_use_case(self) -> ListExpenseAccountCandidatesUseCase:
        if self._use_case is None:
            self._use_case = build_list_expense_account_candidates_use_case(settings=self._settings)
        return self._use_case


ListExpenseAccountCandidatesUseCaseDep = Annotated[
    ListExpenseAccountCandidatesUseCase,
    Depends(get_list_expense_account_candidates_use_case),
]


def get_submit_operating_expense_mapping_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> SubmitOperatingExpenseMappingUseCase:
    return _LazySubmitOperatingExpenseMappingUseCase(session=session, settings=settings)


class _LazySubmitOperatingExpenseMappingUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: SubmitOperatingExpenseMappingUseCase | None = None

    async def execute(self, command: SubmitOperatingExpenseMappingCommand) -> OperatingExpenseMappingSubmissionResult:
        return await self._get_use_case().execute(command)

    def _get_use_case(self) -> SubmitOperatingExpenseMappingUseCase:
        if self._use_case is None:
            self._use_case = build_submit_operating_expense_mapping_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


SubmitOperatingExpenseMappingUseCaseDep = Annotated[
    SubmitOperatingExpenseMappingUseCase,
    Depends(get_submit_operating_expense_mapping_use_case),
]


def get_submit_purchase_purpose_use_case(
    session: DbSessionDep,
) -> SubmitPurchasePurposeUseCase:
    return _LazySubmitPurchasePurposeUseCase(session=session)


class _LazySubmitPurchasePurposeUseCase:
    def __init__(self, *, session: Session) -> None:
        self._session = session
        self._use_case: SubmitPurchasePurposeUseCase | None = None

    def execute(self, command: SubmitPurchasePurposeCommand) -> PurchasePurposeSubmissionResult:
        return self._get_use_case().execute(command)

    def _get_use_case(self) -> SubmitPurchasePurposeUseCase:
        if self._use_case is None:
            self._use_case = build_submit_purchase_purpose_use_case(session=self._session)
        return self._use_case


SubmitPurchasePurposeUseCaseDep = Annotated[
    SubmitPurchasePurposeUseCase,
    Depends(get_submit_purchase_purpose_use_case),
]


def get_submit_review_accounting_resolution_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> SubmitReviewAccountingResolutionUseCase:
    return _LazySubmitReviewAccountingResolutionUseCase(session=session, settings=settings)


class _LazySubmitReviewAccountingResolutionUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: SubmitReviewAccountingResolutionUseCase | None = None

    async def execute(
        self, command: SubmitReviewAccountingResolutionCommand
    ) -> ReviewAccountingResolutionSubmissionResult:
        return await self._get_use_case().execute(command)

    def _get_use_case(self) -> SubmitReviewAccountingResolutionUseCase:
        if self._use_case is None:
            self._use_case = build_submit_review_accounting_resolution_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


SubmitReviewAccountingResolutionUseCaseDep = Annotated[
    SubmitReviewAccountingResolutionUseCase,
    Depends(get_submit_review_accounting_resolution_use_case),
]


def get_rebuild_review_execution_evidence_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> RebuildReviewExecutionEvidenceUseCase:
    return _LazyRebuildReviewExecutionEvidenceUseCase(session=session, settings=settings)


class _LazyRebuildReviewExecutionEvidenceUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: RebuildReviewExecutionEvidenceUseCase | None = None

    async def execute(self, command: RebuildExecutionEvidenceCommand) -> RebuildExecutionEvidenceResult:
        return await self._get_use_case().execute(command)

    def _get_use_case(self) -> RebuildReviewExecutionEvidenceUseCase:
        if self._use_case is None:
            self._use_case = build_rebuild_review_execution_evidence_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


RebuildReviewExecutionEvidenceUseCaseDep = Annotated[
    RebuildReviewExecutionEvidenceUseCase,
    Depends(get_rebuild_review_execution_evidence_use_case),
]


def get_create_new_product_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> CreateNewProductUseCase:
    return _LazyCreateNewProductUseCase(session=session, settings=settings)


class _LazyCreateNewProductUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: CreateNewProductUseCase | None = None

    async def execute(self, command: CreateNewProductCommand) -> CreateNewProductResult:
        return await self._get_use_case().execute(command)

    def _get_use_case(self) -> CreateNewProductUseCase:
        if self._use_case is None:
            self._use_case = build_create_new_product_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


CreateNewProductUseCaseDep = Annotated[
    CreateNewProductUseCase,
    Depends(get_create_new_product_use_case),
]


def get_vendor_bill_preview_use_case(
    session: DbSessionDep,
    settings: SettingsDep,
) -> PreviewVendorBillUseCase:
    return _LazyPreviewVendorBillUseCase(session=session, settings=settings)


class _LazyPreviewVendorBillUseCase:
    def __init__(self, *, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._use_case: PreviewVendorBillUseCase | None = None

    def preview(self, request: PreviewVendorBillRequest) -> VendorBillPreview:
        return self._get_use_case().preview(request)

    def _get_use_case(self) -> PreviewVendorBillUseCase:
        if self._use_case is None:
            self._use_case = build_vendor_bill_preview_use_case(
                session=self._session,
                settings=self._settings,
            )
        return self._use_case


VendorBillPreviewUseCaseDep = Annotated[
    PreviewVendorBillUseCase,
    Depends(get_vendor_bill_preview_use_case),
]


def get_workbench_execution_status_use_case(session: DbSessionDep) -> GetWorkbenchExecutionStatusUseCase:
    return build_get_workbench_execution_status_use_case(session=session)


WorkbenchExecutionStatusUseCaseDep = Annotated[
    GetWorkbenchExecutionStatusUseCase,
    Depends(get_workbench_execution_status_use_case),
]


def get_vendor_bill_readback_use_case(session: DbSessionDep, settings: SettingsDep) -> GetVendorBillReadbackUseCase:
    return build_get_vendor_bill_readback_use_case(session=session, settings=settings)


VendorBillReadbackUseCaseDep = Annotated[
    GetVendorBillReadbackUseCase,
    Depends(get_vendor_bill_readback_use_case),
]


# Narrow authorization metadata operations use Hub persistence only; no ERP client.
def get_create_write_authorization_use_case(session: DbSessionDep) -> CreateWriteAuthorizationUseCase:
    return build_create_write_authorization_use_case(session=session)


def get_list_write_authorizations_use_case(session: DbSessionDep) -> ListWriteAuthorizationsUseCase:
    return build_list_write_authorizations_use_case(session=session)


def get_revoke_write_authorization_use_case(session: DbSessionDep) -> RevokeWriteAuthorizationUseCase:
    return build_revoke_write_authorization_use_case(session=session)


CreateWriteAuthorizationUseCaseDep = Annotated[
    CreateWriteAuthorizationUseCase,
    Depends(get_create_write_authorization_use_case),
]
ListWriteAuthorizationsUseCaseDep = Annotated[
    ListWriteAuthorizationsUseCase,
    Depends(get_list_write_authorizations_use_case),
]
RevokeWriteAuthorizationUseCaseDep = Annotated[
    RevokeWriteAuthorizationUseCase,
    Depends(get_revoke_write_authorization_use_case),
]


def get_one_off_vendor_retirement_use_case(session: DbSessionDep) -> GetOneOffVendorRetirementUseCase:
    return build_get_one_off_vendor_retirement_use_case(session=session)


def get_recover_one_off_vendor_retirement_workflow(
    session: DbSessionDep, settings: SettingsDep
) -> RecoverOneOffVendorRetirementWorkflow:
    return build_recover_one_off_vendor_retirement_workflow(session=session, settings=settings)


OneOffVendorRetirementUseCaseDep = Annotated[
    GetOneOffVendorRetirementUseCase, Depends(get_one_off_vendor_retirement_use_case)
]
RecoverOneOffVendorRetirementWorkflowDep = Annotated[
    RecoverOneOffVendorRetirementWorkflow, Depends(get_recover_one_off_vendor_retirement_workflow)
]
