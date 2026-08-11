from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError


@dataclass(frozen=True, slots=True)
class WorkbenchBillingAuthoringRow(ApplicationDTO):
    """One Odoo-authored Customer Invoice billing line for a Workbench review."""

    odoo_record_id: int
    review_id: str
    company_id: int
    review_version: int
    billing_group_key: str
    allocation_key: str
    customer_id: int
    product_id: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    sales_tax_ids: tuple[int, ...]
    billing_ready: bool
    sequence: int | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.odoo_record_id, "odoo_record_id must be a positive ERP id.")
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        _require_text(self.billing_group_key, "billing_group_key is required.")
        _require_text(self.allocation_key, "allocation_key is required.")
        _require_positive_int(self.customer_id, "customer_id must be a positive ERP id.")
        _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
        _require_text(self.description, "description is required.")
        _require_positive_decimal(self.quantity, "quantity must be a positive Decimal value.")
        _require_positive_decimal(self.unit_price, "unit_price must be a positive Decimal value.")
        _require_text(self.currency, "currency is required.")
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise WorkbenchContractError("currency must be a stable ISO-4217 code.")
        sales_tax_ids = tuple(self.sales_tax_ids)
        if not sales_tax_ids:
            raise WorkbenchContractError("sales_tax_ids are required.")
        if len(set(sales_tax_ids)) != len(sales_tax_ids):
            raise WorkbenchContractError("sales_tax_ids must be unique per billing line.")
        for tax_id in sales_tax_ids:
            _require_positive_int(tax_id, "sales_tax_ids must contain positive ERP ids.")
        if type(self.billing_ready) is not bool:
            raise WorkbenchContractError("billing_ready must be boolean.")
        if self.sequence is not None:
            _require_positive_int(self.sequence, "sequence must be positive when supplied.")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "sales_tax_ids", sales_tax_ids)


@dataclass(frozen=True, slots=True)
class CaptureOdooWorkbenchBillingEvidenceCommand(Command):
    review_id: str
    company_id: int

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")


@dataclass(frozen=True, slots=True)
class CaptureOdooWorkbenchBillingEvidenceResult(ApplicationDTO):
    review_id: str
    company_id: int
    review_version: int
    billing_keys: tuple[str, ...] = field(default_factory=tuple)
    captured: bool = True

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.review_version, "review_version must be positive.")
        billing_keys = tuple(self.billing_keys)
        if not billing_keys:
            raise WorkbenchContractError("billing_keys are required.")
        for billing_key in billing_keys:
            _require_text(billing_key, "billing_key is required.")
        if len(set(billing_keys)) != len(billing_keys):
            raise WorkbenchContractError("billing_keys must be unique.")
        if self.captured is not True:
            raise WorkbenchContractError("captured must be true.")
        object.__setattr__(self, "billing_keys", billing_keys)


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_positive_decimal(value: Decimal, message: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= Decimal("0"):
        raise WorkbenchContractError(message)
