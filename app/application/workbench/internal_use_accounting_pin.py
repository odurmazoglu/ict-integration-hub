"""Immutable INTERNAL_USE accounting pin for one accepted Vendor Bill decision.

A product-backed invoice bought for ICT's own use (e.g. internal Microsoft 365
subscriptions) keeps its Odoo products, but must never be booked to the product
category's purchase account -- that account is configured for RESALE. The operator
records the INTERNAL_USE purchase purpose and an explicit review-scoped
``EXPENSE_ACCOUNT`` accounting resolution; the decision gate
(:mod:`app.application.workbench.internal_use_decision_gate`) re-validates that
account read-only and freezes it here, per line, together with the accepted product.

Execution sends exactly this account on every line (after re-validating it), so a
later change to the category or to the accounting resolution can never silently
change what the decision was accepted on. Nothing here is sent to Odoo by itself.

The persisted shape is a strict, versioned JSON document; any deviation fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.application.dto import ApplicationDTO
from app.application.expense_mapping.contracts import EXPENSE_CATEGORY_PATTERN
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workbench.purchase_purpose import PurchasePurpose

INTERNAL_USE_ACCOUNTING_PIN_SCHEMA_VERSION = 1


class InternalUseAccountSource(StrEnum):
    """Where the pinned INTERNAL_USE account came from. v1: the review-scoped accounting resolution only."""

    REVIEW_ACCOUNTING_RESOLUTION = "review_accounting_resolution"


@dataclass(frozen=True, slots=True)
class InternalUseAccountingLinePin(ApplicationDTO):
    """One product-backed line and the product it was accepted with."""

    line_number: str
    product_id: int

    def __post_init__(self) -> None:
        _require_text(self.line_number, "line_number")
        _require_positive_int(self.product_id, "product_id")


@dataclass(frozen=True, slots=True)
class InternalUseAccountingPin(ApplicationDTO):
    """The explicitly selected internal expense account for every line of one decision."""

    review_version: int
    accounting_resolution_id: int
    expense_account_id: int
    expense_account_code: str
    expense_account_name: str
    expense_account_type: str
    expense_category: str
    lines: tuple[InternalUseAccountingLinePin, ...]
    account_source: InternalUseAccountSource = InternalUseAccountSource.REVIEW_ACCOUNTING_RESOLUTION
    purchase_purpose: PurchasePurpose = PurchasePurpose.INTERNAL_USE
    schema_version: int = INTERNAL_USE_ACCOUNTING_PIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        _require_positive_int(self.review_version, "review_version")
        _require_positive_int(self.accounting_resolution_id, "accounting_resolution_id")
        _require_positive_int(self.expense_account_id, "expense_account_id")
        _require_text(self.expense_account_code, "expense_account_code")
        _require_text(self.expense_account_name, "expense_account_name")
        _require_text(self.expense_account_type, "expense_account_type")
        if not isinstance(self.expense_category, str) or not EXPENSE_CATEGORY_PATTERN.match(self.expense_category):
            raise WorkbenchContractError("INTERNAL_USE accounting pin expense_category is invalid.")
        if self.schema_version != INTERNAL_USE_ACCOUNTING_PIN_SCHEMA_VERSION:
            raise WorkbenchContractError("Unsupported INTERNAL_USE accounting pin schema_version.")
        if self.purchase_purpose is not PurchasePurpose.INTERNAL_USE:
            raise WorkbenchContractError("An INTERNAL_USE accounting pin requires the INTERNAL_USE purchase purpose.")
        if not isinstance(self.account_source, InternalUseAccountSource):
            raise WorkbenchContractError("account_source must be a canonical InternalUseAccountSource.")
        if not self.lines:
            raise WorkbenchContractError("An INTERNAL_USE accounting pin requires at least one line.")
        if not all(isinstance(line, InternalUseAccountingLinePin) for line in self.lines):
            raise WorkbenchContractError("INTERNAL_USE accounting pin lines must be InternalUseAccountingLinePin.")
        line_numbers = [line.line_number for line in self.lines]
        if len(set(line_numbers)) != len(line_numbers):
            raise WorkbenchContractError("INTERNAL_USE accounting pin line numbers must be unique.")

    def by_line_number(self) -> dict[str, InternalUseAccountingLinePin]:
        return {line.line_number: line for line in self.lines}


_LINE_KEYS = frozenset({"line_number", "product_id"})
_PIN_KEYS = frozenset(
    {
        "schema_version",
        "purchase_purpose",
        "review_version",
        "accounting_resolution_id",
        "account_source",
        "expense_account_id",
        "expense_account_code",
        "expense_account_name",
        "expense_account_type",
        "expense_category",
        "lines",
    }
)


def internal_use_accounting_pin_to_data(pin: InternalUseAccountingPin) -> dict[str, Any]:
    """The canonical persisted JSON shape of a pin."""

    if not isinstance(pin, InternalUseAccountingPin):
        raise WorkbenchContractError("An InternalUseAccountingPin is required.")
    return {
        "schema_version": pin.schema_version,
        "purchase_purpose": pin.purchase_purpose.value,
        "review_version": pin.review_version,
        "accounting_resolution_id": pin.accounting_resolution_id,
        "account_source": pin.account_source.value,
        "expense_account_id": pin.expense_account_id,
        "expense_account_code": pin.expense_account_code,
        "expense_account_name": pin.expense_account_name,
        "expense_account_type": pin.expense_account_type,
        "expense_category": pin.expense_category,
        "lines": [{"line_number": line.line_number, "product_id": line.product_id} for line in pin.lines],
    }


def internal_use_accounting_pin_from_data(data: object) -> InternalUseAccountingPin:
    """Strictly hydrate a persisted pin; any unknown, missing or mistyped field fails closed."""

    if not isinstance(data, dict) or set(data) != _PIN_KEYS:
        raise WorkbenchContractError("INTERNAL_USE accounting pin has an invalid shape.")
    lines = data["lines"]
    if not isinstance(lines, list) or not all(isinstance(line, dict) and set(line) == _LINE_KEYS for line in lines):
        raise WorkbenchContractError("INTERNAL_USE accounting pin lines have an invalid shape.")
    try:
        purpose = PurchasePurpose(data["purchase_purpose"])
        source = InternalUseAccountSource(data["account_source"])
    except (TypeError, ValueError) as exc:
        raise WorkbenchContractError("INTERNAL_USE accounting pin has an invalid enum value.") from exc
    return InternalUseAccountingPin(
        schema_version=_strict_int(data["schema_version"]),
        purchase_purpose=purpose,
        review_version=_strict_int(data["review_version"]),
        accounting_resolution_id=_strict_int(data["accounting_resolution_id"]),
        account_source=source,
        expense_account_id=_strict_int(data["expense_account_id"]),
        expense_account_code=data["expense_account_code"],
        expense_account_name=data["expense_account_name"],
        expense_account_type=data["expense_account_type"],
        expense_category=data["expense_category"],
        lines=tuple(
            InternalUseAccountingLinePin(line_number=line["line_number"], product_id=_strict_int(line["product_id"]))
            for line in lines
        ),
    )


def _strict_int(value: object) -> int:
    if type(value) is not int:
        raise WorkbenchContractError("INTERNAL_USE accounting pin integer field is invalid.")
    return value


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(f"INTERNAL_USE accounting pin {field_name} is required.")


def _require_positive_int(value: object, field_name: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(f"INTERNAL_USE accounting pin {field_name} must be a positive integer.")


__all__ = [
    "INTERNAL_USE_ACCOUNTING_PIN_SCHEMA_VERSION",
    "InternalUseAccountSource",
    "InternalUseAccountingLinePin",
    "InternalUseAccountingPin",
    "internal_use_accounting_pin_from_data",
    "internal_use_accounting_pin_to_data",
]
