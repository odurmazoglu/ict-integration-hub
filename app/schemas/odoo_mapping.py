from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

MappingStatus = Literal["ready", "needs_review"]
MappingSeverity = Literal["warning", "missing_field"]


class OdooMappingIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field_path: str
    severity: MappingSeverity


class OdooPartnerCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    odoo_id: int | None = None
    role: Literal["supplier", "customer"]
    lookup_key: Literal["tax_id", "name"]
    name: str | None = None
    tax_id: str | None = None
    tax_office: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    country: str | None = None


class OdooTaxCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    odoo_id: int | None = None
    name: str | None = None
    percent: Decimal | None = None
    price_include: bool | None = None
    exemption_reason_code: str | None = None
    exemption_reason: str | None = None


class OdooProductCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    odoo_id: int | None = None
    default_code: str | None = None
    lookup_key: Literal["name"]
    name: str


class OdooJournalCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    odoo_id: int | None = None
    journal_type: Literal["purchase"]
    currency: str | None = None


class OdooInvoiceLinePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int
    product: OdooProductCandidate | None = None
    description: str
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    unit_of_measure: str | None = None
    taxes: list[OdooTaxCandidate]
    line_extension_amount: Decimal


class OdooDraftInvoicePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    move_type: Literal["in_invoice"]
    invoice_date: date | None = None
    currency: str | None = None
    currency_id: int | None = None
    journal: OdooJournalCandidate | None = None
    partner: OdooPartnerCandidate | None = None
    invoice_lines: list[OdooInvoiceLinePayload]
    taxes: list[OdooTaxCandidate]
    references: list[str]
    notes: list[str]
    payment_terms: str | None = None
    payment_term_id: int | None = None
    invoice_number: str | None = None
    ettn: str | None = None


class OdooMappingPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice: OdooDraftInvoicePayload
    lines: list[OdooInvoiceLinePayload]
    warnings: list[OdooMappingIssue]
    missing_fields: list[OdooMappingIssue]
    mapping_status: MappingStatus
