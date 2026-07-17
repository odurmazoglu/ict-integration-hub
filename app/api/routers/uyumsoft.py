from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.api.dependencies import SettingsDep, UyumsoftClientDep

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError

from app.schemas.uyumsoft import (
    UyumsoftIdentityResponse,
    UyumsoftOperationsResponse,
    UyumsoftSystemDateResponse,
    UyumsoftTestConnectionResponse,
)
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceListRequest, UyumsoftInvoiceListResponse

router = APIRouter(prefix="/api/v1/connectors/uyumsoft", tags=["uyumsoft"])
InvoiceFromQuery = Annotated[datetime, Query(alias="from", description="Inclusive invoice start date/time.")]
InvoiceToQuery = Annotated[datetime, Query(alias="to", description="Inclusive invoice end date/time.")]
InvoicePageQuery = Annotated[int, Query(ge=1, description="One-based page number.")]
InvoicePageSizeQuery = Annotated[int, Query(ge=1, le=100, description="Number of invoices per page, max 100.")]


@router.get("/test-connection", response_model=UyumsoftTestConnectionResponse)
def test_connection(client: UyumsoftClientDep) -> UyumsoftTestConnectionResponse:
    try:
        return client.test_connection()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/identity", response_model=UyumsoftIdentityResponse)
def identity(client: UyumsoftClientDep) -> UyumsoftIdentityResponse:
    try:
        return client.who_am_i()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/system-date", response_model=UyumsoftSystemDateResponse)
def system_date(client: UyumsoftClientDep) -> UyumsoftSystemDateResponse:
    try:
        return client.get_system_date()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/operations", response_model=UyumsoftOperationsResponse)
def operations(
    settings: SettingsDep,
    client: UyumsoftClientDep,
) -> UyumsoftOperationsResponse:
    if not settings.is_development:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WSDL inspection is available only in development.",
        )
    try:
        return client.inspect_wsdl()
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc

@router.get("/inbox", response_model=UyumsoftInvoiceListResponse)
def inbox_invoices(
    client: UyumsoftClientDep,
    from_date: InvoiceFromQuery,
    to_date: InvoiceToQuery,
    page: InvoicePageQuery = 1,
    page_size: InvoicePageSizeQuery = 50,
) -> UyumsoftInvoiceListResponse:
    request = _build_invoice_list_request(from_date=from_date, to_date=to_date, page=page, page_size=page_size)
    try:
        return client.list_inbox_invoices(request)
    except ConnectorTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


@router.get("/outbox", response_model=UyumsoftInvoiceListResponse)
def outbox_invoices(
    client: UyumsoftClientDep,
    from_date: InvoiceFromQuery,
    to_date: InvoiceToQuery,
    page: InvoicePageQuery = 1,
    page_size: InvoicePageSizeQuery = 50,
) -> UyumsoftInvoiceListResponse:
    request = _build_invoice_list_request(from_date=from_date, to_date=to_date, page=page, page_size=page_size)
    try:
        return client.list_outbox_invoices(request)
    except ConnectorTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=exc.safe_message) from exc
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.safe_message) from exc


def _build_invoice_list_request(
    *,
    from_date: datetime,
    to_date: datetime,
    page: int,
    page_size: int,
) -> UyumsoftInvoiceListRequest:
    if from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Query parameter 'from' must be before or equal to query parameter 'to'.",
        )
    return UyumsoftInvoiceListRequest(from_date=from_date, to_date=to_date, page=page, page_size=page_size)

