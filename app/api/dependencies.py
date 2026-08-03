from collections.abc import Generator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.api.security import (
    DevelopmentHeaderRequestContextResolver,
    DisabledRequestContextResolver,
    RequestContext,
    RequestContextResolver,
    RequestMetadata,
)
from app.connectors.odoo.client import OdooJson2Client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
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
    if settings.ipp_enable_development_header_auth:
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
