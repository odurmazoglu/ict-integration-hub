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
    invoice_lines: tuple[VendorBillLine, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
