from __future__ import annotations

import asyncio
import hashlib
import json

from app.application.commands import VendorBillWriteCommand
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
from app.application.ports import VendorBillWriter
from app.billing import VendorBillBuilder
from app.billing.exceptions import VendorBillBuildError


class VendorBillExecutionStrategy:
    """Production-capable execution strategy for Draft Vendor Bill creation only."""

    name = "vendor_bill_execution"
    supported_step_types = (ExecutionStepType.VENDOR_BILL,)

    def __init__(
        self,
        *,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        vendor_bill_builder: VendorBillBuilder,
        vendor_bill_writer: VendorBillWriter,
    ) -> None:
        self._source_invoice_reader = source_invoice_reader
        self._vendor_bill_builder = vendor_bill_builder
        self._vendor_bill_writer = vendor_bill_writer

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE}

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.step.step_type is not ExecutionStepType.VENDOR_BILL:
            raise ExecutionUnsupportedStepError("VendorBillExecutionStrategy only supports VENDOR_BILL steps.")
        if request.mode is ExecutionMode.EXECUTE and request.approval is None:
            raise ExecutionApprovalError("Explicit execution approval is required for Vendor Bill execution.")

        try:
            source = self._source_invoice_reader.get_source_invoice(
                review_id=request.review_id,
                company_id=request.company_id,
                decision_version=request.decision_version,
            )
            _validate_source(request=request, source=source)
            vendor_bill = self._vendor_bill_builder.build(
                source.invoice,
                source.partner_match,
                source.product_match,
                source.tax_match,
            )
            write_result = _run_writer(
                writer=self._vendor_bill_writer,
                command=VendorBillWriteCommand(
                    vendor_bill=vendor_bill,
                    idempotency_key=vendor_bill_write_idempotency_key(request),
                    dry_run=request.mode is ExecutionMode.DRY_RUN,
                    approved_by=request.approval.approved_by if request.approval is not None else None,
                ),
            )
        except ExecutionSourceInvoiceError as exc:
            return _failure_result(request, error_code=exc.error_category, message=exc.safe_message)
        except VendorBillBuildError as exc:
            return _failure_result(request, error_code="vendor_bill_build_error", message=exc.safe_message)
        except ApplicationError as exc:
            return _failure_result(request, error_code=_writer_error_code(exc), message=exc.safe_message)

        if write_result.status not in {"dry_run", "created", "existing"}:
            return _failure_result(
                request,
                error_code="vendor_bill_write_failed",
                message=write_result.safe_message or "Vendor Bill write failed safely.",
            )

        step_status = (
            ExecutionStepStatus.DRY_RUN_OK if request.mode is ExecutionMode.DRY_RUN else ExecutionStepStatus.EXECUTED
        )
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=step_status,
            dry_run=request.mode is ExecutionMode.DRY_RUN,
            message=write_result.safe_message,
            warnings=write_result.warnings,
            produced_artifacts=_produced_artifacts(
                request=request,
                external_id=write_result.external_id,
                created=write_result.status == "created",
            ),
        )


def vendor_bill_write_idempotency_key(request: ExecutionStepRequest) -> str:
    identity = {
        "company_id": request.company_id,
        "review_id": request.review_id,
        "decision_version": request.decision_version,
        "step_key": request.step.step_key,
        "step_type": request.step.step_type.value,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"vendor-bill-write:{digest}"


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


def _run_writer(*, writer: VendorBillWriter, command: VendorBillWriteCommand):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(writer.write_vendor_bill(command))
    raise ExecutionSourceInvoiceError("Vendor Bill writer cannot run inside an active event loop.")


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
        return "vendor_bill_safety_gate_failure"
    if category == "authentication_failure":
        return "vendor_bill_authentication_failure"
    if category == "authorization_failure":
        return "vendor_bill_authorization_failure"
    if category == "validation_failure":
        return "vendor_bill_validation_failure"
    if category == "duplicate_detection_failure":
        return "vendor_bill_duplicate_detection_failure"
    if category == "transport_failure":
        return "vendor_bill_transport_failure"
    return "vendor_bill_write_error"


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
            artifact_type=ExecutionArtifactType.VENDOR_BILL,
            artifact_id=str(external_id),
            external_identity=vendor_bill_write_idempotency_key(request),
            created=created,
        ),
    )
