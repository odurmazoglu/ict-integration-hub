from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class VendorBillLine:
    product_id: int
    quantity: Decimal
    uom: str | None
    unit_price: Decimal
    tax_ids: tuple[int, ...] = field(default_factory=tuple)
    description: str | None = None


@dataclass(frozen=True, slots=True)
class VendorBill:
    supplier_id: int
    invoice_number: str
    invoice_date: date
    currency: str
    external_uuid: str | None
    reference: str | None
    company_id: int | None = None
    invoice_lines: tuple[VendorBillLine, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class CustomerInvoiceLine:
    product_id: int
    quantity: Decimal
    unit_price: Decimal
    tax_ids: tuple[int, ...] = field(default_factory=tuple)
    description: str | None = None
    source_allocation_key: str | None = None


@dataclass(frozen=True, slots=True)
class CustomerInvoiceBillingLine:
    allocation_key: str
    product_id: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    sales_tax_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        _require_text(self.allocation_key, "allocation_key is required.")
        _require_positive_int(self.product_id, "product_id must be a positive ERP id.")
        _require_text(self.description, "description is required.")
        _require_positive_decimal(self.quantity, "quantity must be a positive Decimal value.")
        _require_positive_decimal(self.unit_price, "unit_price must be a positive Decimal value.")
        sales_tax_ids = tuple(self.sales_tax_ids)
        if not sales_tax_ids:
            raise ValueError("sales_tax_ids are required.")
        if len(set(sales_tax_ids)) != len(sales_tax_ids):
            raise ValueError("sales_tax_ids must be unique per billing line.")
        for sales_tax_id in sales_tax_ids:
            _require_positive_int(sales_tax_id, "sales_tax_ids must contain positive ERP ids.")
        object.__setattr__(self, "sales_tax_ids", sales_tax_ids)


@dataclass(frozen=True, slots=True)
class CustomerInvoiceBillingInstruction:
    billing_key: str
    customer_id: int
    currency: str
    lines: tuple[CustomerInvoiceBillingLine, ...]

    def __post_init__(self) -> None:
        _require_text(self.billing_key, "billing_key is required.")
        _require_positive_int(self.customer_id, "customer_id must be a positive ERP id.")
        _require_text(self.currency, "currency is required.")
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a stable ISO-4217 code.")
        lines = tuple(self.lines)
        if not lines:
            raise ValueError("Customer Invoice billing instruction requires at least one line.")
        for line in lines:
            if not isinstance(line, CustomerInvoiceBillingLine):
                raise ValueError("lines must contain canonical CustomerInvoiceBillingLine values.")
        allocation_keys = tuple(line.allocation_key for line in lines)
        if len(set(allocation_keys)) != len(allocation_keys):
            raise ValueError("billing instruction allocation keys must be unique.")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "lines", lines)


@dataclass(frozen=True, slots=True)
class CustomerInvoice:
    company_id: int
    customer_id: int
    invoice_date: date
    currency: str
    external_uuid: str | None
    reference: str
    invoice_lines: tuple[CustomerInvoiceLine, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValueError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(message)


def _require_positive_decimal(value: Decimal, message: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= Decimal("0"):
        raise ValueError(message)
