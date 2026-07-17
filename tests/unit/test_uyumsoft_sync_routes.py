from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_uyumsoft_client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)


class FakeSyncUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Inbox", request, "inbox-ettn")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Outbox", request, "outbox-ettn")


async def test_sync_endpoint_requires_read_only_confirmation(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/v1/sync/uyumsoft/invoices",
        params={
            "from": "2026-07-16T00:00:00+00:00",
            "to": "2026-07-17T00:00:00+00:00",
        },
    )

    assert response.status_code == 422
    assert "confirm_read_only" in response.json()["detail"]


async def test_sync_endpoint_is_unavailable_outside_uyumsoft_test(api_client: AsyncClient) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_environment="production")
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


async def test_sync_endpoint_persists_read_only_summary(api_client: AsyncClient) -> None:
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Both",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["skipped"] == 0
    with session_factory() as session:
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert {record.direction for record in records} == {"Inbox", "Outbox"}


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _response(
    direction: str,
    request: UyumsoftInvoiceListRequest,
    ettn: str,
) -> UyumsoftInvoiceListResponse:
    return UyumsoftInvoiceListResponse(
        direction=direction,
        page=request.page,
        page_size=request.page_size,
        total_count=1,
        invoices=[
            UyumsoftInvoiceSummary(
                invoice_id=f"{direction.lower()}-1",
                ettn=ettn,
                invoice_number=f"{direction}-INV-1",
                invoice_date=datetime(2026, 7, 17, tzinfo=UTC),
                sender="Sender",
                receiver="Receiver",
                tax_number="1234567890",
                currency="TRY",
                total_amount=Decimal("10.00"),
                direction=direction,
                status="NEW",
            )
        ],
    )
