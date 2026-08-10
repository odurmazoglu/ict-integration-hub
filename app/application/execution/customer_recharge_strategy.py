from __future__ import annotations

from app.application.execution.contracts import (
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import (
    CustomerRechargeInvoiceCreationRequiredError,
    ExecutionUnsupportedStepError,
)
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationType


class CustomerRechargeExecutionStrategy:
    """No-write strategy for recharge allocations linked to existing customer invoices."""

    name = "customer_recharge_existing_invoice"
    supported_step_types = (ExecutionStepType.CUSTOMER_RECHARGE,)

    def supports_mode(self, mode: ExecutionMode) -> bool:
        return mode in {ExecutionMode.DRY_RUN, ExecutionMode.EXECUTE}

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        if request.step.step_type is not ExecutionStepType.CUSTOMER_RECHARGE:
            raise ExecutionUnsupportedStepError(
                "CustomerRechargeExecutionStrategy only supports CUSTOMER_RECHARGE steps."
            )

        allocations = _customer_recharge_allocations(request)
        if request.mode is ExecutionMode.EXECUTE:
            _require_existing_customer_invoices(allocations)

        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.DRY_RUN_OK
            if request.mode is ExecutionMode.DRY_RUN
            else ExecutionStepStatus.EXECUTED,
            dry_run=request.mode is ExecutionMode.DRY_RUN,
            message=_message(request.mode),
            produced_artifacts=_customer_invoice_artifacts(allocations),
        )


def _customer_recharge_allocations(request: ExecutionStepRequest) -> tuple[BusinessContextAllocation, ...]:
    allocations = tuple(
        allocation
        for allocation in request.step.allocations
        if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
    )
    if len(allocations) != len(request.step.allocations):
        raise ExecutionUnsupportedStepError("Customer Recharge execution step contains non-recharge allocations.")
    if tuple(allocation.allocation_key for allocation in allocations) != request.step.allocation_keys:
        raise ExecutionUnsupportedStepError("Customer Recharge allocation context does not match execution step.")
    return allocations


def _require_existing_customer_invoices(allocations: tuple[BusinessContextAllocation, ...]) -> None:
    if not allocations or any(allocation.customer_invoice_id is None for allocation in allocations):
        raise CustomerRechargeInvoiceCreationRequiredError(
            "Customer Recharge execution requires an existing customer invoice reference."
        )


def _customer_invoice_artifacts(allocations: tuple[BusinessContextAllocation, ...]) -> tuple[ExecutionArtifact, ...]:
    invoice_ids = sorted(
        {allocation.customer_invoice_id for allocation in allocations if allocation.customer_invoice_id is not None}
    )
    return tuple(
        ExecutionArtifact(
            artifact_type=ExecutionArtifactType.CUSTOMER_INVOICE,
            artifact_id=str(invoice_id),
            external_identity=f"account.move:{invoice_id}",
            created=False,
        )
        for invoice_id in invoice_ids
    )


def _message(mode: ExecutionMode) -> str:
    if mode is ExecutionMode.DRY_RUN:
        return "Customer Recharge dry run completed. No ERP write was performed."
    return "Customer Recharge associated with existing customer invoice evidence."
