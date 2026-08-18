from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from app.application.commands import ImportInvoiceCommand
from app.application.dto import ImportInvoiceResult
from app.application.exceptions import ApplicationError
from app.application.use_cases import ImportInvoiceUseCase
from app.connectors.exceptions import ConnectorError, ConnectorTimeoutError
from app.domain.invoice import InternalInvoice
from app.domain.invoice.exceptions import InvoiceDomainError
from app.domain.invoice.parser import parse_ubl_invoice
from app.erp.exceptions import ErpRepositoryError
from app.erp.repositories import CompanyRepository
from app.models.uyumsoft_invoice import UyumsoftInvoiceMetadata
from app.schemas.uyumsoft_invoices import InvoiceDirection, UyumsoftInvoiceSummary
from app.services.document_service import DOCUMENT_TYPE_UBL_XML, DocumentDownloadError, InvoiceDocumentService
from app.services.document_storage import DocumentStorage, DocumentStorageError
from app.services.invoice_persistence import build_invoice_identity

IMPORT_STATUS_ACCEPTED = "accepted"
IMPORT_STATUS_IMPORTED = IMPORT_STATUS_ACCEPTED
IMPORT_STATUS_REVIEW_CREATED = "review_created"
IMPORT_STATUS_ALREADY_IMPORTED = "already_imported"
IMPORT_STATUS_SKIPPED_DIRECTION = "skipped_direction"
IMPORT_STATUS_PROVIDER_METADATA_NOT_FOUND = "provider_metadata_not_found"
IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED = "provider_download_failed"
IMPORT_STATUS_NORMALIZATION_FAILED = "normalization_failed"
IMPORT_STATUS_COMPANY_RESOLUTION_FAILED = "company_resolution_failed"
IMPORT_STATUS_CANONICAL_IMPORT_FAILED = "canonical_import_failed"


class ImportUseCaseFactory(Protocol):
    def __call__(self) -> ImportInvoiceUseCase:
        pass


ParseUblInvoice = Callable[[bytes], InternalInvoice]


@dataclass(frozen=True, slots=True)
class UyumsoftCanonicalImportOutcome:
    direction: InvoiceDirection
    invoice_identity: str
    status: str
    company_id: int | None = None
    import_status: str | None = None
    imported_invoice_id: str | None = None
    review_id: str | None = None
    warning_count: int = 0
    safe_message: str | None = None


@dataclass(frozen=True, slots=True)
class UyumsoftCanonicalImportBatchResult:
    outcomes: tuple[UyumsoftCanonicalImportOutcome, ...] = field(default_factory=tuple)

    @property
    def imported_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == IMPORT_STATUS_IMPORTED)

    @property
    def review_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == IMPORT_STATUS_REVIEW_CREATED)

    @property
    def already_imported_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == IMPORT_STATUS_ALREADY_IMPORTED)

    @property
    def failed_import_count(self) -> int:
        failed_statuses = {
            IMPORT_STATUS_PROVIDER_METADATA_NOT_FOUND,
            IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
            IMPORT_STATUS_NORMALIZATION_FAILED,
            IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
            IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
        }
        return sum(1 for outcome in self.outcomes if outcome.status in failed_statuses)

    @property
    def skipped_import_count(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == IMPORT_STATUS_SKIPPED_DIRECTION)


class ExactCompanyResolver:
    def __init__(self, repository: CompanyRepository) -> None:
        self._repository = repository

    def resolve_company_id(self, invoice: InternalInvoice) -> int | None:
        tax_number = _clean(invoice.customer.tax_number)
        if tax_number is None:
            return None
        companies = self._repository.find_by_tax_number(tax_number)
        if len(companies) != 1:
            return None
        return companies[0].id


class UyumsoftCanonicalInvoiceImporter:
    def __init__(
        self,
        *,
        document_service: InvoiceDocumentService,
        storage: DocumentStorage,
        company_resolver: ExactCompanyResolver,
        import_use_case_factory: ImportUseCaseFactory,
        parse_invoice: ParseUblInvoice = parse_ubl_invoice,
    ) -> None:
        self._document_service = document_service
        self._storage = storage
        self._company_resolver = company_resolver
        self._import_use_case_factory = import_use_case_factory
        self._parse_invoice = parse_invoice

    def import_invoices(
        self,
        invoices: list[UyumsoftInvoiceSummary],
        *,
        persisted_records: dict[str, UyumsoftInvoiceMetadata],
    ) -> UyumsoftCanonicalImportBatchResult:
        outcomes = tuple(
            self.import_invoice(invoice, persisted_record=persisted_records.get(_invoice_identity(invoice)))
            for invoice in invoices
        )
        return UyumsoftCanonicalImportBatchResult(outcomes=outcomes)

    def import_invoice(
        self,
        invoice: UyumsoftInvoiceSummary,
        *,
        persisted_record: UyumsoftInvoiceMetadata | None,
    ) -> UyumsoftCanonicalImportOutcome:
        identity = _invoice_identity(invoice)
        if invoice.direction != "Inbox":
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_SKIPPED_DIRECTION,
                safe_message="Only incoming supplier invoices are imported.",
            )
        if persisted_record is None:
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_PROVIDER_METADATA_NOT_FOUND,
                safe_message="Provider invoice metadata was not found after synchronization.",
            )

        document_content = self._download_and_read_document(persisted_record)
        if isinstance(document_content, UyumsoftCanonicalImportOutcome):
            return document_content

        parsed = self._parse_internal_invoice(document_content, invoice=invoice)
        if isinstance(parsed, UyumsoftCanonicalImportOutcome):
            return parsed

        try:
            company_id = self._company_resolver.resolve_company_id(parsed)
        except ErpRepositoryError as exc:
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
                safe_message=_safe_message(exc, "Exact company resolution failed."),
            )
        if company_id is None:
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_COMPANY_RESOLUTION_FAILED,
                safe_message="Exact company could not be resolved from invoice buyer tax number.",
            )

        idempotency_key = import_idempotency_key(company_id=company_id, provider="uyumsoft", invoice=invoice)
        try:
            result = _run_import(
                self._import_use_case_factory().execute(
                    ImportInvoiceCommand(
                        invoice=parsed,
                        idempotency_key=idempotency_key,
                        company_id=company_id,
                        dry_run=True,
                    )
                )
            )
        except ApplicationError as exc:
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=identity,
                status=IMPORT_STATUS_CANONICAL_IMPORT_FAILED,
                company_id=company_id,
                safe_message=_safe_message(exc, "Canonical invoice import failed."),
            )
        return _outcome_from_import_result(
            result,
            direction=invoice.direction,
            invoice_identity=identity,
            company_id=company_id,
        )

    def _download_and_read_document(
        self,
        persisted_record: UyumsoftInvoiceMetadata,
    ) -> bytes | UyumsoftCanonicalImportOutcome:
        try:
            download = self._document_service.download_documents(
                invoice_ids=[persisted_record.id],
                document_type=DOCUMENT_TYPE_UBL_XML,
            )
            if len(download.items) != 1:
                raise DocumentDownloadError("Exactly one UBL document is required for canonical import.")
            item = download.items[0]
            return self._storage.read(item.storage_key)
        except (ConnectorError, ConnectorTimeoutError, DocumentDownloadError) as exc:
            return UyumsoftCanonicalImportOutcome(
                direction=persisted_record.direction,  # type: ignore[arg-type]
                invoice_identity=persisted_record.identity_key,
                status=IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
                safe_message=_safe_message(exc, "Provider invoice document download failed."),
            )
        except DocumentStorageError as exc:
            return UyumsoftCanonicalImportOutcome(
                direction=persisted_record.direction,  # type: ignore[arg-type]
                invoice_identity=persisted_record.identity_key,
                status=IMPORT_STATUS_PROVIDER_DOWNLOAD_FAILED,
                safe_message=_safe_message(exc, "Provider invoice document could not be read."),
            )

    def _parse_internal_invoice(
        self,
        content: bytes,
        *,
        invoice: UyumsoftInvoiceSummary,
    ) -> InternalInvoice | UyumsoftCanonicalImportOutcome:
        try:
            parsed = self._parse_invoice(content)
        except InvoiceDomainError as exc:
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=_invoice_identity(invoice),
                status=IMPORT_STATUS_NORMALIZATION_FAILED,
                safe_message=_safe_message(exc, "Invoice UBL normalization failed."),
            )
        if not isinstance(parsed, InternalInvoice):
            return UyumsoftCanonicalImportOutcome(
                direction=invoice.direction,
                invoice_identity=_invoice_identity(invoice),
                status=IMPORT_STATUS_NORMALIZATION_FAILED,
                safe_message="Invoice UBL parser did not return InternalInvoice.",
            )
        return parsed


def import_idempotency_key(*, company_id: int, provider: str, invoice: UyumsoftInvoiceSummary) -> str:
    identity = build_invoice_identity(invoice)
    return f"{provider}:company:{company_id}:{invoice.direction.lower()}:{identity.key}"


def _outcome_from_import_result(
    result: ImportInvoiceResult,
    *,
    direction: InvoiceDirection,
    invoice_identity: str,
    company_id: int,
) -> UyumsoftCanonicalImportOutcome:
    if result.status == "already_imported":
        status = IMPORT_STATUS_ALREADY_IMPORTED
    elif result.review_required or result.status == "review_required":
        status = IMPORT_STATUS_REVIEW_CREATED
    elif result.success:
        status = IMPORT_STATUS_IMPORTED
    else:
        status = IMPORT_STATUS_CANONICAL_IMPORT_FAILED
    return UyumsoftCanonicalImportOutcome(
        direction=direction,
        invoice_identity=invoice_identity,
        status=status,
        company_id=company_id,
        import_status=result.status,
        imported_invoice_id=result.invoice_id,
        review_id=result.review_id,
        warning_count=len(result.warnings),
        safe_message=_first_safe_message(result.errors or result.warnings),
    )


def _run_import(awaitable: object) -> ImportInvoiceResult:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)  # type: ignore[arg-type]
    raise RuntimeError("Canonical Uyumsoft import cannot run inside an active event loop.")


def _invoice_identity(invoice: UyumsoftInvoiceSummary) -> str:
    return build_invoice_identity(invoice).key


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _safe_message(exc: Exception, fallback: str) -> str:
    safe_message = getattr(exc, "safe_message", None)
    return safe_message if isinstance(safe_message, str) and safe_message.strip() else fallback


def _first_safe_message(messages: tuple[str, ...]) -> str | None:
    if not messages:
        return None
    first = messages[0].strip()
    return first or None
