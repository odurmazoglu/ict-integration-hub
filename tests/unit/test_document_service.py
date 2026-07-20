from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.db.base import Base
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import UyumsoftInvoiceDocument
from app.services import document_service
from app.services.document_service import (
    DocumentConflictError,
    DocumentPersistenceError,
    DocumentValidationError,
    InvoiceDocumentNotFoundError,
    InvoiceDocumentService,
)
from app.services.document_storage import DocumentStorageError, LocalDocumentStorage

INBOX_XML = b'<?xml version="1.0"?><Invoice><ID>IN-1</ID></Invoice>'
OUTBOX_XML = b"<Invoice><ID>OUT-1</ID></Invoice>"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db_session:
        yield db_session


class RecordingDocumentClient(UyumsoftSoapClient):
    def __init__(
        self,
        *,
        content: bytes = INBOX_XML,
        fail_with: Exception | None = None,
    ) -> None:
        self.content = content
        self.fail_with = fail_with
        self.calls: list[tuple[str, str]] = []

    def download_invoice(self, *, direction: str, invoice_id: str) -> UyumsoftInvoiceDocument:
        self.calls.append((direction, invoice_id))
        if self.fail_with is not None:
            raise self.fail_with
        return UyumsoftInvoiceDocument(
            direction=direction,
            invoice_id=invoice_id,
            content=self.content if direction == "Inbox" else OUTBOX_XML,
        )

    def __getattribute__(self, name: str) -> Any:
        forbidden = {"SetInvoicesTaken", "SendInvoice", "CancelInvoice", "RetrySendInvoices", "MoveToDraftStatus"}
        if name in forbidden:
            raise AssertionError(f"Forbidden operation accessed: {name}")
        return super().__getattribute__(name)


class FailingStorage(LocalDocumentStorage):
    def write(self, storage_key: str, content: bytes) -> None:
        raise DocumentStorageError("Document storage write failed.")


class FlushFailingStorage(LocalDocumentStorage):
    def __init__(self, root: Path, session: Session) -> None:
        super().__init__(root)
        self._session = session

    def write(self, storage_key: str, content: bytes) -> None:
        super().write(storage_key, content)

        def fail_flush() -> None:
            raise SQLAlchemyError("db failure")

        self._session.flush = fail_flush  # type: ignore[method-assign]


def test_successful_inbox_xml_download_persists_metadata_and_file(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    client = RecordingDocumentClient(content=INBOX_XML)

    result = _service(session, client, tmp_path).download_documents(invoice_ids=[invoice.id])

    document = session.scalar(select(InvoiceDocument))
    assert result.downloaded == 1
    assert result.existing == 0
    assert document is not None
    assert document.invoice_id == invoice.id
    assert document.document_type == "UBL_XML"
    assert document.mime_type == "application/xml"
    assert document.content_size_bytes == len(INBOX_XML)
    assert document.content_hash_sha256 == sha256(INBOX_XML).hexdigest()
    assert (tmp_path / document.storage_key).read_bytes() == INBOX_XML
    assert client.calls == [("Inbox", "in-1")]


def test_successful_outbox_xml_download_uses_outbox_direction(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Outbox", provider_invoice_id="out-1")
    client = RecordingDocumentClient()

    result = _service(session, client, tmp_path).download_documents(invoice_ids=[invoice.id])

    document = session.scalar(select(InvoiceDocument))
    assert result.downloaded == 1
    assert document is not None
    assert document.direction == "Outbox"
    assert (tmp_path / document.storage_key).read_bytes() == OUTBOX_XML
    assert client.calls == [("Outbox", "out-1")]


def test_invoice_metadata_not_found(session: Session, tmp_path: Path) -> None:
    with pytest.raises(InvoiceDocumentNotFoundError):
        _service(session, RecordingDocumentClient(), tmp_path).download_documents(invoice_ids=[999])


def test_repeated_identical_download_is_idempotent(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    service = _service(session, RecordingDocumentClient(content=INBOX_XML), tmp_path)

    first = service.download_documents(invoice_ids=[invoice.id])
    second = service.download_documents(invoice_ids=[invoice.id])

    documents = session.scalars(select(InvoiceDocument)).all()
    assert len(documents) == 1
    assert first.downloaded == 1
    assert second.downloaded == 0
    assert second.existing == 1


def test_different_repeated_download_fails_as_conflict(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    _service(session, RecordingDocumentClient(content=INBOX_XML), tmp_path).download_documents(invoice_ids=[invoice.id])

    with pytest.raises(DocumentConflictError):
        _service(session, RecordingDocumentClient(content=b"<Invoice>changed</Invoice>"), tmp_path).download_documents(
            invoice_ids=[invoice.id]
        )


def test_empty_or_invalid_xml_response_is_rejected(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")

    with pytest.raises(DocumentValidationError):
        _service(session, RecordingDocumentClient(content=b"not xml"), tmp_path).download_documents(
            invoice_ids=[invoice.id]
        )

    assert session.scalar(select(InvoiceDocument)) is None


def test_connector_failure_does_not_store_document(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")

    with pytest.raises(ConnectorError):
        _service(
            session, RecordingDocumentClient(fail_with=ConnectorError("provider fault")), tmp_path
        ).download_documents(invoice_ids=[invoice.id])

    assert session.scalar(select(InvoiceDocument)) is None
    assert list(tmp_path.rglob("*.xml")) == []


def test_timeout_failure_is_distinguishable(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")

    with pytest.raises(ConnectorTimeoutError):
        _service(
            session,
            RecordingDocumentClient(fail_with=ConnectorTimeoutError("timeout")),
            tmp_path,
        ).download_documents(invoice_ids=[invoice.id])


def test_storage_write_failure_does_not_persist_metadata(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    service = InvoiceDocumentService(
        session=session,
        client=RecordingDocumentClient(content=INBOX_XML),
        storage=FailingStorage(tmp_path),
    )

    with pytest.raises(DocumentStorageError):
        service.download_documents(invoice_ids=[invoice.id])

    assert session.scalar(select(InvoiceDocument)) is None


def test_database_persistence_failure_cleans_up_stored_file(
    session: Session,
    tmp_path: Path,
) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    service = InvoiceDocumentService(
        session=session,
        client=RecordingDocumentClient(content=INBOX_XML),
        storage=FlushFailingStorage(tmp_path, session),
    )

    with pytest.raises(DocumentPersistenceError):
        service.download_documents(invoice_ids=[invoice.id])

    assert list(tmp_path.rglob("*.xml")) == []


def test_local_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalDocumentStorage(tmp_path)

    with pytest.raises(DocumentStorageError):
        storage.write("../escape.xml", b"<Invoice/>")


def test_safe_structured_logging(monkeypatch: pytest.MonkeyPatch, session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    log_calls: list[dict[str, Any]] = []

    def capture_log(message: str, *args: Any, **kwargs: Any) -> None:
        log_calls.append({"message": message, **kwargs})

    monkeypatch.setattr(document_service.logger, "info", capture_log)
    _service(session, RecordingDocumentClient(content=INBOX_XML), tmp_path).download_documents(invoice_ids=[invoice.id])

    assert log_calls
    success_log = next(log for log in log_calls if log["message"] == "invoice_document_download_succeeded")
    assert success_log["extra"]["provider"] == "uyumsoft"
    assert success_log["extra"]["invoice_id"] == invoice.id
    assert success_log["extra"]["provider_invoice_id"] == "in-1"
    assert success_log["extra"]["direction"] == "Inbox"
    assert success_log["extra"]["document_size_bytes"] == len(INBOX_XML)
    assert success_log["extra"]["content_hash_sha256"] == sha256(INBOX_XML).hexdigest()
    completed_log = next(log for log in log_calls if log["message"] == "invoice_document_download_completed")
    assert completed_log["extra"]["document_type"] == "UBL_XML"
    assert completed_log["extra"]["downloaded"] == 1
    assert "Invoice><ID>" not in str(log_calls)
    assert "secret" not in str(log_calls).lower()


def test_read_only_operation_enforcement(session: Session, tmp_path: Path) -> None:
    invoice = _invoice(session, direction="Inbox", provider_invoice_id="in-1")
    client = RecordingDocumentClient(content=INBOX_XML)

    _service(session, client, tmp_path).download_documents(invoice_ids=[invoice.id])

    assert client.calls == [("Inbox", "in-1")]


def _service(session: Session, client: RecordingDocumentClient, storage_root: Path) -> InvoiceDocumentService:
    return InvoiceDocumentService(
        session=session,
        client=client,
        storage=LocalDocumentStorage(storage_root),
    )


def _invoice(session: Session, *, direction: str, provider_invoice_id: str) -> UyumsoftInvoiceMetadata:
    invoice = UyumsoftInvoiceMetadata(
        provider="uyumsoft",
        direction=direction,
        provider_invoice_id=provider_invoice_id,
        ettn=f"{provider_invoice_id}-ettn",
        identity_key=f"ettn:{provider_invoice_id}-ettn",
        identity_strategy="ettn",
        invoice_number=f"{provider_invoice_id}-number",
        invoice_date=datetime(2026, 7, 17, tzinfo=UTC),
        raw_metadata={},
        first_seen_at=datetime(2026, 7, 17, tzinfo=UTC),
        last_seen_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    session.add(invoice)
    session.flush()
    return invoice
