from __future__ import annotations

import asyncio
import hashlib
import json

from app.application.commands import CustomerInvoiceWriteCommand
from app.application.exceptions import ApplicationError
from app.application.execution.contracts import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionSourceInvoice,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    ExecutionApprovalError,
    ExecutionSourceInvoiceError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionUnsupportedStepError,
)
from app.application.execution.ports import ExecutionSourceInvoiceReader
from app.application.ports import CustomerInvoiceWriter
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationType
from app.billing import CustomerInvoiceBuilder
from app.billing.exceptions import CustomerInvoiceBuildError


class CustomerInvoiceExecutionStrategy:
    """Production-capable strategy for Draft Customer Invoice creation only."""

    name = "customer_invoice_creation"
    supported_step_types = (ExecutionStepType.CUSTOMER_RECHARGE,)

    def __init__(
        self,
        *,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        customer_invoice_builder: CustomerInvoiceBuilder,
        customer_invoice_writer: CustomerInvoiceWriter,
    ) -> None:
        self._source_invoice_reader = source_invoice_reader
        self._customer_invoice_builder = customer_invoice_builder
        self._customer_invoice_writer = customer_invoice_writer

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE}

    def supports_step(self, *, step: object, mode: ExecutionMode) -> bool:
        if not self.supports_mode(mode):
            return False
        if not hasattr(step, "step_type") or step.step_type is not ExecutionStepType.CUSTOMER_RECHARGE:
            return False
        allocations = getattr(step, "allocations", ())
        instruction = getattr(step, "customer_invoice_billing_instruction", None)
        if mode is ExecutionMode.EXECUTE and instruction is None:
            return False
        return bool(allocations) and all(_is_creation_allocation(allocation) for allocation in allocations)

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.step.step_type is not ExecutionStepType.CUSTOMER_RECHARGE:
            raise ExecutionUnsupportedStepError(
                "CustomerInvoiceExecutionStrategy only supports CUSTOMER_RECHARGE steps."
            )
        if request.mode is ExecutionMode.EXECUTE and request.approval is None:
            raise ExecutionApprovalError("Explicit execution approval is required for Customer Invoice execution.")

        _customer_invoice_creation_allocations(request)
        try:
            source = self._source_invoice_reader.get_source_invoice(
                review_id=request.review_id,
                company_id=request.company_id,
                decision_version=request.decision_version,
            )
            _validate_source(request=request, source=source)
            customer_invoice = self._customer_invoice_builder.build(
                company_id=request.company_id,
                source_invoice_id=source.source_invoice_id,
                invoice=source.invoice,
                billing_instruction=_billing_instruction(request),
            )
            write_result = _run_writer(
                writer=self._customer_invoice_writer,
                command=CustomerInvoiceWriteCommand(
                    customer_invoice=customer_invoice,
                    idempotency_key=customer_invoice_write_idempotency_key(request),
                    dry_run=request.mode is ExecutionMode.DRY_RUN,
                    approved_by=request.approval.approved_by if request.approval is not None else None,
                ),
            )
        except ExecutionSourceInvoiceError as exc:
            return _failure_result(request, error_code=exc.error_category, message=exc.safe_message)
        except CustomerInvoiceBuildError as exc:
            return _failure_result(request, error_code="customer_invoice_build_error", message=exc.safe_message)
        except ApplicationError as exc:
            return _failure_result(request, error_code=_writer_error_code(exc), message=exc.safe_message)

        if write_result.status not in {"dry_run", "created", "existing"}:
            return _failure_result(
                request,
                error_code="customer_invoice_write_failed",
                message=write_result.safe_message or "Customer Invoice write failed safely.",
            )
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.DRY_RUN_OK
            if request.mode is ExecutionMode.DRY_RUN
            else ExecutionStepStatus.EXECUTED,
            dry_run=request.mode is ExecutionMode.DRY_RUN,
            message=write_result.safe_message,
            warnings=write_result.warnings,
            produced_artifacts=_produced_artifacts(
                request=request,
                external_id=write_result.external_id,
                created=write_result.status == "created",
            ),
        )


def customer_invoice_write_idempotency_key(request: ExecutionStepRequest) -> str:
    identity = {
        "company_id": request.company_id,
        "review_id": request.review_id,
        "decision_version": request.decision_version,
        "decision_id": request.decision_id,
        "step_key": request.step.step_key,
        "step_type": request.step.step_type.value,
        "allocation_keys": list(request.step.allocation_keys),
        "billing_key": request.step.customer_invoice_billing_instruction.billing_key
        if request.step.customer_invoice_billing_instruction is not None
        else None,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"customer-invoice-write:{digest}"


def _customer_invoice_creation_allocations(
    request: ExecutionStepRequest,
) -> tuple[BusinessContextAllocation, ...]:
    allocations = tuple(request.step.allocations)
    if tuple(allocation.allocation_key for allocation in allocations) != request.step.allocation_keys:
        raise ExecutionUnsupportedStepError("Customer Invoice allocation context does not match execution step.")
    if not allocations or not all(_is_creation_allocation(allocation) for allocation in allocations):
        raise ExecutionUnsupportedStepError("Customer Invoice creation requires recharge allocations without invoices.")
    return allocations


def _billing_instruction(request: ExecutionStepRequest):
    instruction = request.step.customer_invoice_billing_instruction
    if instruction is None:
        raise CustomerInvoiceBuildError(
            (
                "Customer Invoice creation requires explicit accepted billing instructions; "
                "cost allocation evidence is not customer billing evidence.",
            ),
        )
    return instruction


def _is_creation_allocation(allocation: object) -> bool:
    return (
        isinstance(allocation, BusinessContextAllocation)
        and allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
        and allocation.customer_invoice_id is None
    )


def _validate_source(*, request: ExecutionStepRequest, source: ExecutionSourceInvoice) -> None:
    if source.review_id != request.review_id:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice review identity does not match execution request.")
    if source.company_id != request.company_id:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice company does not match execution request.")
    if source.decision_version != request.decision_version:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice decision version does not match execution request.")
    invoice_identity = source.invoice.header.ettn or source.invoice.header.invoice_uuid
    if source.source_invoice_id != invoice_identity:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice identity does not match authoritative invoice.")


def _run_writer(*, writer: CustomerInvoiceWriter, command: CustomerInvoiceWriteCommand):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(writer.write_customer_invoice(command))
    raise ExecutionSourceInvoiceError("Customer Invoice writer cannot run inside an active event loop.")


def _failure_result(request: ExecutionStepRequest, *, error_code: str, message: str) -> ExecutionStepResult:
    return ExecutionStepResult(
        step_key=request.step.step_key,
        step_type=request.step.step_type,
        status=ExecutionStepStatus.FAILED,
        dry_run=request.mode is ExecutionMode.DRY_RUN,
        message=message,
        error_code=error_code,
    )


def _writer_error_code(exc: ApplicationError) -> str:
    category = exc.error_category
    if category == "production_safety_gate_failure":
        return "customer_invoice_safety_gate_failure"
    if category == "authentication_failure":
        return "customer_invoice_authentication_failure"
    if category == "authorization_failure":
        return "customer_invoice_authorization_failure"
    if category == "validation_failure":
        return "customer_invoice_validation_failure"
    if category == "duplicate_detection_failure":
        return "customer_invoice_duplicate_detection_failure"
    if category == "transport_failure":
        return "customer_invoice_transport_failure"
    return "customer_invoice_write_error"


def _produced_artifacts(
    *,
    request: ExecutionStepRequest,
    external_id: int | None,
    created: bool,
) -> tuple[ExecutionArtifact, ...]:
    if external_id is None:
        return ()
    return (
        ExecutionArtifact(
            artifact_type=ExecutionArtifactType.CUSTOMER_INVOICE,
            artifact_id=str(external_id),
            external_identity=customer_invoice_write_idempotency_key(request),
            created=created,
        ),
    )
