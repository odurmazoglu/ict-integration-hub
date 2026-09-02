from __future__ import annotations

import asyncio
import hashlib
import json

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
from app.application.workbench.allocations import BusinessContextAllocation


class ExistingPurchaseOrderExecutionStrategy:
    """Execute an accepted Existing Purchase Order allocation through Odoo's standard PO billing flow."""

    name = "existing_purchase_order_execution"
    supported_step_types = (ExecutionStepType.EXISTING_PURCHASE_ORDER,)

    def __init__(
        self,
        *,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        purchase_order_vendor_bill_repository: object,
    ) -> None:
        self._source_invoice_reader = source_invoice_reader
        self._purchase_order_vendor_bill_repository = purchase_order_vendor_bill_repository

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE}

    def supports_step(self, *, step: object, mode: ExecutionMode) -> bool:
        is_existing_purchase_order = getattr(step, "step_type", None) is ExecutionStepType.EXISTING_PURCHASE_ORDER
        return is_existing_purchase_order and self.supports_mode(mode)

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.step.step_type is not ExecutionStepType.EXISTING_PURCHASE_ORDER:
            raise ExecutionUnsupportedStepError(
                "ExistingPurchaseOrderExecutionStrategy only supports EXISTING_PURCHASE_ORDER steps."
            )
        if request.mode is ExecutionMode.EXECUTE and request.approval is None:
            raise ExecutionApprovalError(
                "Explicit execution approval is required for Existing Purchase Order execution."
            )

        try:
            source = self._source_invoice_reader.get_source_invoice(
                review_id=request.review_id,
                company_id=request.company_id,
                decision_version=request.decision_version,
            )
            _validate_source(request=request, source=source)
            allocation = _purchase_order_allocation(request)
            purchase_order_id = allocation.purchase_order_id
            if purchase_order_id is None:
                raise ExecutionSourceInvoiceError(
                    "Purchase Order identity is required for Existing Purchase Order execution."
                )
            if source.partner_match.partner_id is None:
                raise ExecutionSourceInvoiceError("Supplier partner identity is required for purchase-order billing.")

            if request.mode is ExecutionMode.DRY_RUN:
                return ExecutionStepResult(
                    step_key=request.step.step_key,
                    step_type=request.step.step_type,
                    status=ExecutionStepStatus.DRY_RUN_OK,
                    dry_run=True,
                    message="Dry run completed. No Odoo purchase-order billing was triggered.",
                )

            write_result = _run_writer(
                self._purchase_order_vendor_bill_repository.create_or_recover_vendor_bill_from_purchase_order,
                purchase_order_id=purchase_order_id,
                company_id=request.company_id,
                partner_id=source.partner_match.partner_id,
                idempotency_key=existing_purchase_order_write_idempotency_key(request, purchase_order_id),
                invoice_reference=source.invoice.header.invoice_number,
                invoice_date=source.invoice.header.issue_date,
            )
            move = getattr(write_result, "move", None)
            if move is None or getattr(move, "id", None) is None:
                raise ExecutionSourceInvoiceError(
                    "Purchase Order Vendor Bill creation did not return a valid Odoo move."
                )
            created = bool(getattr(write_result, "created", True))
            message = (
                "Draft Vendor Bill recovered from the existing Odoo Purchase Order."
                if not created
                else "Draft Vendor Bill created from the existing Purchase Order in Odoo."
            )
            return ExecutionStepResult(
                step_key=request.step.step_key,
                step_type=request.step.step_type,
                status=ExecutionStepStatus.EXECUTED,
                dry_run=False,
                message=message,
                produced_artifacts=(_produced_artifact(request=request, external_id=move.id, created=created),),
            )
        except ExecutionSourceInvoiceError as exc:
            return _failure_result(request, error_code=exc.error_category, message=exc.safe_message)
        except ApplicationError as exc:
            return _failure_result(request, error_code=_writer_error_code(exc), message=exc.safe_message)


def existing_purchase_order_write_idempotency_key(
    request: ExecutionStepRequest,
    purchase_order_id: int | None = None,
) -> str:
    identity = {
        "company_id": request.company_id,
        "review_id": request.review_id,
        "decision_version": request.decision_version,
        "purchase_order_id": purchase_order_id,
        "step_key": request.step.step_key,
        "step_type": request.step.step_type.value,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"purchase-order-bill:{digest}"


def _validate_source(*, request: ExecutionStepRequest, source: ExecutionSourceInvoice) -> None:
    if source.review_id != request.review_id:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice review identity does not match execution request.")
    if source.company_id != request.company_id:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice company does not match execution request.")
    if source.decision_version != request.decision_version:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice decision version does not match execution request.")
    if source.partner_match.partner_id is None:
        raise ExecutionSourceInvoiceError("Supplier partner is required before purchase-order billing.")
    invoice_identity = source.invoice.header.ettn or source.invoice.header.invoice_uuid
    if source.source_invoice_id != invoice_identity:
        raise ExecutionSourceInvoiceIntegrityError("Source invoice identity does not match authoritative invoice.")


def _purchase_order_allocation(request: ExecutionStepRequest) -> BusinessContextAllocation:
    allocations = tuple(request.step.allocations)
    if not allocations:
        raise ExecutionSourceInvoiceError(
            "Existing Purchase Order execution requires a deterministic purchase_order allocation."
        )
    if len(allocations) != 1:
        raise ExecutionSourceInvoiceError(
            "Existing Purchase Order execution must resolve to exactly one purchase order allocation."
        )
    allocation = allocations[0]
    if allocation.purchase_order_id is None:
        raise ExecutionSourceInvoiceError("Purchase Order identity is required for Existing Purchase Order execution.")
    return allocation


def _run_writer(method: object, **kwargs: object) -> object:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(method(**kwargs))
    raise ExecutionSourceInvoiceError("Purchase Order writer cannot run inside an active event loop.")


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
        return "purchase_order_safety_gate_failure"
    if category == "authentication_failure":
        return "purchase_order_authentication_failure"
    if category == "authorization_failure":
        return "purchase_order_authorization_failure"
    if category == "validation_failure":
        return "purchase_order_validation_failure"
    if category == "duplicate_detection_failure":
        return "purchase_order_duplicate_detection_failure"
    if category == "transport_failure":
        return "purchase_order_transport_failure"
    return "purchase_order_write_error"


def _produced_artifact(
    *,
    request: ExecutionStepRequest,
    external_id: int | None,
    created: bool,
) -> ExecutionArtifact:
    purchase_order_id = next(
        (
            allocation.purchase_order_id
            for allocation in request.step.allocations
            if allocation.purchase_order_id is not None
        ),
        None,
    )
    return ExecutionArtifact(
        artifact_type=ExecutionArtifactType.VENDOR_BILL,
        artifact_id=str(external_id),
        external_identity=existing_purchase_order_write_idempotency_key(
            request,
            purchase_order_id=purchase_order_id,
        ),
        created=created,
    )
