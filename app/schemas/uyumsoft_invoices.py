from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

InvoiceDirection = Literal["Inbox", "Outbox"]
InvoiceListDateField = Literal["execution", "create"]


class UyumsoftInvoiceListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: datetime
    to_date: datetime
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    only_newest_invoices: bool = False
    date_field: InvoiceListDateField = "execution"
    #: PR R. Provider-side exact-number filter (``InvoiceNumbers``); empty sends nothing.
    invoice_numbers: tuple[str, ...] = ()
    #: PR R. Provider ``IsArchived`` filter; ``None`` leaves it unset (provider default).
    is_archived: bool | None = None


class UyumsoftInvoiceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: str | None = None
    ettn: str | None = None
    invoice_number: str | None = None
    invoice_date: datetime | None = None
    sender: str | None = None
    receiver: str | None = None
    tax_number: str | None = None
    currency: str | None = None
    total_amount: Decimal | None = None
    direction: InvoiceDirection
    status: str | None = None
    extra_fields: dict[str, Any] = Field(default_factory=dict)


class UyumsoftInvoiceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: InvoiceDirection
    page: int
    page_size: int
    total_count: int | None = None
    invoices: list[UyumsoftInvoiceSummary]
    extra_fields: dict[str, Any] = Field(default_factory=dict)


class UyumsoftOutboxInvoiceRecord(BaseModel):
    """One ``OutboxInvoiceListItem`` mapped with the verified Uyumsoft field semantics.

    Verified against production (PR R probe): ``InvoiceId`` is the official GİB invoice
    number, ``DocumentId`` is the ETTN, ``InvoiceTipType`` is the invoice type (e.g.
    ``Sales``) and ``Type`` is the scenario (e.g. ``ComercialInvoice``). The generic list
    summary mapping keeps its historical aliases; this record never reuses them.
    """

    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = None
    ettn: str | None = None
    invoice_type: str | None = None
    scenario: str | None = None
    status: str | None = None
    target_tax_number: str | None = None
    currency: str | None = None
    #: Calendar issue date (``ExecutionDate``) in Turkish local time; a naive provider
    #: timestamp is already local and is never shifted through UTC.
    issue_date: date | None = None
    is_archived: bool | None = None


class UyumsoftOutboxLookupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_size: int
    total_count: int | None = None
    invoices: list[UyumsoftOutboxInvoiceRecord]


class UyumsoftInvoiceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: InvoiceDirection
    invoice_id: str
    content: bytes
    mime_type: str = "application/xml"
