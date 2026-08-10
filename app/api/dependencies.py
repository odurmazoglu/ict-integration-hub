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
from app.application.execution import RunAcceptedDecisionExecutionUseCase
from app.application.workbench import (
    GetReviewItemUseCase,
    ListReviewQueueUseCase,
    ReviewDecisionWriter,
    ReviewQueueReader,
    SubmitReviewDecisionUseCase,
)
from app.composition import build_vendor_bill_execution_use_case
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.persistence.workbench_review_repository import SqlAlchemyReviewRepository
from app.services.document_storage import DocumentStorage, LocalDocumentStorage


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


def get_submit_review_decision_use_case(writer: ReviewDecisionWriterDep) -> SubmitReviewDecisionUseCase:
    return SubmitReviewDecisionUseCase(review_decision_writer=writer)


ListReviewQueueUseCaseDep = Annotated[ListReviewQueueUseCase, Depends(get_list_review_queue_use_case)]
GetReviewItemUseCaseDep = Annotated[GetReviewItemUseCase, Depends(get_review_item_use_case)]
SubmitReviewDecisionUseCaseDep = Annotated[
    SubmitReviewDecisionUseCase,
    Depends(get_submit_review_decision_use_case),
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
