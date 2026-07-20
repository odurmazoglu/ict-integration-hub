from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class NormalizedAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    street_name: str | None = None
    building_number: str | None = None
    city_subdivision_name: str | None = None
    city_name: str | None = None
    postal_zone: str | None = None
    country: str | None = None


class NormalizedContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    telephone: str | None = None
    telefax: str | None = None
    email: str | None = None


class NormalizedParty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tax_id: str | None = None
    party_name: str | None = None
    tax_office: str | None = None
    address: NormalizedAddress | None = None
    contact: NormalizedContact | None = None


class NormalizedMonetaryTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_extension_amount: Decimal | None = None
    tax_exclusive_amount: Decimal | None = None
    tax_inclusive_amount: Decimal | None = None
    allowance_total_amount: Decimal | None = None
    charge_total_amount: Decimal | None = None
    payable_amount: Decimal


class NormalizedTaxSubtotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tax_category: str | None = None
    percent: Decimal | None = None
    taxable_amount: Decimal | None = None
    tax_amount: Decimal
    exemption_reason: str | None = None
    exemption_reason_code: str | None = None


class NormalizedTaxTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tax_amount: Decimal
    subtotals: list[NormalizedTaxSubtotal]


class NormalizedAllowanceCharge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charge_indicator: bool
    reason: str | None = None
    amount: Decimal


class NormalizedInvoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_id: str
    item_name: str | None = None
    description: str | None = None
    quantity: Decimal | None = None
    unit_code: str | None = None
    unit_price: Decimal | None = None
    line_extension_amount: Decimal
    allowance_charges: list[NormalizedAllowanceCharge]
    tax_totals: list[NormalizedTaxTotal]


class NormalizedInvoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    ettn: str
    invoice_type_code: str | None = None
    profile_id: str | None = None
    issue_datetime: datetime
    document_currency: str
    notes: list[str]
    references: list[str]
    supplier: NormalizedParty
    customer: NormalizedParty
    monetary_totals: NormalizedMonetaryTotals
    tax_totals: list[NormalizedTaxTotal]
    lines: list[NormalizedInvoiceLine]
