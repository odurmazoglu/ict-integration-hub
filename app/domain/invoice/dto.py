from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Address:
    street: str | None = None
    building_number: str | None = None
    city: str | None = None
    district: str | None = None
    postal_code: str | None = None
    country: str | None = None


@dataclass(frozen=True, slots=True)
class Party:
    name: str | None = None
    tax_number: str | None = None
    tax_office: str | None = None
    mersis_number: str | None = None
    website: str | None = None
    emails: tuple[str, ...] = field(default_factory=tuple)
    phones: tuple[str, ...] = field(default_factory=tuple)
    addresses: tuple[Address, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Header:
    invoice_number: str
    invoice_uuid: str
    ettn: str | None = None
    invoice_type: str | None = None
    profile_id: str | None = None
    issue_date: date | None = None
    issue_time: time | None = None
    currency_code: str | None = None
    exchange_rate: Decimal | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MonetaryTotals:
    line_extension_amount: Decimal | None = None
    tax_exclusive_amount: Decimal | None = None
    tax_inclusive_amount: Decimal | None = None
    allowance_total: Decimal | None = None
    charge_total: Decimal | None = None
    payable_amount: Decimal | None = None
    rounding_amount: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Tax:
    tax_type: str | None = None
    rate: Decimal | None = None
    base_amount: Decimal | None = None
    tax_amount: Decimal | None = None
    exemption_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Discount:
    amount: Decimal | None = None
    reason: str | None = None
    rate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    line_number: str | None = None
    description: str | None = None
    seller_item_code: str | None = None
    buyer_item_code: str | None = None
    barcode: str | None = None
    quantity: Decimal | None = None
    unit_code: str | None = None
    unit_price: Decimal | None = None
    line_extension_amount: Decimal | None = None
    discounts: tuple[Discount, ...] = field(default_factory=tuple)
    taxes: tuple[Tax, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    size: int | None = None


@dataclass(frozen=True, slots=True)
class InternalInvoice:
    header: Header
    supplier: Party
    customer: Party
    totals: MonetaryTotals
    lines: tuple[InvoiceLine, ...] = field(default_factory=tuple)
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)
