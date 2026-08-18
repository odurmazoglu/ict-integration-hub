from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_uyumsoft_canonical_importer, get_uyumsoft_client
from app.connectors.exceptions import ConnectorError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.models.uyumsoft_sync_run import UyumsoftSyncRun
from app.schemas.uyumsoft_invoices import (
    UyumsoftInvoiceListRequest,
    UyumsoftInvoiceListResponse,
    UyumsoftInvoiceSummary,
)
from app.services.uyumsoft_canonical_import import (
    IMPORT_STATUS_REVIEW_CREATED,
    UyumsoftCanonicalImportBatchResult,
    UyumsoftCanonicalImportOutcome,
)


class FakeSyncUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def list_inbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Inbox", request, "inbox-ettn")

    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        return _response("Outbox", request, "outbox-ettn")


class FailingOutboxSyncUyumsoftClient(FakeSyncUyumsoftClient):
    def list_outbox_invoices(self, request: UyumsoftInvoiceListRequest) -> UyumsoftInvoiceListResponse:
        raise ConnectorError("Outbox transport failed")


class NoopCanonicalImporter:
    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        return UyumsoftCanonicalImportBatchResult()


class RecordingCanonicalImporter:
    def __init__(self) -> None:
        self.calls: list[list[UyumsoftInvoiceSummary]] = []

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, object],
    ) -> UyumsoftCanonicalImportBatchResult:
        self.calls.append(invoices)
        return UyumsoftCanonicalImportBatchResult(
            outcomes=tuple(
                UyumsoftCanonicalImportOutcome(
                    direction=invoice.direction,
                    invoice_identity=invoice.ettn or "missing",
                    status=IMPORT_STATUS_REVIEW_CREATED,
                    company_id=7,
                    import_status="review_required",
                    imported_invoice_id=invoice.ettn,
                    review_id="review-1",
                )
                for invoice in invoices
            )
        )


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
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
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
    assert body["status"] == "completed"
    assert body["run_id"] is not None
    assert body["cursor_state"]["Inbox"]["current_page"] == 1
    assert body["cursor_state"]["Outbox"]["current_page"] == 1
    with session_factory() as session:
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
        sync_run = session.scalar(select(UyumsoftSyncRun))
    assert {record.direction for record in records} == {"Inbox", "Outbox"}
    assert sync_run is not None
    assert sync_run.status == "completed"


async def test_sync_endpoint_records_failed_run_on_connector_error(api_client: AsyncClient) -> None:
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FailingOutboxSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: NoopCanonicalImporter()
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

    assert response.status_code == 502
    with session_factory() as session:
        sync_run = session.scalar(select(UyumsoftSyncRun))
        records = session.scalars(select(UyumsoftInvoiceMetadata)).all()
    assert sync_run is not None
    assert sync_run.status == "failed"
    assert sync_run.cursor_state["Inbox"]["status"] == "completed"
    assert sync_run.cursor_state["Outbox"]["status"] == "failed"
    assert {record.direction for record in records} == {"Inbox"}


async def test_sync_endpoint_reaches_canonical_importer(api_client: AsyncClient) -> None:
    session_factory = _session_factory()
    importer = RecordingCanonicalImporter()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeSyncUyumsoftClient()
    app.dependency_overrides[get_uyumsoft_canonical_importer] = lambda: importer
    try:
        response = await api_client.post(
            "/api/v1/sync/uyumsoft/invoices",
            params={
                "from": "2026-07-16T00:00:00+00:00",
                "to": "2026-07-17T00:00:00+00:00",
                "direction": "Inbox",
                "page_size": "10",
                "max_pages": "1",
                "confirm_read_only": "true",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(importer.calls) == 1
    assert importer.calls[0][0].direction == "Inbox"
    body = response.json()
    assert body["review_count"] == 1
    assert body["directions"][0]["import_outcomes"][0]["status"] == IMPORT_STATUS_REVIEW_CREATED


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
