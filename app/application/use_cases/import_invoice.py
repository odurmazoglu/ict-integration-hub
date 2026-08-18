from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from app.application.commands import ImportInvoiceCommand
from app.application.decision import DecisionEngine
from app.application.dto import DecisionResult, ImportInvoiceResult
from app.application.exceptions import ApplicationError
from app.application.ports import InvoiceImportHistory
from app.application.services import UnitOfWork
from app.application.workbench import (
    ReviewClassificationEvidence,
    ReviewItem,
    ReviewItemCreationService,
    ReviewStatus,
    WorkbenchProjection,
    WorkbenchProjectionPublisher,
)
from app.domain.invoice import InternalInvoice

WORKBENCH_PROJECTION_FAILURE_WARNING = "Odoo Workbench projection publish failed; Hub review remains authoritative."


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
        review_item_creation_service: ReviewItemCreationService | None = None,
        workbench_projection_publisher: WorkbenchProjectionPublisher | None = None,
        unit_of_work: UnitOfWork | None = None,
    ) -> None:
        self._import_history = import_history
        self._decision_engine = decision_engine
        self._review_item_creation_service = review_item_creation_service
        self._workbench_projection_publisher = workbench_projection_publisher
        self._unit_of_work = unit_of_work

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
        review_item, projection_warnings = _persist_and_publish_review_if_required(
            command=replace(command, idempotency_key=idempotency_key),
            decision_result=decision_result,
            review_item_creation_service=self._review_item_creation_service,
            workbench_projection_publisher=self._workbench_projection_publisher,
            unit_of_work=self._unit_of_work,
        )
        return _result_from_decision(
            invoice_id=invoice_id,
            decision_result=decision_result,
            review_id=review_item.review_id if review_item is not None else decision_result.review_id,
            warnings=projection_warnings,
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
    review_id: str | None = None,
    warnings: tuple[str, ...] = (),
    duration: float,
) -> ImportInvoiceResult:
    return ImportInvoiceResult(
        success=decision_result.success,
        invoice_id=invoice_id,
        status=decision_result.status,
        vendor_bill_id=decision_result.vendor_bill_id,
        review_id=review_id,
        review_required=decision_result.review_required,
        review_reasons=decision_result.review_reasons,
        classification_result=decision_result.classification_result,
        warnings=decision_result.warnings + warnings,
        errors=decision_result.errors,
        duration=duration,
    )


def _persist_and_publish_review_if_required(
    *,
    command: ImportInvoiceCommand,
    decision_result: DecisionResult,
    review_item_creation_service: ReviewItemCreationService | None,
    workbench_projection_publisher: WorkbenchProjectionPublisher | None,
    unit_of_work: UnitOfWork | None,
) -> tuple[ReviewItem | None, tuple[str, ...]]:
    if not decision_result.review_required or review_item_creation_service is None:
        return None, ()
    company_id = _company_id(command)
    item = _review_item_from_import(command=command, decision_result=decision_result, company_id=company_id)
    classification_evidence = _classification_evidence(
        item=item,
        company_id=company_id,
        decision_result=decision_result,
    )
    persisted = _create_review_item(
        item=item,
        company_id=company_id,
        idempotency_key=command.idempotency_key,
        classification_evidence=classification_evidence,
        review_item_creation_service=review_item_creation_service,
    )
    if unit_of_work is not None:
        unit_of_work.commit()
    if workbench_projection_publisher is None:
        return persisted, ()
    return persisted, _publish_workbench_projection(
        persisted,
        company_id=company_id,
        publisher=workbench_projection_publisher,
    )


def _create_review_item(
    *,
    item: ReviewItem,
    company_id: int,
    idempotency_key: str,
    classification_evidence: ReviewClassificationEvidence | None,
    review_item_creation_service: ReviewItemCreationService,
) -> ReviewItem:
    if classification_evidence is None:
        return review_item_creation_service.create_pending_review_item(
            item,
            company_id=company_id,
            idempotency_key=idempotency_key,
        )
    return review_item_creation_service.create_pending_review_item_with_classification_evidence(
        item,
        company_id=company_id,
        idempotency_key=idempotency_key,
        classification_evidence=classification_evidence,
    )


def _review_item_from_import(
    *,
    command: ImportInvoiceCommand,
    decision_result: DecisionResult,
    company_id: int,
) -> ReviewItem:
    invoice = command.invoice
    review_id = decision_result.review_id or _review_id(
        company_id=company_id,
        idempotency_key=command.idempotency_key,
    )
    return ReviewItem(
        review_id=review_id,
        invoice_id=invoice.header.ettn or invoice.header.invoice_uuid,
        invoice_number=invoice.header.invoice_number,
        supplier_tax_number=invoice.supplier.tax_number,
        supplier_name=invoice.supplier.name,
        invoice_date=invoice.header.issue_date,
        currency=invoice.header.currency_code,
        total_amount=invoice.totals.payable_amount,
        workflow=decision_result.workflow,
        status=ReviewStatus.PENDING_REVIEW,
        review_reasons=decision_result.review_reasons,
        warnings=decision_result.warnings,
    )


def _classification_evidence(
    *,
    item: ReviewItem,
    company_id: int,
    decision_result: DecisionResult,
) -> ReviewClassificationEvidence | None:
    if decision_result.classification_result is None:
        return None
    return ReviewClassificationEvidence.from_result(
        review_id=item.review_id,
        company_id=company_id,
        review_version=item.version,
        result=decision_result.classification_result,
    )


def _publish_workbench_projection(
    item: ReviewItem,
    *,
    company_id: int,
    publisher: WorkbenchProjectionPublisher,
) -> tuple[str, ...]:
    try:
        result = publisher.publish_projection(_projection_from_review_item(item, company_id=company_id))
    except Exception:
        return (WORKBENCH_PROJECTION_FAILURE_WARNING,)
    return result.warnings


def _projection_from_review_item(item: ReviewItem, *, company_id: int) -> WorkbenchProjection:
    return WorkbenchProjection(
        review_id=item.review_id,
        company_id=company_id,
        invoice_id=item.invoice_id,
        version=item.version,
        status=item.status,
        invoice_number=item.invoice_number,
        supplier_name=item.supplier_name,
        supplier_tax_number=item.supplier_tax_number,
        invoice_date=item.invoice_date,
        currency=item.currency,
        total_amount=item.total_amount,
        workflow=item.workflow,
        review_summary=None,
        review_reasons=item.review_reasons,
        warnings=item.warnings,
        updated_at=item.updated_at,
    )


def _company_id(command: ImportInvoiceCommand) -> int:
    if type(command.company_id) is not int or command.company_id <= 0:
        raise ImportInvoiceValidationError("A positive company_id is required for Workbench review creation.")
    return command.company_id


def _review_id(*, company_id: int, idempotency_key: str) -> str:
    identity = f"ict-integration-hub:workbench-review:{company_id}:{idempotency_key}"
    return f"review:{uuid5(NAMESPACE_URL, identity)}"


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
