from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.application.exceptions import ApplicationError
from app.application.workbench.allocations import BusinessContextAllocationType
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import ReviewDecisionAcknowledgement, ReviewDecisionType
from app.application.workbench.exceptions import ReviewDecisionError, WorkbenchContractError
from app.application.workbench.ports import (
    ReviewBillingEvidenceReader,
    ReviewDecisionWriter,
    ReviewExecutionEvidenceReader,
    SelectedAccountReader,
    SelectedProductReader,
)
from app.application.workbench.selected_expense_account_resolution import (
    selected_expense_account_ids,
    validate_selected_expense_accounts,
)
from app.application.workbench.selected_product_resolution import (
    apply_selected_product_resolutions,
    selected_product_ids,
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
        selected_product_reader: SelectedProductReader | None = None,
        selected_account_reader: SelectedAccountReader | None = None,
    ) -> None:
        self._review_decision_writer = review_decision_writer
        self._execution_evidence_reader = execution_evidence_reader
        self._billing_evidence_reader = billing_evidence_reader
        self._selected_product_reader = selected_product_reader
        self._selected_account_reader = selected_account_reader

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
            evidence = self._apply_selected_product_resolutions(command, evidence)
            self._validate_selected_expense_accounts(command)
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

    def has_matching_decision(self, command: ReviewDecisionCommand) -> bool:
        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        return self._review_decision_writer.has_matching_review_decision(command)

    def _apply_selected_product_resolutions(self, command: ReviewDecisionCommand, evidence):
        """Validate and pin any explicit ``LineResolution.selected_product_id`` overrides.

        The read-only lookup happens here, once, at decision-acceptance time -- never
        during Vendor Bill execution. A no-op when no line names an explicit selection.
        """

        product_ids = selected_product_ids(command.line_resolutions)
        if not product_ids:
            return evidence
        if self._selected_product_reader is None:
            raise ReviewDecisionError("Selected product resolution is required but not configured.")
        products = _translate_decision_failure(
            lambda: self._selected_product_reader.find_products_by_ids(product_ids),
            "Selected product evidence could not be loaded safely.",
        )
        products_by_id = {product.id: product for product in products}
        new_product_match = apply_selected_product_resolutions(
            evidence.product_match,
            line_resolutions=command.line_resolutions,
            company_id=command.company_id,
            products_by_id=products_by_id,
        )
        return replace(evidence, product_match=new_product_match)

    def _validate_selected_expense_accounts(self, command: ReviewDecisionCommand) -> None:
        """Validate any explicit ``LineResolution.expense_account_id`` overrides (P0-PROD-08G).

        Read-only lookup at decision-acceptance time only -- never during Vendor Bill
        execution. There is nothing to substitute: the operator's own account id is
        never replaced, only proven real and company-scoped before the decision is
        persisted. A no-op when no line names an explicit expense account.
        """

        account_ids = selected_expense_account_ids(command.line_resolutions)
        if not account_ids:
            return
        if self._selected_account_reader is None:
            raise ReviewDecisionError("Selected expense account resolution is required but not configured.")
        accounts = _translate_decision_failure(
            lambda: self._selected_account_reader.find_accounts_by_ids(account_ids),
            "Selected expense account evidence could not be loaded safely.",
        )
        accounts_by_id = {account.id: account for account in accounts}
        validate_selected_expense_accounts(
            line_resolutions=command.line_resolutions,
            company_id=command.company_id,
            accounts_by_id=accounts_by_id,
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
