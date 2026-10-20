"""INTERNAL_USE gate at Vendor Bill decision acceptance for product-backed invoices.

A product-backed review (at least one line carries a product identifier) whose
current-version purchase purpose is INTERNAL_USE must not be booked to the product
category's purchase account: that account is configured for RESALE. This gate makes
the operator's explicit, review-scoped ``EXPENSE_ACCOUNT`` accounting resolution
mandatory for such a decision and freezes it as an :class:`InternalUseAccountingPin`.

Scope is deliberately narrow:

* only the *current-version* purpose counts; an INTERNAL_USE purpose recorded for any
  other version never carries over and fails closed (like RESALE);
* identifier-free (operating-expense / fixed-asset shaped) invoices are untouched --
  they already post to their review-scoped account through the existing
  operating-expense and CAPITALIZE_FIXED_ASSET paths;
* every line must be product-backed by exactly one matched Odoo product; account-only
  lines are rejected, never mixed with the pinned account;
* the accounting resolution must exist for the same review version, be an
  ``EXPENSE_ACCOUNT`` treatment, and its account must still be an eligible,
  company-scoped expense account (read-only re-validation through the same reader the
  accounting-resolution use case uses). The account is never taken from Odoo's
  product/category configuration and never from the decision request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.application.exceptions import ApplicationError
from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.workbench.accounting_resolution import AccountingTreatmentType, ReviewAccountingResolution
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.exceptions import InternalUseDecisionEligibilityError, WorkbenchContractError
from app.application.workbench.expense_account_lookup import ExpenseAccountCandidate
from app.application.workbench.internal_use_accounting_pin import (
    InternalUseAccountingLinePin,
    InternalUseAccountingPin,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import PurchasePurposeHistoryReader
from app.matching import ProductMatchStatus

if TYPE_CHECKING:
    from app.application.execution.contracts import ExecutionSourceInvoice


class AccountingResolutionVersionReader(Protocol):
    """Read-only port: the accounting resolution recorded for one exact review version."""

    def find_accounting_resolution(
        self,
        *,
        review_id: str,
        company_id: int,
        review_version: int,
    ) -> ReviewAccountingResolution | None:
        pass


class EligibleExpenseAccountReader(Protocol):
    """Read-only port: ``ExpenseAccountCandidateReader.find_eligible_by_id``."""

    def find_eligible_by_id(self, *, company_id: int, account_id: int) -> ExpenseAccountCandidate | None:
        pass


class InternalUseDecisionGate:
    """Fail-closed INTERNAL_USE accounting check for one fresh product-backed Vendor Bill decision."""

    def __init__(
        self,
        *,
        purpose_reader: PurchasePurposeHistoryReader,
        accounting_resolution_reader: AccountingResolutionVersionReader,
        expense_account_reader: EligibleExpenseAccountReader,
    ) -> None:
        self._purpose_reader = purpose_reader
        self._accounting_resolution_reader = accounting_resolution_reader
        self._expense_account_reader = expense_account_reader

    def enforce(
        self, command: ReviewDecisionCommand, evidence: ExecutionSourceInvoice
    ) -> InternalUseAccountingPin | None:
        """``None`` unless the current-version purpose is INTERNAL_USE on a product-backed
        invoice; otherwise the accepted pin, or ``InternalUseDecisionEligibilityError``."""

        if not isinstance(command, ReviewDecisionCommand):
            raise WorkbenchContractError("ReviewDecisionCommand is required.")
        if not self._current_purpose_is_internal_use(command):
            return None
        if not invoice_has_product_identifier(evidence.invoice):
            return None
        if any(resolution.account_only for resolution in command.line_resolutions):
            raise InternalUseDecisionEligibilityError(
                "INTERNAL_USE decisions for product invoices do not allow account-only line resolutions."
            )
        product_by_line = _resolved_product_by_line(evidence)
        resolution = self._current_expense_resolution(command)
        account = self._eligible_account(command.company_id, resolution)
        assert resolution.id is not None and resolution.expense_category is not None
        return InternalUseAccountingPin(
            review_version=command.expected_version,
            accounting_resolution_id=resolution.id,
            expense_account_id=account.id,
            expense_account_code=account.code,
            expense_account_name=account.name,
            expense_account_type=account.account_type,
            expense_category=resolution.expense_category,
            lines=tuple(
                InternalUseAccountingLinePin(line_number=line_number, product_id=product_id)
                for line_number, product_id in product_by_line.items()
            ),
        )

    def _current_purpose_is_internal_use(self, command: ReviewDecisionCommand) -> bool:
        resolutions = self._purpose_reader.list_purchase_purpose_resolutions(
            review_id=command.review_id,
            company_id=command.company_id,
        )
        current = [resolution for resolution in resolutions if resolution.review_version == command.expected_version]
        if len(current) > 1:
            raise InternalUseDecisionEligibilityError("More than one purchase purpose exists for this review version.")
        return bool(current) and current[0].purchase_purpose is PurchasePurpose.INTERNAL_USE

    def _current_expense_resolution(self, command: ReviewDecisionCommand) -> ReviewAccountingResolution:
        resolution = self._accounting_resolution_reader.find_accounting_resolution(
            review_id=command.review_id,
            company_id=command.company_id,
            review_version=command.expected_version,
        )
        if resolution is None:
            raise InternalUseDecisionEligibilityError(
                "INTERNAL_USE requires an explicitly selected internal expense account for this review version; "
                "record the accounting resolution (expense account) first. The product category's purchase "
                "account is never used for INTERNAL_USE."
            )
        if resolution.treatment_type is not AccountingTreatmentType.EXPENSE_ACCOUNT:
            raise InternalUseDecisionEligibilityError(
                "INTERNAL_USE product invoices support only the expense_account accounting treatment."
            )
        if type(resolution.id) is not int or resolution.id <= 0 or resolution.expense_account_id is None:
            raise InternalUseDecisionEligibilityError("The INTERNAL_USE accounting resolution is incomplete.")
        return resolution

    def _eligible_account(self, company_id: int, resolution: ReviewAccountingResolution) -> ExpenseAccountCandidate:
        assert resolution.expense_account_id is not None
        try:
            account = self._expense_account_reader.find_eligible_by_id(
                company_id=company_id, account_id=resolution.expense_account_id
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001 - any read failure fails the decision closed
            raise InternalUseDecisionEligibilityError(
                "The selected internal expense account could not be verified safely."
            ) from exc
        if account is None or account.id != resolution.expense_account_id:
            raise InternalUseDecisionEligibilityError(
                "The selected internal expense account no longer exists, is not an eligible expense account, "
                "or is not scoped to this company."
            )
        return account


def _resolved_product_by_line(evidence: ExecutionSourceInvoice) -> dict[str, int]:
    results_by_line: dict[str | None, list] = {}
    for line_result in evidence.product_match.line_results:
        results_by_line.setdefault(line_result.line_number, []).append(line_result.result)

    product_by_line: dict[str, int] = {}
    unresolved: list[str] = []
    for line in evidence.invoice.lines:
        results = results_by_line.get(line.line_number, [])
        if (
            isinstance(line.line_number, str)
            and line.line_number.strip()
            and len(results) == 1
            and results[0].status is ProductMatchStatus.MATCHED
            and type(results[0].product_id) is int
            and results[0].product_id > 0
        ):
            product_by_line[line.line_number] = results[0].product_id
        else:
            unresolved.append(str(line.line_number))
    if unresolved:
        raise InternalUseDecisionEligibilityError(
            "INTERNAL_USE product invoices require every line to resolve to exactly one existing Odoo product; "
            "unresolved lines: " + ", ".join(unresolved) + "."
        )
    return product_by_line


__all__ = [
    "AccountingResolutionVersionReader",
    "EligibleExpenseAccountReader",
    "InternalUseDecisionGate",
]
