from datetime import UTC, datetime

from httpx import AsyncClient

from app.api.dependencies import get_uyumsoft_client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.main import app
from app.schemas.uyumsoft import UyumsoftOperationsResponse
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)


class FakeUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def inspect_wsdl(self) -> UyumsoftOperationsResponse:
        return UyumsoftOperationsResponse(
            status="ok",
            wsdl_url="https://uyumsoft.test/wsdl",
            operations=["SendInvoice", "TestConnection"],
            read_only_operations=["TestConnection"],
        )

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _invoice_response(direction="Inbox", request=request, invoice_id="in-1")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _invoice_response(direction="Outbox", request=request, invoice_id="out-1")


async def test_operations_endpoint_is_mockable(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get("/api/v1/connectors/uyumsoft/operations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["read_only_operations"] == ["TestConnection"]


async def test_operations_endpoint_returns_404_outside_development(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(app_env="production")
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get("/api/v1/connectors/uyumsoft/operations")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_inbox_endpoint_returns_normalized_invoice_list(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get(
            "/api/v1/connectors/uyumsoft/inbox",
            params={
                "from": "2026-07-15T00:00:00+00:00",
                "to": "2026-07-16T00:00:00+00:00",
                "page": "2",
                "page_size": "25",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == "Inbox"
    assert body["page"] == 2
    assert body["page_size"] == 25
    assert body["invoices"][0]["invoice_id"] == "in-1"


async def test_outbox_endpoint_returns_normalized_invoice_list(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeUyumsoftClient()
    try:
        response = await api_client.get(
            "/api/v1/connectors/uyumsoft/outbox",
            params={
                "from": "2026-07-15T00:00:00+00:00",
                "to": "2026-07-16T00:00:00+00:00",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["direction"] == "Outbox"


async def test_invoice_endpoint_rejects_from_after_to(api_client: AsyncClient) -> None:
    response = await api_client.get(
        "/api/v1/connectors/uyumsoft/inbox",
        params={
            "from": "2026-07-17T00:00:00+00:00",
            "to": "2026-07-16T00:00:00+00:00",
        },
    )

    assert response.status_code == 422
    assert "from" in response.json()["detail"]


async def test_invoice_endpoint_rejects_invalid_pagination(api_client: AsyncClient) -> None:
    response = await api_client.get(
        "/api/v1/connectors/uyumsoft/inbox",
        params={
            "from": "2026-07-15T00:00:00+00:00",
            "to": "2026-07-16T00:00:00+00:00",
            "page": "0",
            "page_size": "101",
        },
    )

    assert response.status_code == 422


async def test_invoice_endpoint_rejects_invalid_dates(api_client: AsyncClient) -> None:
    response = await api_client.get(
        "/api/v1/connectors/uyumsoft/inbox",
        params={
            "from": "not-a-date",
            "to": "2026-07-16T00:00:00+00:00",
        },
    )

    assert response.status_code == 422


def _invoice_response(
    *,
    direction: str,
    request: UyumsoftInvoiceListRequest,
    invoice_id: str,
) -> UyumsoftInvoiceListResponse:
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=1,
        invoices=[
            UyumsoftInvoiceSummary(
                invoice_id=invoice_id,
                ettn="ettn-1",
                invoice_date=datetime(2026, 7, 16, tzinfo=UTC),
                direction=direction,
            )
        ],
    )
