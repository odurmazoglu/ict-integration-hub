from __future__ import annotations

from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from app.application.commands import ImportInvoiceCommand, ImportSessionCommand
from app.application.dto import ImportInvoiceResult, ImportSessionResult, ImportSessionStatus
from app.domain.invoice import InternalInvoice

DUPLICATE_STATUSES = frozenset({"already_imported", "already_exists"})


class InvoiceImportUseCase(Protocol):
    async def execute(self, command: ImportInvoiceCommand) -> ImportInvoiceResult:
        pass


class ImportSession:
    """Sequential in-memory orchestration for multiple invoice imports."""

    def __init__(self, *, import_invoice_use_case: InvoiceImportUseCase) -> None:
        self._import_invoice_use_case = import_invoice_use_case
        self._status: ImportSessionStatus = "CREATED"

    @property
    def status(self) -> ImportSessionStatus:
        return self._status

    async def execute(self, command: ImportSessionCommand) -> ImportSessionResult:
        session_id = _session_id(command)
        started_at = datetime.now(UTC)
        started = perf_counter()
        self._status = "RUNNING"
        results: list[ImportInvoiceResult] = []

        for invoice in command.invoices:
            results.append(await self._execute_invoice(invoice=invoice, command=command))

        finished_at = datetime.now(UTC)
        result_tuple = tuple(results)
        failed = sum(1 for result in result_tuple if not result.success)
        self._status = "FAILED" if failed else "COMPLETED"
        return ImportSessionResult(
            session_id=session_id,
            status=self._status,
            started_at=started_at,
            finished_at=finished_at,
            duration=perf_counter() - started,
            processed=len(result_tuple),
            successful=sum(1 for result in result_tuple if result.success),
            duplicates=sum(1 for result in result_tuple if result.status in DUPLICATE_STATUSES),
            failed=failed,
            warnings=tuple(warning for result in result_tuple for warning in result.warnings),
            errors=tuple(error for result in result_tuple for error in result.errors),
            results=result_tuple,
        )

    async def _execute_invoice(self, *, invoice: InternalInvoice, command: ImportSessionCommand) -> ImportInvoiceResult:
        started = perf_counter()
        try:
            return await self._import_invoice_use_case.execute(
                ImportInvoiceCommand(
                    invoice=invoice,
                    idempotency_key=_invoice_id(invoice),
                    company_id=command.company_id,
                    dry_run=command.dry_run,
                    approved_by=command.approved_by,
                )
            )
        except Exception as exc:
            return ImportInvoiceResult(
                success=False,
                invoice_id=_invoice_id(invoice),
                status="failed",
                errors=(_safe_error(exc),),
                duration=perf_counter() - started,
            )


def _session_id(command: ImportSessionCommand) -> str:
    if command.session_id is not None and command.session_id.strip():
        return command.session_id.strip()
    return str(uuid4())


def _invoice_id(invoice: InternalInvoice) -> str:
    return invoice.header.ettn or invoice.header.invoice_uuid


def _safe_error(exc: Exception) -> str:
    safe_message = getattr(exc, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return safe_message
    return "Invoice import failed."
