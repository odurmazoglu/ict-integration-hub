"""Immutable RESALE accounting pin for one accepted Vendor Bill decision (P0-PROD-18F-1).

When a Vendor Bill decision is accepted for a review whose current-version purchase
purpose is RESALE, the P0-PROD-18E-1B decision gate has already proven -- via
P0-PROD-18D discovery and the P0-PROD-18E-1A eligibility policy -- which Odoo
product/category purchase account each product-backed line would get. This module
freezes exactly that accepted evidence so later Odoo configuration changes cannot
silently change what the decision was accepted on.

Scope, deliberately narrow:

* the pinned account is the *pre-fiscal-position* account Odoo's product-category
  configuration resolves to. Fiscal-position mapping is not evaluated and no final
  Vendor Bill line account is claimed (``fiscal_position_mapping`` is always
  ``NOT_EVALUATED``);
* nothing here is sent to Odoo. Vendor Bill execution is unchanged -- sending the
  pinned account, execution-time drift detection and readback verification are
  P0-PROD-18F-2;
* no account id/code/name/type is interpreted: whatever valid account Odoo
  configuration resolved is recorded verbatim.

The persisted shape is a strict, versioned JSON document (see
:func:`resale_accounting_pin_to_data` / :func:`resale_accounting_pin_from_data`);
any deviation fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import ResaleDecisionEligibilityError, WorkbenchContractError
from app.application.workbench.purchase_account_discovery import (
    FiscalPositionMapping,
    ProductPurchaseAccountResolution,
)
from app.application.workbench.purchase_purpose import PurchasePurpose
from app.application.workbench.resale_product_eligibility import ResaleProductEligibility

RESALE_ACCOUNTING_PIN_SCHEMA_VERSION = 1


class ResaleAccountingSource(StrEnum):
    """Where a pinned RESALE account came from. v1 accepts the category-derived account only."""

    RESALE_PRODUCT_CATEGORY = "resale_product_category"


@dataclass(frozen=True, slots=True)
class ResaleAccountingLinePin(ApplicationDTO):
    """The accepted accounting evidence for one product-backed invoice line."""

    line_number: str
    product_id: int
    product_categ_id: int
    pre_fiscal_position_account_id: int
    pre_fiscal_position_account_code: str
    pre_fiscal_position_account_name: str
    pre_fiscal_position_account_type: str
    product_categ_name: str | None = None
    account_source: ResaleAccountingSource = ResaleAccountingSource.RESALE_PRODUCT_CATEGORY
    fiscal_position_mapping: FiscalPositionMapping = FiscalPositionMapping.NOT_EVALUATED

    def __post_init__(self) -> None:
        _require_text(self.line_number, "line_number")
        _require_positive_int(self.product_id, "product_id")
        _require_positive_int(self.product_categ_id, "product_categ_id")
        _require_positive_int(self.pre_fiscal_position_account_id, "pre_fiscal_position_account_id")
        _require_text(self.pre_fiscal_position_account_code, "pre_fiscal_position_account_code")
        _require_text(self.pre_fiscal_position_account_name, "pre_fiscal_position_account_name")
        _require_text(self.pre_fiscal_position_account_type, "pre_fiscal_position_account_type")
        if self.product_categ_name is not None:
            _require_text(self.product_categ_name, "product_categ_name")
        if not isinstance(self.account_source, ResaleAccountingSource):
            raise WorkbenchContractError("account_source must be a canonical ResaleAccountingSource.")
        if self.fiscal_position_mapping is not FiscalPositionMapping.NOT_EVALUATED:
            raise WorkbenchContractError("A RESALE pin never claims an evaluated fiscal-position mapping.")


@dataclass(frozen=True, slots=True)
class ResaleAccountingPin(ApplicationDTO):
    """Every RESALE line pin for one accepted decision, taken at ``review_version``."""

    review_version: int
    lines: tuple[ResaleAccountingLinePin, ...]
    purchase_purpose: PurchasePurpose = PurchasePurpose.RESALE
    schema_version: int = RESALE_ACCOUNTING_PIN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        _require_positive_int(self.review_version, "review_version")
        if self.schema_version != RESALE_ACCOUNTING_PIN_SCHEMA_VERSION:
            raise WorkbenchContractError("Unsupported RESALE accounting pin schema_version.")
        if self.purchase_purpose is not PurchasePurpose.RESALE:
            raise WorkbenchContractError("A RESALE accounting pin requires the RESALE purchase purpose.")
        if not self.lines:
            raise WorkbenchContractError("A RESALE accounting pin requires at least one line.")
        if not all(isinstance(line, ResaleAccountingLinePin) for line in self.lines):
            raise WorkbenchContractError("RESALE accounting pin lines must be ResaleAccountingLinePin.")
        line_numbers = [line.line_number for line in self.lines]
        if len(set(line_numbers)) != len(line_numbers):
            raise WorkbenchContractError("RESALE accounting pin line numbers must be unique.")

    def by_line_number(self) -> dict[str, ResaleAccountingLinePin]:
        return {line.line_number: line for line in self.lines}


def resale_line_pin_from_accepted_evidence(
    line_number: str | None,
    resolution: ProductPurchaseAccountResolution | None,
    eligibility: ResaleProductEligibility,
) -> ResaleAccountingLinePin:
    """Freeze one line exactly as the 18E-1A policy accepted it -- no reinterpretation.

    Every value is copied from the eligible result and the 18D resolution it was
    computed from; anything incomplete or inconsistent fails closed.
    """

    account = eligibility.pre_fiscal_position_account
    category = resolution.category if resolution is not None else None
    if (
        not eligibility.eligible
        or account is None
        or eligibility.product_id is None
        or eligibility.category_id is None
        or resolution is None
        or resolution.product_id != eligibility.product_id
        or category is None
        or category.category_id != eligibility.category_id
        or resolution.pre_fiscal_position_account != account
    ):
        raise ResaleDecisionEligibilityError("RESALE accounting evidence is incomplete; the decision cannot be pinned.")
    if not isinstance(line_number, str) or not line_number.strip():
        raise ResaleDecisionEligibilityError("RESALE accounting pins require every invoice line to have a line number.")
    return ResaleAccountingLinePin(
        line_number=line_number,
        product_id=eligibility.product_id,
        product_categ_id=eligibility.category_id,
        product_categ_name=category.category_complete_name or category.category_name,
        pre_fiscal_position_account_id=account.account_id,
        pre_fiscal_position_account_code=account.code,
        pre_fiscal_position_account_name=account.name,
        pre_fiscal_position_account_type=account.account_type,
        account_source=ResaleAccountingSource.RESALE_PRODUCT_CATEGORY,
        fiscal_position_mapping=eligibility.fiscal_position_mapping,
    )


_LINE_KEYS = frozenset(
    {
        "line_number",
        "product_id",
        "product_categ_id",
        "product_categ_name",
        "pre_fiscal_position_account_id",
        "pre_fiscal_position_account_code",
        "pre_fiscal_position_account_name",
        "pre_fiscal_position_account_type",
        "account_source",
        "fiscal_position_mapping",
    }
)
_PIN_KEYS = frozenset({"schema_version", "purchase_purpose", "review_version", "lines"})


def resale_accounting_pin_to_data(pin: ResaleAccountingPin) -> dict[str, Any]:
    """The canonical persisted JSON shape of a pin."""

    if not isinstance(pin, ResaleAccountingPin):
        raise WorkbenchContractError("A ResaleAccountingPin is required.")
    return {
        "schema_version": pin.schema_version,
        "purchase_purpose": pin.purchase_purpose.value,
        "review_version": pin.review_version,
        "lines": [
            {
                "line_number": line.line_number,
                "product_id": line.product_id,
                "product_categ_id": line.product_categ_id,
                "product_categ_name": line.product_categ_name,
                "pre_fiscal_position_account_id": line.pre_fiscal_position_account_id,
                "pre_fiscal_position_account_code": line.pre_fiscal_position_account_code,
                "pre_fiscal_position_account_name": line.pre_fiscal_position_account_name,
                "pre_fiscal_position_account_type": line.pre_fiscal_position_account_type,
                "account_source": line.account_source.value,
                "fiscal_position_mapping": line.fiscal_position_mapping.value,
            }
            for line in pin.lines
        ],
    }


def resale_accounting_pin_from_data(data: object) -> ResaleAccountingPin:
    """Strictly hydrate a persisted pin; any unknown, missing or mistyped field fails closed."""

    if not isinstance(data, dict) or set(data) != _PIN_KEYS:
        raise WorkbenchContractError("RESALE accounting pin has an invalid shape.")
    lines = data["lines"]
    if not isinstance(lines, list):
        raise WorkbenchContractError("RESALE accounting pin lines must be a list.")
    try:
        return ResaleAccountingPin(
            schema_version=data["schema_version"],
            purchase_purpose=PurchasePurpose(data["purchase_purpose"]),
            review_version=data["review_version"],
            lines=tuple(_line_from_data(line) for line in lines),
        )
    except ValueError as exc:
        raise WorkbenchContractError("RESALE accounting pin has an invalid value.") from exc


def _line_from_data(data: object) -> ResaleAccountingLinePin:
    if not isinstance(data, dict) or set(data) != _LINE_KEYS:
        raise WorkbenchContractError("RESALE accounting pin line has an invalid shape.")
    return ResaleAccountingLinePin(
        line_number=data["line_number"],
        product_id=data["product_id"],
        product_categ_id=data["product_categ_id"],
        product_categ_name=data["product_categ_name"],
        pre_fiscal_position_account_id=data["pre_fiscal_position_account_id"],
        pre_fiscal_position_account_code=data["pre_fiscal_position_account_code"],
        pre_fiscal_position_account_name=data["pre_fiscal_position_account_name"],
        pre_fiscal_position_account_type=data["pre_fiscal_position_account_type"],
        account_source=ResaleAccountingSource(data["account_source"]),
        fiscal_position_mapping=FiscalPositionMapping(data["fiscal_position_mapping"]),
    )


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(f"{label} must be non-empty text.")


def _require_positive_int(value: object, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(f"{label} must be a positive integer.")


__all__ = [
    "RESALE_ACCOUNTING_PIN_SCHEMA_VERSION",
    "ResaleAccountingLinePin",
    "ResaleAccountingPin",
    "ResaleAccountingSource",
    "resale_accounting_pin_from_data",
    "resale_accounting_pin_to_data",
    "resale_line_pin_from_accepted_evidence",
]
