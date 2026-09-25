from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING

from app.application.exceptions import ApplicationError
from app.application.services import UnitOfWork
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
from app.application.workbench.resale_accounting_pin import ResaleAccountingPin
from app.application.workbench.resale_decision_gate import ResaleDecisionGate
from app.application.workbench.selected_expense_account_resolution import (
    selected_expense_account_ids,
    validate_selected_expense_accounts,
)
from app.application.workbench.selected_product_resolution import (
    apply_selected_product_resolutions,
    selected_product_ids,
)
from app.application.workflow import WorkflowType
from app.billing.builder import validate_vendor_bill_inputs
from app.billing.dto import CustomerInvoiceBillingInstruction

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice


class SubmitReviewDecisionUseCase:
    """Application boundary for explicit Workbench review decision submission."""

    def __init__(
        self,
        *,
        review_decision_writer: ReviewDecisionWriter,
        unit_of_work: UnitOfWork,
        execution_evidence_reader: ReviewExecutionEvidenceReader | None = None,
        billing_evidence_reader: ReviewBillingEvidenceReader | None = None,
        selected_product_reader: SelectedProductReader | None = None,
        selected_account_reader: SelectedAccountReader | None = None,
        resale_decision_gate: ResaleDecisionGate | None = None,
    ) -> None:
        self._review_decision_writer = review_decision_writer
        self._unit_of_work = unit_of_work
        self._execution_evidence_reader = execution_evidence_reader
        self._billing_evidence_reader = billing_evidence_reader
        self._selected_product_reader = selected_product_reader
        self._selected_account_reader = selected_account_reader
        self._resale_decision_gate = resale_decision_gate

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
            is_replay = _translate_decision_failure(
                lambda: self.has_matching_decision(command), "Review decision replay could not be checked safely."
            )
            # A replay never re-runs the RESALE gate or re-pins: the accepted decision keeps the
            # RESALE accounting pin it was accepted with (P0-PROD-18F-1).
            resale_pin: ResaleAccountingPin | None = None
            if not is_replay:
                _validate_resolved_execution_inputs(command, evidence)
                resale_pin = self._enforce_resale_eligibility(command, evidence)
            # Only passed when present, so every non-RESALE write call is byte-identical to before.
            pin_kwargs = {"resale_accounting_pin": resale_pin} if resale_pin is not None else {}
            if requires_billing_evidence:
                billing_instructions = self._billing_instructions(command)
                return self._write_and_commit(
                    lambda: self._review_decision_writer.submit_review_decision_with_execution_and_billing_evidence(
                        command,
                        evidence,
                        billing_instructions,
                        **pin_kwargs,
                    )
                )
            return self._write_and_commit(
                lambda: self._review_decision_writer.submit_review_decision_with_execution_evidence(
                    command,
                    evidence,
                    **pin_kwargs,
                )
            )
        if requires_billing_evidence:
            raise ReviewDecisionError("Execution source evidence is required for Customer Invoice creation decisions.")
        return self._write_and_commit(lambda: self._review_decision_writer.submit_review_decision(command))

    def _write_and_commit(
        self, operation: Callable[[], ReviewDecisionAcknowledgement]
    ) -> ReviewDecisionAcknowledgement:
        """Single transaction boundary for one decision write.

        Each of the three ``ReviewDecisionWriter`` write methods already stages its
        own record set (review item version/status advance, decision row, optionally
        execution/billing evidence rows) atomically as one internal nested unit of
        work -- but staging pending changes only makes them visible to code sharing
        the same persistence context; it never durably commits the outer request
        transaction. Without an explicit commit here, the request-scoped persistence
        context is discarded once the request completes, while the acknowledgement
        returned to the caller is built from the in-memory (staged-but-uncommitted)
        result -- so the API can report HTTP 200/accepted=true for a decision that
        was never durably persisted, and a second stale-``expected_version`` request
        can appear to succeed identically rather than hit the intended optimistic-
        concurrency conflict. See P0-PROD-15AB.
        """

        try:
            result = _translate_decision_failure(operation, "Review decision submission failed.")
        except BaseException:
            self._unit_of_work.rollback()
            raise
        self._unit_of_work.commit()
        return result

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

    def _enforce_resale_eligibility(self, command: ReviewDecisionCommand, evidence) -> ResaleAccountingPin | None:
        """Gate a fresh Vendor Bill decision on RESALE product eligibility (P0-PROD-18E-1B).

        Runs after selected products are applied, so the operator's explicit
        ``selected_product_id`` is what gets checked. A no-op (``None``) unless the
        review's current-version purchase purpose is RESALE; read-only. Returns the
        accepted evidence to pin with the decision (P0-PROD-18F-1).
        """

        if self._resale_decision_gate is None:
            return None
        return _translate_decision_failure(
            lambda: self._resale_decision_gate.enforce(command, evidence),
            "RESALE product eligibility could not be checked safely.",
        )

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


def _validate_resolved_execution_inputs(
    command: ReviewDecisionCommand,
    evidence: ExecutionSourceInvoice,
) -> None:
    """Validate fresh decisions; historical replay keeps its original contract."""

    invoice_lines = {line.line_number for line in evidence.invoice.lines}
    if any(resolution.line_number not in invoice_lines for resolution in command.line_resolutions):
        raise ReviewDecisionError("Line resolution references an unknown invoice line.")
    account_only_lines = frozenset(
        resolution.line_number for resolution in command.line_resolutions if resolution.account_only
    )
    explicit_accounts = {
        resolution.line_number: resolution.expense_account_id
        for resolution in command.line_resolutions
        if resolution.account_only and resolution.expense_account_id is not None
    }
    if account_only_lines - explicit_accounts.keys():
        raise ReviewDecisionError("New account-only decisions require an explicit expense_account_id.")
    validation = validate_vendor_bill_inputs(
        evidence.invoice,
        evidence.partner_match,
        evidence.product_match,
        evidence.tax_match,
        company_id=command.company_id,
        operating_expense_match=evidence.operating_expense_match,
        account_only_line_numbers=account_only_lines,
        explicit_account_only_accounts=explicit_accounts,
    )
    if not validation.is_valid:
        raise ReviewDecisionError("Vendor Bill decision requires complete resolved execution inputs.")
