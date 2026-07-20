import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.connectors.uyumsoft.client import UyumsoftSoapClient
from app.models.invoice_document import InvoiceDocument
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.invoice_document import DocumentType
from app.services.document_storage import DocumentStorage

logger = logging.getLogger(__name__)

DOCUMENT_TYPE_UBL_XML: DocumentType = "UBL_XML"
MIME_TYPE_UBL_XML = "application/xml"


class DocumentDownloadError(Exception):
    safe_message = "Document download failed."


class InvoiceDocumentNotFoundError(DocumentDownloadError):
    safe_message = "Invoice metadata was not found."


class UnsupportedDocumentTypeError(DocumentDownloadError):
    safe_message = "Document type is not supported."


class DocumentValidationError(DocumentDownloadError):
    safe_message = "Downloaded document is empty or invalid."


class DocumentConflictError(DocumentDownloadError):
    safe_message = "A different document already exists for this invoice and document type."


class DocumentPersistenceError(DocumentDownloadError):
    safe_message = "Document metadata persistence failed."


@dataclass(frozen=True)
class DocumentDownloadItem:
    invoice_id: int
    document_id: int
    status: str
    document_type: DocumentType
    storage_backend: str
    storage_key: str
    content_hash_sha256: str
    content_size_bytes: int


@dataclass(frozen=True)
class DocumentDownloadResult:
    provider: str
    document_type: DocumentType
    items: list[DocumentDownloadItem]

    @property
    def downloaded(self) -> int:
        return sum(1 for item in self.items if item.status == "downloaded")

    @property
    def existing(self) -> int:
        return sum(1 for item in self.items if item.status == "existing")


class InvoiceDocumentService:
    def __init__(
        self,
        *,
        session: Session,
        client: UyumsoftSoapClient,
        storage: DocumentStorage,
        provider: str = "uyumsoft",
    ) -> None:
        self._session = session
        self._client = client
        self._storage = storage
        self._provider = provider

    def download_documents(
        self,
        *,
        invoice_ids: list[int],
        document_type: DocumentType = DOCUMENT_TYPE_UBL_XML,
    ) -> DocumentDownloadResult:
        _validate_document_type(document_type)
        started = perf_counter()
        items = [self._download_one(invoice_id=invoice_id, document_type=document_type) for invoice_id in invoice_ids]
        result = DocumentDownloadResult(provider=self._provider, document_type=document_type, items=items)
        logger.info(
            "invoice_document_download_completed",
            extra={
                "provider": result.provider,
                "document_type": result.document_type,
                "downloaded": result.downloaded,
                "existing": result.existing,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
                "result": "success",
            },
        )
        return result

    def _download_one(self, *, invoice_id: int, document_type: DocumentType) -> DocumentDownloadItem:
        invoice = self._get_invoice(invoice_id)
        provider_invoice_id = _required_provider_invoice_id(invoice)
        downloaded = self._client.download_invoice(direction=invoice.direction, invoice_id=provider_invoice_id)
        content = downloaded.content
        _validate_xml_like(content)
        content_hash = hashlib.sha256(content).hexdigest()
        content_size = len(content)

        existing = self._find_existing(invoice.id, document_type)
        if existing is not None:
            if existing.content_hash_sha256 != content_hash:
                raise DocumentConflictError(DocumentConflictError.safe_message)
            return _item_from_record(existing, status="existing")

        storage_key = _storage_key(
            provider=self._provider,
            direction=invoice.direction,
            invoice_id=invoice.id,
            document_type=document_type,
            content_hash=content_hash,
        )
        self._storage.write(storage_key, content)
        record = InvoiceDocument(
            invoice_id=invoice.id,
            provider=self._provider,
            direction=invoice.direction,
            document_type=document_type,
            storage_backend=self._storage.backend_name,
            storage_key=storage_key,
            content_hash_sha256=content_hash,
            mime_type=MIME_TYPE_UBL_XML,
            content_size_bytes=content_size,
            downloaded_at=datetime.now(UTC),
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError as exc:
            self._storage.delete(storage_key)
            concurrent = self._find_existing(invoice.id, document_type)
            if concurrent is not None and concurrent.content_hash_sha256 == content_hash:
                return _item_from_record(concurrent, status="existing")
            raise DocumentConflictError(DocumentConflictError.safe_message) from exc
        except SQLAlchemyError as exc:
            self._storage.delete(storage_key)
            raise DocumentPersistenceError(DocumentPersistenceError.safe_message) from exc
        logger.info(
            "invoice_document_download_succeeded",
            extra={
                "provider": self._provider,
                "invoice_id": invoice.id,
                "provider_invoice_id": provider_invoice_id,
                "direction": invoice.direction,
                "document_type": document_type,
                "document_size_bytes": content_size,
                "content_hash_sha256": content_hash,
                "result": "success",
            },
        )
        return _item_from_record(record, status="downloaded")

    def _get_invoice(self, invoice_id: int) -> UyumsoftInvoiceMetadata:
        invoice = self._session.get(UyumsoftInvoiceMetadata, invoice_id)
        if invoice is None or invoice.provider != self._provider:
            raise InvoiceDocumentNotFoundError(InvoiceDocumentNotFoundError.safe_message)
        if invoice.direction not in {"Inbox", "Outbox"}:
            raise UnsupportedDocumentTypeError("Unsupported invoice direction for document download.")
        return invoice

    def _find_existing(self, invoice_id: int, document_type: DocumentType) -> InvoiceDocument | None:
        return self._session.scalar(
            select(InvoiceDocument).where(
                InvoiceDocument.invoice_id == invoice_id,
                InvoiceDocument.document_type == document_type,
            )
        )


def _validate_document_type(document_type: DocumentType) -> None:
    if document_type != DOCUMENT_TYPE_UBL_XML:
        raise UnsupportedDocumentTypeError(UnsupportedDocumentTypeError.safe_message)


def _required_provider_invoice_id(invoice: UyumsoftInvoiceMetadata) -> str:
    if invoice.provider_invoice_id is None or not invoice.provider_invoice_id.strip():
        raise DocumentValidationError("Invoice metadata does not contain a provider invoice id.")
    return invoice.provider_invoice_id


def _validate_xml_like(content: bytes) -> None:
    if not content:
        raise DocumentValidationError(DocumentValidationError.safe_message)
    stripped = content.lstrip()
    for marker in (b"\xef\xbb\xbf", b"\xfe\xff", b"\xff\xfe"):
        if stripped.startswith(marker):
            stripped = stripped[len(marker) :].lstrip()
            break
    if not stripped.startswith(b"<"):
        raise DocumentValidationError(DocumentValidationError.safe_message)


def _storage_key(
    *,
    provider: str,
    direction: str,
    invoice_id: int,
    document_type: DocumentType,
    content_hash: str,
) -> str:
    return f"{provider}/{direction.lower()}/{invoice_id}/{document_type.lower()}/{content_hash}.xml"


def _item_from_record(record: InvoiceDocument, *, status: str) -> DocumentDownloadItem:
    return DocumentDownloadItem(
        invoice_id=record.invoice_id,
        document_id=record.id,
        status=status,
        document_type=record.document_type,
        storage_backend=record.storage_backend,
        storage_key=record.storage_key,
        content_hash_sha256=record.content_hash_sha256,
        content_size_bytes=record.content_size_bytes,
    )
