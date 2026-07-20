from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

InvoiceDirection = Literal["Inbox", "Outbox"]


class UyumsoftInvoiceListRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_date: datetime
    to_date: datetime
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


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


class UyumsoftInvoiceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: InvoiceDirection
    invoice_id: str
    content: bytes
    mime_type: str = "application/xml"
