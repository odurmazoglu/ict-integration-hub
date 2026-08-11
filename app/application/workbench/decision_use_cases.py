from __future__ import annotations

from collections.abc import Callable

from app.application.exceptions import ApplicationError
from app.application.workbench.allocations import BusinessContextAllocationType
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewDecisionType
from app.application.workbench.exceptions import ReviewDecisionError, WorkbenchContractError
from app.application.workbench.ports import (
    ReviewBillingEvidenceReader,
    ReviewDecisionWriter,
    ReviewExecutionEvidenceReader,
)
from app.application.workflow import WorkflowType
from app.billing.dto import CustomerInvoiceBillingInstruction


class SubmitReviewDecisionUseCase:
    """Application boundary for explicit Workbench review decision submission."""

    def __init__(
        self,
        *,
        review_decision_writer: ReviewDecisionWriter,
        execution_evidence_reader: ReviewExecutionEvidenceReader | None = None,
        billing_evidence_reader: ReviewBillingEvidenceReader | None = None,
    ) -> None:
        self._review_decision_writer = review_decision_writer
        self._execution_evidence_reader = execution_evidence_reader
        self._billing_evidence_reader = billing_evidence_reader

    def execute(self, command: ReviewDecisionCommand) -> ReviewDecisionAcknowledgement:
        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        requires_execution_evidence = _requires_execution_evidence(command)
        requires_billing_evidence = _requires_billing_evidence(command)
        if requires_execution_evidence:
            if self._execution_evidence_reader is None:
                raise ReviewDecisionError("Execution source evidence is required for Vendor Bill decisions.")
            evidence = _translate_decision_failure(
                lambda: self._execution_evidence_reader.get_evidence(
                    review_id=command.review_id,
                    company_id=command.company_id,
                    expected_version=command.expected_version,
                ),
                "Execution source evidence could not be loaded safely.",
            )
            if requires_billing_evidence:
                billing_instructions = self._billing_instructions(command)
                return _translate_decision_failure(
                    lambda: self._review_decision_writer.submit_review_decision_with_execution_and_billing_evidence(
                        command,
                        evidence,
                        billing_instructions,
                    ),
                    "Review decision submission failed.",
                )
            return _translate_decision_failure(
                lambda: self._review_decision_writer.submit_review_decision_with_execution_evidence(
                    command,
                    evidence,
                ),
                "Review decision submission failed.",
            )
        if requires_billing_evidence:
            raise ReviewDecisionError("Execution source evidence is required for Customer Invoice creation decisions.")
        return _translate_decision_failure(
            lambda: self._review_decision_writer.submit_review_decision(command),
            "Review decision submission failed.",
        )

    def _billing_instructions(
        self,
        command: ReviewDecisionCommand,
    ) -> tuple[CustomerInvoiceBillingInstruction, ...]:
        if self._billing_evidence_reader is None:
            raise ReviewDecisionError("Customer billing evidence is required for Customer Invoice creation decisions.")
        billing_instructions = _translate_decision_failure(
            lambda: self._billing_evidence_reader.get_billing_instructions(
                review_id=command.review_id,
                company_id=command.company_id,
                review_version=command.expected_version,
            ),
            "Customer billing evidence could not be loaded safely.",
        )
        _validate_billing_coverage(command, billing_instructions)
        return billing_instructions


def _translate_decision_failure[ResultT](operation: Callable[[], ResultT], fallback_message: str) -> ResultT:
    try:
        return operation()
    except ApplicationError:
        raise
    except Exception as exc:
        raise ReviewDecisionError(fallback_message) from exc


def _requires_execution_evidence(command: ReviewDecisionCommand) -> bool:
    return (
        command.decision is ReviewDecisionType.SELECT_WORKFLOW and command.selected_workflow is WorkflowType.VENDOR_BILL
    )


def _requires_billing_evidence(command: ReviewDecisionCommand) -> bool:
    allocations = command.business_context_allocations
    if command.decision is not ReviewDecisionType.SELECT_WORKFLOW or allocations is None:
        return False
    return any(
        allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
        and allocation.customer_invoice_id is None
        for allocation in allocations.allocations
    )


def _validate_billing_coverage(
    command: ReviewDecisionCommand,
    billing_instructions: tuple[CustomerInvoiceBillingInstruction, ...],
) -> None:
    allocations = command.business_context_allocations
    if allocations is None:
        raise ReviewDecisionError("Customer billing evidence requires allocation evidence.")
    creation_allocations = tuple(
        allocation
        for allocation in allocations.allocations
        if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
        and allocation.customer_invoice_id is None
    )
    existing_invoice_keys = {
        allocation.allocation_key
        for allocation in allocations.allocations
        if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
        and allocation.customer_invoice_id is not None
    }
    allocation_by_key = {allocation.allocation_key: allocation for allocation in creation_allocations}
    covered: list[str] = []
    for instruction in billing_instructions:
        for line in instruction.lines:
            if line.allocation_key in existing_invoice_keys:
                raise ReviewDecisionError("Existing-invoice allocations must not have creation billing evidence.")
            allocation = allocation_by_key.get(line.allocation_key)
            if allocation is None:
                raise ReviewDecisionError("Customer billing evidence references an unknown allocation.")
            if allocation.recharge_partner_id != instruction.customer_id:
                raise ReviewDecisionError("Billing customer must match allocation recharge_partner_id.")
            covered.append(line.allocation_key)
    if len(set(covered)) != len(covered):
        raise ReviewDecisionError("Customer billing evidence duplicates allocation coverage.")
    if set(covered) != set(allocation_by_key):
        raise ReviewDecisionError("Customer billing evidence must cover every creation allocation exactly.")
