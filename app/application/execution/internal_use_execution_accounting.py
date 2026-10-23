"""INTERNAL_USE product-invoice Vendor Bill execution account safety.

Runs inside Vendor Bill execution, before the first Odoo Vendor Bill write, and is the
only issuer of :class:`~app.billing.ValidatedInternalUseLineAccount`. For a decision
accepted under an INTERNAL_USE purchase purpose on a product-backed invoice it:

1. loads the decision's immutable INTERNAL_USE accounting pin and fails closed if it is
   missing, corrupt, for another review version, or does not describe exactly this
   decision's lines and products -- such a decision never falls back to the product
   category's (RESALE) account;
2. re-reads the pinned expense account through the same read-only eligibility reader
   the accounting resolution used, and fails closed if it is no longer an eligible,
   company-scoped expense account or its code/type drifted from the pin;
3. only then issues one validated account per line, whose ``account_id`` is the pin's.

It never updates the pin, never takes an account from a request and never writes to
Odoo. Every other decision -- RESALE, identifier-free INTERNAL_USE (operating-expense /
fixed-asset paths), or no purpose -- gets ``None`` and reads nothing from Odoo here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from app.application.exceptions import ApplicationError
from app.application.execution.contracts import ExecutionSourceInvoice
from app.application.execution.exceptions import ExecutionSourceInvoiceError, InternalUseExecutionAccountingError
from app.application.expense_mapping.predicates import invoice_has_product_identifier
from app.application.workbench.internal_use_accounting_pin import (
    InternalUseAccountingLinePin,
    InternalUseAccountingPin,
)
from app.application.workbench.internal_use_decision_gate import EligibleExpenseAccountReader
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_decision_gate import PurchasePurposeHistoryReader
from app.billing import ValidatedInternalUseLineAccount, issue_validated_internal_use_line_account
from app.matching import ProductMatchStatus


class InternalUseAccountingPinReader(Protocol):
    """Hub-persistence-only read of an accepted decision's INTERNAL_USE pin. No Odoo access."""

    def get_internal_use_accounting_pin(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> InternalUseAccountingPin | None:
        pass


def load_decision_internal_use_pin(
    *,
    pin_reader: InternalUseAccountingPinReader,
    purpose_reader: PurchasePurposeHistoryReader,
    source: ExecutionSourceInvoice,
) -> InternalUseAccountingPin | None:
    """The decision's own pin if it was accepted under INTERNAL_USE on a product invoice, else ``None``.

    INTERNAL_USE-ness is the purchase purpose of the version the decision was accepted on
    (``decision_version - 1``) -- exactly what the decision gate evaluated.
    """

    accepted_on_version = source.decision_version - 1
    try:
        resolutions = purpose_reader.list_purchase_purpose_resolutions(
            review_id=source.review_id, company_id=source.company_id
        )
    except ApplicationError as exc:
        raise InternalUseExecutionAccountingError("The purchase purpose could not be read safely.") from exc
    current = [resolution for resolution in resolutions if resolution.review_version == accepted_on_version]
    if len(current) > 1:
        raise InternalUseExecutionAccountingError("More than one purchase purpose exists for the decision version.")
    is_internal_use = bool(current) and current[0].purchase_purpose is PurchasePurpose.INTERNAL_USE
    try:
        pin = pin_reader.get_internal_use_accounting_pin(
            review_id=source.review_id, company_id=source.company_id, decision_version=source.decision_version
        )
    except ExecutionSourceInvoiceError as exc:
        raise InternalUseExecutionAccountingError("The INTERNAL_USE accounting pin is invalid.") from exc
    if not is_internal_use:
        if pin is not None:
            raise InternalUseExecutionAccountingError(
                "An INTERNAL_USE accounting pin exists for a decision not accepted under an INTERNAL_USE purpose."
            )
        return None
    if not invoice_has_product_identifier(source.invoice):
        # Identifier-free INTERNAL_USE: the existing operating-expense / fixed-asset path owns it.
        if pin is not None:
            raise InternalUseExecutionAccountingError(
                "An INTERNAL_USE accounting pin exists for an identifier-free invoice."
            )
        return None
    if pin is None:
        raise InternalUseExecutionAccountingError(
            "This INTERNAL_USE product decision has no pinned internal expense account (accepted before "
            "INTERNAL_USE pinning); the product category's account is never used. Accept a new decision."
        )
    if pin.review_version != accepted_on_version:
        raise InternalUseExecutionAccountingError("The INTERNAL_USE accounting pin belongs to another review version.")
    return pin


class InternalUseExecutionAccountingValidator:
    """Fail-closed pre-write validation for INTERNAL_USE product-invoice Vendor Bill execution."""

    def __init__(
        self,
        *,
        pin_reader: InternalUseAccountingPinReader,
        purpose_reader: PurchasePurposeHistoryReader,
        expense_account_reader: EligibleExpenseAccountReader,
    ) -> None:
        self._pin_reader = pin_reader
        self._purpose_reader = purpose_reader
        self._expense_account_reader = expense_account_reader

    def validate(self, source: ExecutionSourceInvoice) -> Mapping[str, ValidatedInternalUseLineAccount] | None:
        """``None`` unless INTERNAL_USE on a product invoice; otherwise one validated account per line."""

        pin = load_decision_internal_use_pin(
            pin_reader=self._pin_reader, purpose_reader=self._purpose_reader, source=source
        )
        if pin is None:
            return None
        pinned_lines = pinned_lines_for_source(pin, source)
        self._require_account_unchanged(source.company_id, pin)
        return {
            line_number: issue_validated_internal_use_line_account(
                line_number=line_number, product_id=pinned.product_id, account_id=pin.expense_account_id
            )
            for line_number, pinned in pinned_lines.items()
        }

    def _require_account_unchanged(self, company_id: int, pin: InternalUseAccountingPin) -> None:
        try:
            account = self._expense_account_reader.find_eligible_by_id(
                company_id=company_id, account_id=pin.expense_account_id
            )
        except Exception as exc:  # any read failure (ERP transport/response included) fails closed
            raise InternalUseExecutionAccountingError(
                "INTERNAL_USE execution blocked: the pinned expense account could not be read safely."
            ) from exc
        if account is None or account.id != pin.expense_account_id:
            raise InternalUseExecutionAccountingError(
                f"INTERNAL_USE execution blocked: expense account {pin.expense_account_id} is no longer an eligible "
                "company expense account; accept a new decision."
            )
        drifted = [
            name
            for name, current, pinned in (
                ("account_code", account.code, pin.expense_account_code),
                ("account_type", account.account_type, pin.expense_account_type),
            )
            if current != pinned
        ]
        if drifted:
            raise InternalUseExecutionAccountingError(
                f"INTERNAL_USE execution blocked: expense account {pin.expense_account_id} drifted from the accepted "
                f"pin ({', '.join(drifted)}); accept a new decision."
            )


def pinned_lines_for_source(
    pin: InternalUseAccountingPin, source: ExecutionSourceInvoice
) -> dict[str, InternalUseAccountingLinePin]:
    """The pin per invoice line, proven to describe exactly this decision's lines/products."""

    pinned_by_line = pin.by_line_number()
    source_line_numbers = [line.line_number for line in source.invoice.lines]
    if len(set(source_line_numbers)) != len(source_line_numbers) or set(source_line_numbers) != set(pinned_by_line):
        raise InternalUseExecutionAccountingError(
            "The INTERNAL_USE accounting pin does not cover exactly every invoice line."
        )
    if any(resolution.account_only for resolution in source.line_resolutions):
        raise InternalUseExecutionAccountingError("An INTERNAL_USE product decision cannot contain account-only lines.")
    results_by_line: dict[str | None, list] = {}
    for line_result in source.product_match.line_results:
        results_by_line.setdefault(line_result.line_number, []).append(line_result.result)
    for line_number, pinned in pinned_by_line.items():
        results = results_by_line.get(line_number, [])
        if (
            len(results) != 1
            or results[0].status is not ProductMatchStatus.MATCHED
            or results[0].product_id != pinned.product_id
        ):
            raise InternalUseExecutionAccountingError(
                f"The INTERNAL_USE accounting pin for line {line_number} does not match the decision's product."
            )
    return {line_number: pinned_by_line[line_number] for line_number in source_line_numbers}


__all__ = [
    "InternalUseAccountingPinReader",
    "InternalUseExecutionAccountingValidator",
    "load_decision_internal_use_pin",
    "pinned_lines_for_source",
]
