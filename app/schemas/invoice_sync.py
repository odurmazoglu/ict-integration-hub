from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.uyumsoft_invoices import InvoiceDirection

SyncDirection = Literal["Inbox", "Outbox", "Both"]


class DirectionSyncSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction: InvoiceDirection
    pages_fetched: int
    invoices_seen: int
    created: int
    updated: int
    skipped: int


class UyumsoftInvoiceSyncResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    created: int
    updated: int
    skipped: int
    directions: list[DirectionSyncSummaryResponse]
