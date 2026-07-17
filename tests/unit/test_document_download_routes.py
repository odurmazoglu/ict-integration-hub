from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db_session, get_document_storage, get_uyumsoft_client
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.main import app
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.services.document_storage import LocalDocumentStorage


class FakeDocumentUyumsoftClient(UyumsoftSoapClient):
    def __init__(self) -> None:
        pass

    def download_invoice_ubl_xml(self, *, direction: str, invoice_id: str) -> bytes:
        return b"<Invoice><ID>route</ID></Invoice>"


async def test_document_download_requires_read_only_confirmation(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/v1/documents/uyumsoft/invoices/download",
        json={"invoice_ids": [1], "document_type": "UBL_XML"},
    )

    assert response.status_code == 422
    assert "confirm_read_only" in response.json()["detail"]


async def test_document_download_rejects_unbounded_batch(api_client: AsyncClient) -> None:
    response = await api_client.post(
        "/api/v1/documents/uyumsoft/invoices/download",
        json={"invoice_ids": list(range(1, 22)), "document_type": "UBL_XML", "confirm_read_only": True},
    )

    assert response.status_code == 422


async def test_document_download_persists_document_metadata_and_file(
    api_client: AsyncClient,
    tmp_path: Path,
) -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        invoice = _invoice(session)
        session.commit()
        invoice_id = invoice.id

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_environment="test")
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeDocumentUyumsoftClient()
    app.dependency_overrides[get_document_storage] = lambda: LocalDocumentStorage(tmp_path)
    try:
        response = await api_client.post(
            "/api/v1/documents/uyumsoft/invoices/download",
            json={"invoice_ids": [invoice_id], "document_type": "UBL_XML", "confirm_read_only": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["downloaded"] == 1
    assert body["existing"] == 0
    assert body["items"][0]["invoice_id"] == invoice_id
    assert body["items"][0]["document_type"] == "UBL_XML"
    with session_factory() as session:
        document = session.scalar(select(InvoiceDocument))
    assert document is not None
    assert (tmp_path / document.storage_key).read_bytes() == b"<Invoice><ID>route</ID></Invoice>"


async def test_document_download_returns_not_found_for_unknown_invoice(
    api_client: AsyncClient,
    tmp_path: Path,
) -> None:
    session_factory = _session_factory()

    def db_override() -> Generator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_settings] = lambda: Settings(uyumsoft_environment="test")
    app.dependency_overrides[get_db_session] = db_override
    app.dependency_overrides[get_uyumsoft_client] = lambda: FakeDocumentUyumsoftClient()
    app.dependency_overrides[get_document_storage] = lambda: LocalDocumentStorage(tmp_path)
    try:
        response = await api_client.post(
            "/api/v1/documents/uyumsoft/invoices/download",
            json={"invoice_ids": [999], "document_type": "UBL_XML", "confirm_read_only": True},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _invoice(session: Session) -> UyumsoftInvoiceMetadata:
    invoice = UyumsoftInvoiceMetadata(
        provider="uyumsoft",
        direction="Inbox",
        provider_invoice_id="in-route",
        ettn="route-ettn",
        identity_key="ettn:route-ettn",
        identity_strategy="ettn",
        invoice_number="route-number",
        invoice_date=datetime(2026, 7, 17, tzinfo=UTC),
        raw_metadata={},
        first_seen_at=datetime(2026, 7, 17, tzinfo=UTC),
        last_seen_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    session.add(invoice)
    session.flush()
    return invoice
