from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import perf_counter

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult, ImportInvoiceResult
from app.application.exceptions import ApplicationError
from app.application.ports import InvoiceImportHistory
from app.domain.invoice import InternalInvoice


class ImportInvoiceValidationError(ApplicationError):
    error_category = "invalid_import_invoice_request"


class ImportInvoiceInfrastructureError(ApplicationError):
    error_category = "import_invoice_infrastructure_error"


class ImportInvoiceUseCase:
    """Coordinate one invoice import through the Decision Engine."""

    def __init__(
        self,
        *,
        import_history: InvoiceImportHistory,
        decision_engine: DecisionEngine,
    ) -> None:
        self._import_history = import_history
        self._decision_engine = decision_engine

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

        decision_result = await _translate_decision(
            self._decision_engine.decide(replace(command, idempotency_key=idempotency_key)),
        )
        return _result_from_decision(
            invoice_id=invoice_id,
            decision_result=decision_result,
            duration=_duration(started),
        )


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


def _result_from_decision(
    *,
    invoice_id: str,
    decision_result: DecisionResult,
    duration: float,
) -> ImportInvoiceResult:
    return ImportInvoiceResult(
        success=decision_result.success,
        invoice_id=invoice_id,
        status=decision_result.status,
        vendor_bill_id=decision_result.vendor_bill_id,
        warnings=decision_result.warnings,
        errors=decision_result.errors,
        duration=duration,
    )


def _translate_infrastructure[T](operation: Callable[[], T], fallback_message: str) -> T:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ImportInvoiceInfrastructureError(_safe_message(exc, fallback_message)) from exc


async def _translate_decision(awaitable: Awaitable[DecisionResult]) -> DecisionResult:
    try:
        return await awaitable
    except ApplicationError:
        raise
    except Exception as exc:
        raise ImportInvoiceInfrastructureError(_safe_message(exc, "Decision Engine execution failed.")) from exc


def _safe_message(exc: Exception, fallback_message: str) -> str:
    safe_message = getattr(exc, "safe_message", None)
    return safe_message if isinstance(safe_message, str) and safe_message.strip() else fallback_message


def _duration(started: float) -> float:
    return perf_counter() - started
