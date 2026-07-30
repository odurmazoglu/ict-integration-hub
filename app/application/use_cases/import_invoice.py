from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Protocol

from app.application.commands import ImportInvoiceCommand, VendorBillWriteCommand
from app.application.dto import ImportInvoiceResult, VendorBillWriteResult
from app.application.exceptions import ApplicationError
from app.application.ports import InvoiceImportHistory, VendorBillWriter
from app.billing import VendorBillBuilder
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult
from app.tax_mapping import InvoiceTaxMappingResult


class ImportInvoiceValidationError(ApplicationError):
    error_category = "invalid_import_invoice_request"


class ImportInvoiceInfrastructureError(ApplicationError):
    error_category = "import_invoice_infrastructure_error"


class SupplierPartnerMatcher(Protocol):
    """Deterministic supplier matcher used by ImportInvoiceUseCase."""

    def match_supplier(self, invoice: InternalInvoice, *, company_id: int | None = None) -> PartnerMatchResult:
        pass


class InvoiceProductMatcher(Protocol):
    """Deterministic product matcher used by ImportInvoiceUseCase."""

    def match_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceProductMatchResult:
        pass


class InvoiceTaxMapper(Protocol):
    """Deterministic tax mapper used by ImportInvoiceUseCase."""

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None = None) -> InvoiceTaxMappingResult:
        pass


class ImportInvoiceUseCase:
    """Coordinate one deterministic invoice import through the Vendor Bill path."""

    def __init__(
        self,
        *,
        import_history: InvoiceImportHistory,
        partner_matcher: SupplierPartnerMatcher,
        product_matcher: InvoiceProductMatcher,
        tax_mapper: InvoiceTaxMapper,
        vendor_bill_builder: VendorBillBuilder,
        vendor_bill_writer: VendorBillWriter,
    ) -> None:
        self._import_history = import_history
        self._partner_matcher = partner_matcher
        self._product_matcher = product_matcher
        self._tax_mapper = tax_mapper
        self._vendor_bill_builder = vendor_bill_builder
        self._vendor_bill_writer = vendor_bill_writer

    async def execute(self, command: ImportInvoiceCommand) -> ImportInvoiceResult:
        started = perf_counter()
        invoice_id = _invoice_id(command)
        idempotency_key = _idempotency_key(command)
        duplicate = _translate_infrastructure(
            lambda: self._import_history.find_imported_invoice(idempotency_key),
            "Invoice import duplicate check failed.",
        )
        if duplicate is not None:
            return ImportInvoiceResult(
                success=True,
                invoice_id=duplicate.invoice_id,
                status="already_imported",
                vendor_bill_id=duplicate.vendor_bill_id,
                warnings=("Invoice was already imported.",),
                duration=_duration(started),
            )

        partner_match = _translate_infrastructure(
            lambda: self._partner_matcher.match_supplier(command.invoice, company_id=command.company_id),
            "Supplier partner matching failed.",
        )
        product_match = _translate_infrastructure(
            lambda: self._product_matcher.match_invoice(command.invoice, company_id=command.company_id),
            "Product matching failed.",
        )
        tax_match = _translate_infrastructure(
            lambda: self._tax_mapper.map_invoice(command.invoice, company_id=command.company_id),
            "Tax mapping failed.",
        )
        vendor_bill = self._vendor_bill_builder.build(command.invoice, partner_match, product_match, tax_match)
        write_result = await _translate_writer(
            self._vendor_bill_writer.write_vendor_bill(
                VendorBillWriteCommand(
                    vendor_bill=vendor_bill,
                    idempotency_key=idempotency_key,
                    dry_run=command.dry_run,
                    approved_by=command.approved_by,
                )
            )
        )
        return _result_from_write(invoice_id=invoice_id, write_result=write_result, duration=_duration(started))


def _invoice_id(command: ImportInvoiceCommand) -> str:
    if not isinstance(command.invoice, InternalInvoice):
        raise ImportInvoiceValidationError("InternalInvoice DTO is required.")
    _idempotency_key(command)
    return command.invoice.header.ettn or command.invoice.header.invoice_uuid


def _idempotency_key(command: ImportInvoiceCommand) -> str:
    idempotency_key = command.idempotency_key.strip()
    if not idempotency_key:
        raise ImportInvoiceValidationError("Import idempotency key is required.")
    return idempotency_key


def _result_from_write(
    *,
    invoice_id: str,
    write_result: VendorBillWriteResult,
    duration: float,
) -> ImportInvoiceResult:
    success = write_result.status in {"dry_run", "created", "existing"}
    status = "already_exists" if write_result.status == "existing" else write_result.status
    errors = () if success else _safe_errors(write_result.safe_message)
    warnings = _safe_warnings(write_result.safe_message) if success and write_result.safe_message else ()
    return ImportInvoiceResult(
        success=success,
        invoice_id=invoice_id,
        status=status,
        vendor_bill_id=write_result.external_id,
        warnings=warnings,
        errors=errors,
        duration=duration,
    )


def _translate_infrastructure[T](operation: Callable[[], T], fallback_message: str) -> T:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ImportInvoiceInfrastructureError(_safe_message(exc, fallback_message)) from exc


async def _translate_writer(awaitable: Awaitable[VendorBillWriteResult]) -> VendorBillWriteResult:
    try:
        return await awaitable
    except ApplicationError:
        raise
    except Exception as exc:
        raise ImportInvoiceInfrastructureError(_safe_message(exc, "Vendor Bill write operation failed.")) from exc


def _safe_message(exc: Exception, fallback_message: str) -> str:
    safe_message = getattr(exc, "safe_message", None)
    return safe_message if isinstance(safe_message, str) and safe_message.strip() else fallback_message


def _safe_errors(message: str | None) -> tuple[str, ...]:
    return (message,) if message else ("Vendor Bill write operation failed.",)


def _safe_warnings(message: str | None) -> tuple[str, ...]:
    return (message,) if message else ()


def _duration(started: float) -> float:
    return perf_counter() - started
