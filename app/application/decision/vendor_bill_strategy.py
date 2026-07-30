from __future__ import annotations

from time import perf_counter

from app.application.commands import ImportInvoiceCommand, VendorBillWriteCommand
from app.application.decision.exceptions import UnsupportedWorkflowError
from app.application.dto import DecisionResult, RuleEvaluationResult, VendorBillWriteResult
from app.application.ports import VendorBillWriter
from app.application.workflow import WorkflowType
from app.billing import VendorBillBuilder


class VendorBillStrategy:
    """Direct Vendor Bill workflow execution strategy."""

    workflow = WorkflowType.VENDOR_BILL
    name = WorkflowType.VENDOR_BILL.value

    def __init__(self, *, vendor_bill_builder: VendorBillBuilder, vendor_bill_writer: VendorBillWriter) -> None:
        self._vendor_bill_builder = vendor_bill_builder
        self._vendor_bill_writer = vendor_bill_writer

    async def execute(self, command: ImportInvoiceCommand, rule_result: RuleEvaluationResult) -> DecisionResult:
        started = perf_counter()
        _validate_rule_result(rule_result)
        assert rule_result.partner_match is not None
        assert rule_result.product_match is not None
        assert rule_result.tax_match is not None
        vendor_bill = self._vendor_bill_builder.build(
            command.invoice,
            rule_result.partner_match,
            rule_result.product_match,
            rule_result.tax_match,
        )
        write_result = await self._vendor_bill_writer.write_vendor_bill(
            VendorBillWriteCommand(
                vendor_bill=vendor_bill,
                idempotency_key=command.idempotency_key,
                dry_run=command.dry_run,
                approved_by=command.approved_by,
            )
        )
        return _decision_from_write(
            invoice_id=command.invoice.header.ettn or command.invoice.header.invoice_uuid,
            write_result=write_result,
            duration=perf_counter() - started,
        )


def _validate_rule_result(rule_result: RuleEvaluationResult) -> None:
    if rule_result.workflow != WorkflowType.VENDOR_BILL:
        raise UnsupportedWorkflowError(f"VendorBillStrategy cannot execute workflow: {rule_result.workflow.value}.")
    if rule_result.partner_match is None or rule_result.product_match is None or rule_result.tax_match is None:
        raise UnsupportedWorkflowError("Vendor Bill workflow requires matching rule outputs.")


def _decision_from_write(
    *,
    invoice_id: str,
    write_result: VendorBillWriteResult,
    duration: float,
) -> DecisionResult:
    success = write_result.status in {"dry_run", "created", "existing"}
    status = "already_exists" if write_result.status == "existing" else write_result.status
    return DecisionResult(
        success=success,
        invoice_id=invoice_id,
        workflow=WorkflowType.VENDOR_BILL,
        strategy=WorkflowType.VENDOR_BILL.value,
        status=status,
        vendor_bill_id=write_result.external_id,
        warnings=_warnings(write_result) if success else (),
        errors=() if success else _errors(write_result),
        duration=duration,
    )


def _warnings(write_result: VendorBillWriteResult) -> tuple[str, ...]:
    if write_result.warnings:
        return write_result.warnings
    return (write_result.safe_message,) if write_result.safe_message else ()


def _errors(write_result: VendorBillWriteResult) -> tuple[str, ...]:
    if write_result.errors:
        return write_result.errors
    return (write_result.safe_message,) if write_result.safe_message else ("Vendor Bill write operation failed.",)
