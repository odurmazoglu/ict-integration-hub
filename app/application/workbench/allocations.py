from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum, StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError

ALLOCATION_AMOUNT_PRECISION = 24
ALLOCATION_AMOUNT_SCALE = 6
ALLOCATION_AMOUNT_INTEGER_DIGITS = ALLOCATION_AMOUNT_PRECISION - ALLOCATION_AMOUNT_SCALE
MAX_ALLOCATION_DESCRIPTION_LENGTH = 500
MAX_ALLOCATION_NOTE_LENGTH = 1000
ONE_HUNDRED = Decimal("100")


class BusinessContextAllocationType(StrEnum):
    """Canonical business purpose for one allocation line."""

    SALES_ORDER_COST = "sales_order_cost"
    CUSTOMER_RECHARGE = "customer_recharge"
    EXISTING_PURCHASE_ORDER = "existing_purchase_order"
    NEW_RFQ_PURCHASE = "new_rfq_purchase"
    PROJECT_COST = "project_cost"
    OPERATING_EXPENSE = "operating_expense"
    FIXED_ASSET = "fixed_asset"
    SUBSCRIPTION_SERVICE = "subscription_service"
    INTERNAL_COST = "internal_cost"


class AllocationCompleteness(StrEnum):
    """Whether an allocation set is intended to reconcile the full invoice."""

    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class BusinessContextAllocation(ApplicationDTO):
    """Immutable ERP-neutral allocation of invoice cost to business context."""

    allocation_key: str
    allocation_type: BusinessContextAllocationType
    source_line_number: str | None = None
    description: str | None = None
    amount: Decimal | None = None
    percentage: Decimal | None = None
    currency: str | None = None
    customer_id: int | None = None
    recharge_partner_id: int | None = None
    target_company_id: int | None = None
    opportunity_id: int | None = None
    sales_order_id: int | None = None
    sales_order_line_id: int | None = None
    proposal_scenario_id: int | None = None
    purchase_order_id: int | None = None
    project_id: int | None = None
    analytic_account_id: int | None = None
    subscription_id: int | None = None
    internal_note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.allocation_key, "allocation_key is required.")
        _require_enum(
            self.allocation_type,
            BusinessContextAllocationType,
            "allocation_type must be a canonical BusinessContextAllocationType.",
        )
        if self.source_line_number is not None:
            _require_text(self.source_line_number, "source_line_number must be non-empty when supplied.")
        _validate_text_length(
            self.description,
            MAX_ALLOCATION_DESCRIPTION_LENGTH,
            "description exceeds maximum length.",
        )
        _validate_text_length(self.internal_note, MAX_ALLOCATION_NOTE_LENGTH, "internal_note exceeds maximum length.")
        amount = _optional_positive_decimal(
            self.amount,
            "amount must be a finite Decimal value.",
            "amount must be greater than zero.",
        )
        percentage = _optional_positive_decimal(
            self.percentage,
            "percentage must be a finite Decimal value.",
            "percentage must be greater than zero.",
        )
        if amount is None and percentage is None:
            raise WorkbenchContractError("amount or percentage is required.")
        if percentage is not None and percentage > ONE_HUNDRED:
            raise WorkbenchContractError("percentage must be at most 100.")
        object.__setattr__(self, "currency", _canonical_currency(self.currency))
        _validate_positive_ids(self)
        _validate_type_specific_context(self)


@dataclass(frozen=True, slots=True)
class BusinessContextAllocationSet(ApplicationDTO):
    """Immutable allocation aggregate for one review decision candidate."""

    allocations: tuple[BusinessContextAllocation, ...] = field(default_factory=tuple)
    completeness: AllocationCompleteness = AllocationCompleteness.COMPLETE
    invoice_total: Decimal | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        allocations = tuple(self.allocations)
        if not allocations:
            raise WorkbenchContractError("at least one allocation is required.")
        _require_enum(self.completeness, AllocationCompleteness, "completeness must be a canonical value.")
        object.__setattr__(self, "allocations", allocations)
        object.__setattr__(self, "currency", _canonical_currency(self.currency))
        invoice_total = _optional_positive_decimal(
            self.invoice_total,
            "invoice_total must be a finite Decimal value.",
            "invoice_total must be greater than zero.",
        )
        _reject_duplicate_allocation_keys(allocations)
        _validate_currency_consistency(allocations, self.currency)
        _validate_allocation_totals(allocations, completeness=self.completeness, invoice_total=invoice_total)

    @property
    def allocated_amount_total(self) -> Decimal:
        return sum(
            (allocation.amount for allocation in self.allocations if allocation.amount is not None),
            Decimal("0"),
        )

    @property
    def allocated_percentage_total(self) -> Decimal:
        return sum(
            (allocation.percentage for allocation in self.allocations if allocation.percentage is not None),
            Decimal("0"),
        )


def _validate_type_specific_context(allocation: BusinessContextAllocation) -> None:
    if allocation.allocation_type is BusinessContextAllocationType.SALES_ORDER_COST:
        _require_positive_int(allocation.sales_order_id, "sales_order_id is required for SALES_ORDER_COST.")
    if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE:
        _require_positive_int(
            allocation.recharge_partner_id,
            "recharge_partner_id is required for CUSTOMER_RECHARGE.",
        )
    if allocation.allocation_type is BusinessContextAllocationType.EXISTING_PURCHASE_ORDER:
        _require_positive_int(
            allocation.purchase_order_id,
            "purchase_order_id is required for EXISTING_PURCHASE_ORDER.",
        )
    if (
        allocation.allocation_type is BusinessContextAllocationType.PROJECT_COST
        and allocation.project_id is None
        and allocation.analytic_account_id is None
    ):
        raise WorkbenchContractError("project_id or analytic_account_id is required for PROJECT_COST.")


def _validate_positive_ids(allocation: BusinessContextAllocation) -> None:
    for field_name, value in (
        ("customer_id", allocation.customer_id),
        ("recharge_partner_id", allocation.recharge_partner_id),
        ("target_company_id", allocation.target_company_id),
        ("opportunity_id", allocation.opportunity_id),
        ("sales_order_id", allocation.sales_order_id),
        ("sales_order_line_id", allocation.sales_order_line_id),
        ("proposal_scenario_id", allocation.proposal_scenario_id),
        ("purchase_order_id", allocation.purchase_order_id),
        ("project_id", allocation.project_id),
        ("analytic_account_id", allocation.analytic_account_id),
        ("subscription_id", allocation.subscription_id),
    ):
        if value is not None:
            _require_positive_int(value, f"{field_name} must be a positive ERP id.")


def _validate_allocation_totals(
    allocations: tuple[BusinessContextAllocation, ...],
    *,
    completeness: AllocationCompleteness,
    invoice_total: Decimal | None,
) -> None:
    amount_total = sum((allocation.amount for allocation in allocations if allocation.amount is not None), Decimal("0"))
    percentage_total = sum(
        (allocation.percentage for allocation in allocations if allocation.percentage is not None),
        Decimal("0"),
    )
    all_have_amount = all(allocation.amount is not None for allocation in allocations)
    all_have_percentage = all(allocation.percentage is not None for allocation in allocations)

    if completeness is AllocationCompleteness.COMPLETE:
        if not all_have_amount and not all_have_percentage:
            raise WorkbenchContractError("COMPLETE allocations require all lines to carry amount or percentage.")
        if all_have_amount:
            if invoice_total is None:
                raise WorkbenchContractError("invoice_total is required for COMPLETE amount allocations.")
            if amount_total != invoice_total:
                raise WorkbenchContractError("COMPLETE amount allocations must equal invoice_total.")
        if all_have_percentage and percentage_total != ONE_HUNDRED:
            raise WorkbenchContractError("COMPLETE percentage allocations must total 100.")
        return

    if completeness is AllocationCompleteness.PARTIAL:
        if invoice_total is not None and amount_total > invoice_total:
            raise WorkbenchContractError("PARTIAL amount allocations must not exceed invoice_total.")
        if percentage_total > ONE_HUNDRED:
            raise WorkbenchContractError("PARTIAL percentage allocations must not exceed 100.")


def _reject_duplicate_allocation_keys(allocations: tuple[BusinessContextAllocation, ...]) -> None:
    seen: set[str] = set()
    for allocation in allocations:
        if allocation.allocation_key in seen:
            raise WorkbenchContractError("allocation_key values must be unique.")
        seen.add(allocation.allocation_key)


def _validate_currency_consistency(
    allocations: tuple[BusinessContextAllocation, ...],
    set_currency: str | None,
) -> None:
    supplied = {allocation.currency for allocation in allocations if allocation.currency is not None}
    if set_currency is not None:
        supplied.add(set_currency)
    if len(supplied) > 1:
        raise WorkbenchContractError("allocation currencies must be consistent.")


def _optional_positive_decimal(value: Decimal | None, finite_message: str, positive_message: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError(finite_message)
    try:
        canonical = value.normalize()
    except InvalidOperation as exc:
        raise WorkbenchContractError(finite_message) from exc
    if canonical <= Decimal("0"):
        raise WorkbenchContractError(positive_message)
    if _decimal_scale(canonical) > ALLOCATION_AMOUNT_SCALE:
        raise WorkbenchContractError(f"Decimal values support at most {ALLOCATION_AMOUNT_SCALE} fractional digits.")
    if _decimal_integer_digits(canonical) > ALLOCATION_AMOUNT_INTEGER_DIGITS:
        raise WorkbenchContractError(f"Decimal values support at most {ALLOCATION_AMOUNT_PRECISION} total digits.")
    return canonical


def _canonical_currency(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkbenchContractError("currency must be a string.")
    currency = value.strip()
    if len(currency) != 3 or not currency.isalpha():
        raise WorkbenchContractError("currency must be a three-letter code.")
    return currency.upper()


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _validate_text_length(value: str | None, max_length: int, message: str) -> None:
    if value is not None and len(value) > max_length:
        raise WorkbenchContractError(message)


def _require_positive_int(value: int | None, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_enum(value: Enum, expected_type: type[Enum], message: str) -> None:
    if not isinstance(value, expected_type):
        raise WorkbenchContractError(message)


def _decimal_scale(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return abs(exponent) if exponent < 0 else 0


def _decimal_integer_digits(value: Decimal) -> int:
    adjusted = abs(value).adjusted()
    return adjusted + 1 if adjusted >= 0 else 0
