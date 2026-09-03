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
from app.application.execution import RunAcceptedDecisionExecutionUseCase, WorkbenchVendorBillExecutionWorkflow
from app.application.quotation import WorkbenchQuotationScenarioEvidenceWorkflow
from app.application.workbench import (
    GetReviewItemUseCase,
    ListReviewQueueUseCase,
    ReviewDecisionWriter,
    ReviewQueueReader,
    SubmitReviewDecisionUseCase,
    WorkbenchDecisionIngestionWorkflow,
)
from app.composition import (
    build_odoo_workbench_decision_ingestion_workflow,
    build_uyumsoft_canonical_invoice_importer,
    build_vendor_bill_execution_use_case,
    build_workbench_quotation_scenario_evidence_workflow,
    build_workbench_vendor_bill_execution_workflow,
)
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.persistence.review_billing_evidence_reader import SqlAlchemyReviewBillingEvidenceReader
from app.persistence.review_execution_evidence_reader import SqlAlchemyReviewExecutionEvidenceReader
from app.persistence.workbench_review_repository import SqlAlchemyReviewRepository
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


def get_review_item_use_case(reader: ReviewQueueReaderDep) -> GetReviewItemUseCase:
    return GetReviewItemUseCase(review_queue_reader=reader)


def get_submit_review_decision_use_case(
    writer: ReviewDecisionWriterDep,
    session: DbSessionDep,
) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(
        review_decision_writer=writer,
        execution_evidence_reader=SqlAlchemyReviewExecutionEvidenceReader(session),
        billing_evidence_reader=SqlAlchemyReviewBillingEvidenceReader(session),
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
